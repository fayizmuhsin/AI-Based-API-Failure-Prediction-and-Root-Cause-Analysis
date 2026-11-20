"""Isolated real-process end-to-end evaluation of Phase 8 fault scenarios.

This is intentionally a bounded integration test (up to twenty minutes) rather
than a unit test. All six processes, both SQLite databases, and all scenario
results belong to a temporary directory owned by this test.
"""

from __future__ import annotations

import json
import gc
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.evaluation.reporting import (  # noqa: E402
    build_evaluation_artifact,
    check_expected_label_leaks,
    new_evaluation_run_id,
    utc_now,
    write_evaluation_artifact,
)


SERVICE_MODULES = {
    "api-gateway": "services.gateway.main",
    "user-service": "services.user.main",
    "order-service": "services.order.main",
    "database-service": "services.database.main",
}
SCENARIO_IDS = (
    "database_connection_failure",
    "api_latency_injection",
    "upstream_service_unavailable",
    "cascading_service_failure",
)
EXPECTED_SCENARIO_LABELS = {
    "database_connection_failure": "expected_database_failure",
    "api_latency_injection": "expected_latency_injection",
    "upstream_service_unavailable": "expected_upstream_unavailable",
    "cascading_service_failure": "expected_cascading_failure",
}
RCA_CORRELATION_WINDOW_SECONDS = 90
SCENARIO_REQUESTS = {
    "database_connection_failure": ("GET", "/api/users/user-1001", None, 6),
    "api_latency_injection": ("GET", "/api/users/user-1001", None, 5),
    "upstream_service_unavailable": ("GET", "/api/users/user-1001", None, 6),
    "cascading_service_failure": (
        "POST", "/api/orders",
        {"user_id": "user-1001", "item": "Temporary evaluation order", "quantity": 1},
        5,
    ),
}
START_TIMEOUT_SECONDS = 25
WINDOW_PROCESSING_TIMEOUT_SECONDS = 85
TOTAL_EVALUATION_DEADLINE_SECONDS = 1_200


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 4.0,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url, data=data, method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, json.loads(payload.decode("utf-8")) if payload else {}
    except HTTPError as exc:
        with exc:
            payload = exc.read()
            try:
                result = json.loads(payload.decode("utf-8")) if payload else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                result = {"detail": payload.decode("utf-8", errors="replace")}
            return exc.code, result


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Phase8IsolatedEvaluationTests(unittest.TestCase):
    """Run actual backend/services/controller subprocesses against temp state."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.evaluation_started_at = utc_now()
        cls.evaluation_run_id = new_evaluation_run_id()
        cls.temp = tempfile.TemporaryDirectory(prefix="phase8-evaluation-")
        cls.temp_path = Path(cls.temp.name).resolve()
        simulated_root = PROJECT_ROOT / "data" / "simulated"
        simulated_root.mkdir(parents=True, exist_ok=True)
        cls.application_temp = tempfile.TemporaryDirectory(prefix=".phase8-evaluation-", dir=simulated_root)
        cls.application_temp_path = Path(cls.application_temp.name).resolve()
        cls.application_database = cls.application_temp_path / "application-test.db"
        cls.monitoring_database = cls.temp_path / "monitoring" / "monitoring-test.db"
        cls.monitoring_database.parent.mkdir(parents=True)
        if cls.application_database == cls.monitoring_database:
            raise AssertionError("Evaluation database paths must be distinct")

        cls.ports = {
            "monitoring": _unused_loopback_port(),
            "fault-simulator": _unused_loopback_port(),
            **{service_id: _unused_loopback_port() for service_id in SERVICE_MODULES},
        }
        # Avoid even transient collisions within this test's own allocation.
        if len(set(cls.ports.values())) != len(cls.ports):
            cls.temp.cleanup()
            cls.application_temp.cleanup()
            raise RuntimeError("Could not reserve distinct temporary loopback ports")

        cls.processes: list[subprocess.Popen] = []
        cls.log_streams = []
        cls.run_ids: list[tuple[str, str]] = []
        cls.scenario_results: list[dict[str, Any]] = []
        cls.controller_url = f"http://127.0.0.1:{cls.ports['fault-simulator']}"
        cls.monitor_url = f"http://127.0.0.1:{cls.ports['monitoring']}"
        cls.gateway_url = f"http://127.0.0.1:{cls.ports['api-gateway']}"

        try:
            cls._seed_isolated_trained_model()
            env = cls._subprocess_environment()
            cls._launch(
                "monitoring-backend",
                [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
                 "--host", "127.0.0.1", "--port", str(cls.ports["monitoring"])],
                env,
            )
            cls._wait_health(cls.monitor_url + "/api/health", "monitoring backend")
            # Monitoring alone must not touch the test simulated application DB.
            if cls.application_database.exists():
                raise AssertionError("Monitoring backend unexpectedly created the simulated DB before services started")

            # Start dependencies before callers; each process owns only its test port.
            service_order = ("database-service", "user-service", "order-service", "api-gateway")
            for service_id in service_order:
                cls._launch(service_id, [sys.executable, "-m", SERVICE_MODULES[service_id]], env)
                cls._wait_health(
                    f"http://127.0.0.1:{cls.ports[service_id]}/health", service_id,
                )

            # Invoke the actual controller ASGI app on a dynamically allocated port.
            cls._launch(
                "fault-simulator",
                [sys.executable, "-m", "uvicorn", "services.fault_simulator.main:app",
                 "--host", "127.0.0.1", "--port", str(cls.ports["fault-simulator"]), "--no-access-log"],
                env,
            )
            cls._wait_health(cls.controller_url + "/health", "fault simulator")
            status, scenarios = _request_json(cls.controller_url + "/scenarios")
            if status != 200 or {item["scenario_id"] for item in scenarios.get("items", [])} != set(SCENARIO_IDS):
                raise AssertionError(f"Fault controller scenario readiness failed: {status} {scenarios}")
            cls.scenario_catalog_response = scenarios

            status, model = _request_json(cls.monitor_url + "/api/anomalies/model")
            if status != 200 or model.get("ready") is not True:
                raise AssertionError(f"Temporary backend did not load its isolated trained model: {model}")
            cls.model_status = model
        except Exception:
            cls._cleanup_processes()
            try:
                cls._cleanup_tempdir(cls.temp)
            finally:
                cls._cleanup_tempdir(cls.application_temp)
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._stop_all_fault_runs()
            artifact = build_evaluation_artifact(
                getattr(cls, "scenario_results", []),
                EXPECTED_SCENARIO_LABELS,
                cls.evaluation_started_at,
                utc_now(),
                cls.evaluation_run_id,
            )
            json_path, summary_path = write_evaluation_artifact(
                artifact, PROJECT_ROOT / "evaluation_output" / "phase8",
            )
            print(
                "\nPHASE8_EVALUATION_ARTIFACTS="
                + json.dumps({"json": str(json_path), "summary": str(summary_path)}, sort_keys=True)
            )
        finally:
            cls._cleanup_processes()
            try:
                cls._cleanup_tempdir(cls.temp)
            finally:
                cls._cleanup_tempdir(cls.application_temp)

    @staticmethod
    def _cleanup_tempdir(directory: tempfile.TemporaryDirectory, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                directory.cleanup()
                return
            except PermissionError:
                # SQLite handles and Windows file scanners can release a lock
                # just after the owning process exits. Retry without ignoring
                # a persistent cleanup failure.
                gc.collect()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    @classmethod
    def _subprocess_environment(cls) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "APP_APPLICATION_DATABASE_PATH": str(cls.application_database),
            "APP_MONITORING_DATABASE_PATH": str(cls.monitoring_database),
            "APP_ANOMALY_N_ESTIMATORS": "64",
            "APP_ANOMALY_MAX_SAMPLES": "40",
            "APP_ANOMALY_RANDOM_STATE": "1708",
            "APP_ANOMALY_MIN_TRAINING_SAMPLES": "30",
            "APP_ANOMALY_MIN_TRAINING_REQUESTS": "5",
            "APP_ANOMALY_RULE_MIN_REQUESTS": "5",
            "APP_ANOMALY_WINDOW_LATENESS_SECONDS": "10",
            "APP_RCA_CORRELATION_WINDOW_SECONDS": str(RCA_CORRELATION_WINDOW_SECONDS),
            "TELEMETRY_INGEST_URL": cls.monitor_url + "/api/telemetry/ingest",
            "API_GATEWAY_PORT": str(cls.ports["api-gateway"]),
            "USER_SERVICE_PORT": str(cls.ports["user-service"]),
            "ORDER_SERVICE_PORT": str(cls.ports["order-service"]),
            "DATABASE_SERVICE_PORT": str(cls.ports["database-service"]),
            "USER_SERVICE_URL": f"http://127.0.0.1:{cls.ports['user-service']}",
            "ORDER_SERVICE_URL": f"http://127.0.0.1:{cls.ports['order-service']}",
            "DATABASE_SERVICE_URL": f"http://127.0.0.1:{cls.ports['database-service']}",
            "PYTHONUNBUFFERED": "1",
        })
        return env

    @classmethod
    def _seed_isolated_trained_model(cls) -> None:
        """Train/persist a genuine model in the isolated DB, never the project DB."""
        from app.anomaly.features import FEATURE_NAMES
        from app.anomaly.model import IsolationForestModel
        from app.anomaly.repository import AnomalyRepository
        from app.core.config import Settings
        from app.telemetry.storage import TelemetryRepository

        settings = Settings(
            application_database_path=cls.application_database,
            monitoring_database_path=cls.monitoring_database,
            anomaly_n_estimators=64,
            anomaly_max_samples=40,
            anomaly_random_state=1708,
            anomaly_min_training_samples=30,
        )
        telemetry = TelemetryRepository(cls.monitoring_database, cls.application_database)
        telemetry.initialize()
        repository = AnomalyRepository(cls.monitoring_database, cls.application_database)
        repository.initialize()
        rows = []
        for index in range(40):
            row = {name: 0.0 for name in FEATURE_NAMES}
            row.update({
                "request_count": float(8 + index % 5),
                "successful_request_count": float(8 + index % 5),
                "failed_request_count": 0.0,
                "error_rate": 0.0,
                "timeout_count": 0.0,
                "average_latency_ms": float(25 + index % 10 * 3),
                "median_latency_ms": float(22 + index % 9 * 3),
                "p95_latency_ms": float(35 + index % 10 * 4),
                "latency_deviation_from_baseline_ms": float(-8 + index % 8 * 2),
                "request_count_change": float(-1 + index % 5 * 0.25),
                "dependency_failure_rate": 0.0,
            })
            rows.append(row)
        model = IsolationForestModel(settings)
        model_bytes, metadata = model.fit(rows)
        repository.save_model(model_bytes, metadata, None)
        if repository.active_model() is None:
            raise AssertionError("Isolated model was not persisted")

    @classmethod
    def _launch(cls, label: str, command: list[str], env: dict[str, str]) -> None:
        log_path = cls.temp_path / f"{label}.log"
        stream = log_path.open("w", encoding="utf-8")
        cls.log_streams.append(stream)
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=env.copy(),
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        process.phase8_label = label
        process.phase8_log_path = log_path
        cls.processes.append(process)

    @classmethod
    def _wait_health(cls, url: str, label: str) -> dict[str, Any]:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        last_error: Exception | str | None = None
        process = next((p for p in cls.processes if p.phase8_label == label), None)
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(cls._process_failure(process))
            try:
                status, result = _request_json(url, timeout=0.75)
                if status == 200 and result.get("status") == "ok":
                    return result
                last_error = f"HTTP {status}: {result}"
            except (OSError, URLError, TimeoutError) as exc:
                last_error = exc
            time.sleep(0.1)
        if process is not None and process.poll() is not None:
            raise RuntimeError(cls._process_failure(process))
        raise RuntimeError(f"{label} health endpoint did not become ready at {url}: {last_error}")

    @classmethod
    def _process_failure(cls, process: subprocess.Popen) -> str:
        try:
            process.phase8_log_path and process.phase8_log_path.exists()
            tail = process.phase8_log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            tail = ""
        return f"{process.phase8_label} exited {process.returncode}; log tail:\n{tail}"

    @classmethod
    def _cleanup_processes(cls) -> None:
        cls._stop_all_fault_runs()
        for process in reversed(getattr(cls, "processes", [])):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for stream in getattr(cls, "log_streams", []):
            try:
                stream.close()
            except OSError:
                pass

    @classmethod
    def _stop_all_fault_runs(cls) -> None:
        for scenario_id, run_id in reversed(getattr(cls, "run_ids", [])):
            try:
                _request_json(
                    cls.controller_url + f"/scenarios/{scenario_id}/stop?run_id={run_id}",
                    "POST", timeout=1.5,
                )
            except (OSError, URLError, TimeoutError):
                # Service leases expire independently at 60 seconds at most.
                pass

    def _eventually(self, label: str, predicate, timeout: float, interval: float = 0.25):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            for process in self.processes:
                if process.poll() is not None:
                    self.fail(self._process_failure(process))
            try:
                last = predicate()
                if last:
                    return last
            except (OSError, URLError, sqlite3.Error):
                pass
            time.sleep(interval)
        return None

    def _request_gateway(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        request_started_at = datetime.now(timezone.utc).isoformat()
        try:
            status, payload = _request_json(self.gateway_url + path, method, body, timeout=4.0)
        except (OSError, URLError, TimeoutError) as exc:
            status, payload = 0, {"error": type(exc).__name__}
        return {
            "method": method, "path": path, "status_code": status,
            "duration_ms": (time.perf_counter() - started) * 1_000,
            "request_started_at": request_started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "response": payload,
        }

    @staticmethod
    def _baseline_request(scenario_id: str) -> tuple[str, str, dict | None]:
        method, path, payload, _ = SCENARIO_REQUESTS[scenario_id]
        return method, path, payload

    def _wait_for_next_minute_window(self) -> None:
        now = datetime.now(timezone.utc)
        # Keep enough of the current minute for the bounded request burst,
        # without adding a full minute after the prior window just completed.
        if now.second > 35:
            next_start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            delay = max(0.0, (next_start - now).total_seconds() + 0.15)
            time.sleep(delay)

    def _telemetry(self) -> list[dict[str, Any]]:
        status, result = _request_json(self.monitor_url + "/api/telemetry/recent?limit=500")
        if status != 200:
            raise RuntimeError(f"Telemetry API returned HTTP {status}: {result}")
        return result["items"]

    def _detection_rows(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        uri = f"file:{self.monitoring_database.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT service_id, window_start, model_version, model_score, model_decision,
                          run_status, is_anomaly, threshold_matches_json, feature_values_json
                   FROM detection_runs WHERE window_start >= ? AND window_start <= ?
                   ORDER BY window_start, service_id""",
                (start.replace(second=0, microsecond=0).isoformat(),
                 end.replace(second=0, microsecond=0).isoformat()),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["threshold_matches"] = json.loads(item.pop("threshold_matches_json"))
            item["feature_values"] = json.loads(item.pop("feature_values_json"))
            result.append(item)
        return result

    def _rca_telemetry_inputs(self, episode_start: str, episode_end: str) -> list[dict[str, Any]]:
        padding = timedelta(seconds=RCA_CORRELATION_WINDOW_SECONDS)
        start = (_parse_time(episode_start) - padding).isoformat()
        end = (_parse_time(episode_end) + padding).isoformat()
        return self._telemetry_records_between(start, end)

    def _telemetry_records_between(self, start: str, end: str) -> list[dict[str, Any]]:
        uri = f"file:{self.monitoring_database.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT id, event_id, service_id, record_type, timestamp, trace_id,
                          span_id, payload_json
                   FROM telemetry_records
                   WHERE timestamp >= ? AND timestamp <= ?
                   ORDER BY timestamp, id LIMIT 10000""",
                (start, end),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record.pop("payload_json"))
            records.append(record)
        return records

    def _evaluate_scenario(self, scenario_id: str) -> dict[str, Any]:
        expected_label = EXPECTED_SCENARIO_LABELS[scenario_id]
        self._wait_for_next_minute_window()
        baseline_method, baseline_path, baseline_body = self._baseline_request(scenario_id)
        baseline_results = [
            self._request_gateway(baseline_method, baseline_path, baseline_body)
            for _ in range(5)
        ]
        if any(item["status_code"] < 200 or item["status_code"] >= 300 for item in baseline_results):
            return {"scenario_id": scenario_id, "status": "incomplete", "reason": "baseline requests failed", "baseline_http": baseline_results}

        # Require actual baseline records to reach monitoring before activating a fault.
        baseline_ready = self._eventually(
            "baseline telemetry",
            lambda: sum(
                item["type"] == "metric" and item["metric_name"] == "http.server.request"
                and item["service_id"] == "api-gateway" and item["method"] == baseline_method
                and item["route"] in {"/api/users/{user_id}", "/api/orders"}
                and item["processing_status"] in {"pending", "processed"}
                for item in self._telemetry()
            ) >= 5,
            timeout=12,
        )
        if not baseline_ready:
            return {"scenario_id": scenario_id, "status": "incomplete", "reason": "baseline telemetry missing", "baseline_http": baseline_results}

        status, started = _request_json(
            self.controller_url + f"/scenarios/{scenario_id}/start",
            "POST", {"duration_seconds": 25},
        )
        if status != 200 or not started.get("run_id"):
            return {"scenario_id": scenario_id, "status": "incomplete", "reason": f"controller start failed: HTTP {status}", "controller_response": started, "baseline_http": baseline_results}
        run_id = started["run_id"]
        self.run_ids.append((scenario_id, run_id))
        run_start = _parse_time(started["started_at"])
        activation_verified = (
            started.get("status") == "active"
            and bool(started.get("active_leases"))
            and all(lease.get("active") is True for lease in started["active_leases"])
        )

        method, path, body, request_count = SCENARIO_REQUESTS[scenario_id]
        http_results = []
        try:
            for _ in range(request_count):
                http_results.append(self._request_gateway(method, path, body))
        finally:
            stop_status, stop_result = _request_json(
                self.controller_url + f"/scenarios/{scenario_id}/stop?run_id={run_id}",
                "POST", timeout=4.0,
            )

        run_end = datetime.now(timezone.utc)
        fault_stopped_at = run_end.isoformat()
        # Controller cleanup is part of the measured result. Don't move on until
        # the control plane confirms no active lease remains.
        if stop_status != 200 or stop_result.get("status") != "stopped":
            return {
                "scenario_id": scenario_id, "status": "incomplete", "reason": "fault lease did not stop explicitly",
                "controller_response": stop_result, "http_results": http_results,
                "baseline_http": baseline_results, "run_id": run_id,
            }

        expected_status = 200 if scenario_id == "api_latency_injection" else 503
        fault_behavior_verified = (
            activation_verified
            and len(http_results) == request_count
            and all(item["status_code"] == expected_status for item in http_results)
            and (
                scenario_id != "api_latency_injection"
                or all(item["duration_ms"] >= 1_000 for item in http_results)
            )
        )

        scenario_records = self._eventually(
            "scenario telemetry",
            lambda: [item for item in self._telemetry()
                     if run_start - timedelta(seconds=1) <= _parse_time(item["timestamp"]) <= run_end + timedelta(seconds=2)],
            timeout=15,
        )
        if not scenario_records:
            return {"scenario_id": scenario_id, "status": "incomplete", "reason": "scenario telemetry missing", "http_results": http_results, "run_id": run_id}

        # An anomaly window is eligible only after its UTC minute closes and the
        # configured lateness allowance has passed.
        event_times = [_parse_time(item["timestamp"]) for item in scenario_records]
        last_event = max(event_times)
        window_end_ready = last_event.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=11)
        remaining = (window_end_ready - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            if remaining > WINDOW_PROCESSING_TIMEOUT_SECONDS:
                return {"scenario_id": scenario_id, "status": "incomplete", "reason": "window wait exceeded bounded timeout", "run_id": run_id}
            time.sleep(remaining)

        scenario_start_window = run_start.replace(second=0, microsecond=0)
        scenario_end_window = last_event.replace(second=0, microsecond=0)
        metric_records = [
            item for item in scenario_records
            if item.get("type") == "metric" and item.get("metric_name") == "http.server.request"
        ]
        observed_service_ids = sorted({item["service_id"] for item in metric_records})
        expected_detection_keys = {
            (item["service_id"], _parse_time(item["timestamp"]).replace(second=0, microsecond=0).isoformat())
            for item in metric_records
        }
        # Confirm all observed scenario records were handled by the aggregation worker.
        def processed_scenario_records():
            current = [item for item in self._telemetry()
                       if run_start - timedelta(seconds=1) <= _parse_time(item["timestamp"]) <= run_end + timedelta(seconds=2)]
            if not current or any(item["processing_status"] != "processed" for item in current):
                return None
            return current

        processing = self._eventually(
            "telemetry processing", processed_scenario_records, timeout=20,
        )
        pending_count = sum(item["processing_status"] != "processed" for item in processing or [])
        if pending_count or not processing:
            return {"scenario_id": scenario_id, "status": "incomplete", "reason": "telemetry window processing incomplete", "pending_records": pending_count, "http_results": http_results, "run_id": run_id}

        def completed_scenario_detections():
            rows = self._detection_rows(scenario_start_window, scenario_end_window)
            matching = [row for row in rows if row["service_id"] in observed_service_ids]
            completed_keys = {
                (row["service_id"], row["window_start"]) for row in matching
            }
            return matching if expected_detection_keys.issubset(completed_keys) else None

        scenario_detections = self._eventually(
            "anomaly scoring", completed_scenario_detections, timeout=20,
        ) or []
        completed_detection_keys = {
            (row["service_id"], row["window_start"]) for row in scenario_detections
        }
        detected_api_status, anomaly_response = _request_json(self.monitor_url + "/api/anomalies/recent?limit=500")
        if detected_api_status != 200:
            return {"scenario_id": scenario_id, "status": "incomplete", "reason": "anomaly API unavailable", "http_results": http_results, "run_id": run_id}
        anomaly_items = [
            item for item in anomaly_response.get("items", [])
            if scenario_start_window.isoformat() <= item.get("window_start", "") <= scenario_end_window.isoformat()
            and item.get("service_id") in observed_service_ids
        ]
        anomaly_ids = {item["anomaly_id"] for item in anomaly_items}

        rca_status, rca_response = _request_json(self.monitor_url + "/api/rca/recent?limit=100")
        if rca_status != 200:
            return {"scenario_id": scenario_id, "status": "incomplete", "reason": "RCA API unavailable", "http_results": http_results, "run_id": run_id}
        matching_response_bodies = []

        def matching_investigations():
            current_status, current_response = _request_json(self.monitor_url + "/api/rca/recent?limit=100")
            matching_response_bodies.append(current_response)
            if current_status != 200:
                return None
            matches = [item for item in current_response.get("items", [])
                       if anomaly_ids.intersection(item.get("triggering_anomaly_ids", []))]
            return matches if matches or not anomaly_ids else None

        investigations = self._eventually(
            "RCA processing", matching_investigations, timeout=20,
        ) or []
        investigation_details = []
        detail_api_responses = []
        for item in investigations:
            detail_status, detail = _request_json(
                self.monitor_url + f"/api/rca/{item['investigation_id']}"
            )
            candidate_status, candidates = _request_json(
                self.monitor_url + f"/api/rca/{item['investigation_id']}/candidates"
            )
            detail_api_responses.append({"detail": detail, "candidates": candidates})
            if detail_status == 200 and candidate_status == 200:
                investigation_details.append({
                    "status": detail.get("status"),
                    "investigation_id": item["investigation_id"],
                    "episode_start": detail.get("episode_start"),
                    "episode_end": detail.get("episode_end"),
                    "triggering_anomaly_ids": detail.get("triggering_anomaly_ids", []),
                    "root_causes": detail.get("root_causes", []),
                    "ranked_candidates": candidates,
                    "evidence_summary": detail.get("evidence_summary"),
                })
        rca_processing_complete = (
            not anomaly_items
            or (bool(investigations) and len(investigation_details) == len(investigations))
        )

        recovery_health = {}
        recovery_health_responses = []
        recovery_started_at = fault_stopped_at
        for service_id in ("api-gateway", "user-service", "order-service", "database-service"):
            health_status, health = _request_json(
                f"http://127.0.0.1:{self.ports[service_id]}/health"
            )
            recovery_health[service_id] = {"status_code": health_status, "status": health.get("status")}
            recovery_health_responses.append(health)
        recovery_request = self._request_gateway("GET", "/api/users/user-1001")
        recovery_ok = (
            all(value["status_code"] == 200 and value["status"] == "ok" for value in recovery_health.values())
            and recovery_request["status_code"] == 200
        )
        recovery_completed_at = datetime.now(timezone.utc).isoformat()

        rca_input_records = []
        rca_inputs_complete = True
        if anomaly_items and not investigation_details:
            rca_inputs_complete = False
        for investigation in investigation_details:
            episode_start = investigation.get("episode_start")
            episode_end = investigation.get("episode_end")
            if not isinstance(episode_start, str) or not isinstance(episode_end, str):
                rca_inputs_complete = False
                continue
            rca_input_records.extend(self._rca_telemetry_inputs(episode_start, episode_end))
        rca_input_ids = {
            anomaly_id
            for investigation in investigation_details
            for anomaly_id in investigation.get("triggering_anomaly_ids", [])
        }
        observed_anomaly_ids = {
            item.get("anomaly_id") for item in anomaly_items if item.get("anomaly_id") is not None
        }
        if not rca_input_ids.issubset(observed_anomaly_ids):
            rca_inputs_complete = False
        rca_inputs = None if not rca_inputs_complete else {
            "anomalies": [
                item for item in anomaly_items if item.get("anomaly_id") in rca_input_ids
            ],
            "telemetry_records": rca_input_records,
        }
        process_log_paths = {
            process.phase8_label: process.phase8_log_path
            for process in self.processes
            if process.phase8_label != "fault-simulator"
        }
        process_logs = None
        if (
            "monitoring-backend" in process_log_paths
            and set(SERVICE_MODULES).issubset(process_log_paths)
            and all(path.exists() for path in process_log_paths.values())
        ):
            process_logs = {
                process_label: path.read_text(encoding="utf-8", errors="replace")
                for process_label, path in process_log_paths.items()
            }
        controller_log = self.temp_path / "fault-simulator.log"
        controller_log_text = (
            controller_log.read_text(encoding="utf-8", errors="replace")
            if controller_log.exists() else None
        )
        api_response_bodies = {
            "baseline_business_responses": [item.get("response") for item in baseline_results],
            "fault_business_responses": [item.get("response") for item in http_results],
            "recovery_business_response": recovery_request.get("response"),
            "fault_simulator_start_response": started,
            "fault_simulator_stop_response": stop_result,
            "anomalies_api_response": anomaly_response,
            "rca_recent_api_response": rca_response,
            "matching_rca_api_responses": matching_response_bodies,
            "rca_detail_and_candidate_responses": detail_api_responses,
            "service_health_api_responses": recovery_health_responses,
            "telemetry_api_response_body": scenario_records,
            "scenario_catalog_api_response": self.scenario_catalog_response,
            "model_status_api_response": self.model_status,
        }
        full_telemetry_records = self._telemetry_records_between(
            (run_start - timedelta(seconds=1)).isoformat(),
            (run_end + timedelta(seconds=2)).isoformat(),
        )
        expected_label_leak_checks = check_expected_label_leaks(expected_label, {
            "service_process_stdout_stderr": process_logs,
            "fault_simulator_process_log": controller_log_text,
            "captured_api_response_bodies": api_response_bodies,
            "telemetry_and_trace_records": full_telemetry_records,
            "rca_inputs": rca_inputs,
            "rca_results": investigation_details,
        })

        # Preserve the existing scenario-ID check independently of the post-hoc label check.
        telemetry_text = json.dumps(full_telemetry_records, sort_keys=True)
        label_leaked = (
            scenario_id in telemetry_text
            or expected_label_leak_checks["telemetry_and_trace_records"] is True
        )
        output = {
            "scenario_id": scenario_id,
            "status": "complete",
            "run_id": run_id,
            "model_ready": bool(self.model_status.get("ready")),
            "model_version": self.model_status.get("model_version"),
            "baseline_http": baseline_results,
            "http_results": http_results,
            "fault_activation_verified": activation_verified,
            "fault_behavior_verified": fault_behavior_verified,
            "fault_stopped_at": fault_stopped_at,
            "affected_service_ids_observed": observed_service_ids,
            "telemetry_received": True,
            "telemetry_records": len(scenario_records),
            "telemetry_processed": True,
            "observed_error_rates_and_latency": self._observed_service_metrics(metric_records),
            "trace_and_dependency_evidence": self._trace_dependency_evidence(scenario_records),
            "isolation_forest_scores_and_decisions": [
                {"service_id": row["service_id"], "window_start": row["window_start"],
                 "score": row["model_score"], "decision": row["model_decision"],
                 "model_version": row["model_version"]}
                for row in scenario_detections
            ],
            "threshold_rule_matches": [
                {"service_id": row["service_id"], "window_start": row["window_start"],
                 "matches": row["threshold_matches"]}
                for row in scenario_detections
            ],
            "anomaly_created": bool(anomaly_items),
            "anomalies": anomaly_items,
            "rca_triggered": bool(investigations),
            "rca_processing_complete": rca_processing_complete,
            "rca_investigations": investigation_details,
            "recovery_health": recovery_health,
            "recovery_http": recovery_request,
            "recovery_started_at": recovery_started_at,
            "recovery_completed_at": recovery_completed_at,
            "recovery_succeeded": recovery_ok,
            "scenario_label_leaked_into_telemetry": label_leaked,
            "expected_label_leak_checks": expected_label_leak_checks,
        }
        if not fault_behavior_verified:
            output["status"] = "incomplete"
            output["reason"] = "Observed request results did not confirm the selected bounded fault behavior"
        elif completed_detection_keys != expected_detection_keys:
            output["status"] = "incomplete"
            output["reason"] = "One or more observed service windows lack completed anomaly scoring rows"
        elif any(row["model_score"] is None for row in scenario_detections):
            output["status"] = "incomplete"
            output["reason"] = "Isolation Forest score unavailable for one or more scored windows"
        elif not rca_processing_complete:
            output["status"] = "incomplete"
            output["reason"] = "Anomaly records did not complete RCA processing"
        elif label_leaked:
            output["status"] = "incomplete"
            output["reason"] = "Expected scenario label was found in telemetry"
        elif not recovery_ok:
            output["status"] = "incomplete"
            output["reason"] = "Service health or business request did not recover"
        elif any(value is None for value in expected_label_leak_checks.values()):
            output["status"] = "incomplete"
            output["reason"] = "One or more expected-label leakage checks were unavailable"
        elif any(expected_label_leak_checks.values()):
            output["status"] = "incomplete"
            output["reason"] = "An expected scenario label was found in an observed input or output"
        scenario_id_in_controller_log = (
            controller_log_text is not None and scenario_id in controller_log_text
        )
        output["scenario_label_in_controller_log"] = scenario_id_in_controller_log
        if scenario_id_in_controller_log:
            output["status"] = "incomplete"
            output["reason"] = "Scenario identifier was found in controller process logs"
        return output

    @staticmethod
    def _observed_service_metrics(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in records:
            grouped.setdefault(item["service_id"], []).append(item)
        summary = []
        for service_id, items in sorted(grouped.items()):
            count = len(items)
            failed = sum(int((item.get("status_code") or 0) >= 400 or item.get("timed_out")) for item in items)
            durations = sorted(float(item.get("duration_ms") or 0) for item in items)
            p95_index = max(0, min(count - 1, int(0.95 * count + 0.999999) - 1))
            summary.append({
                "service_id": service_id,
                "request_count": count,
                "error_count": failed,
                "error_rate": failed / count if count else 0,
                "average_latency_ms": sum(durations) / count if count else 0,
                "p95_latency_ms": durations[p95_index] if durations else None,
            })
        return summary

    @staticmethod
    def _trace_dependency_evidence(records: list[dict[str, Any]]) -> dict[str, Any]:
        traces = [item for item in records if item.get("type") == "trace"]
        metrics = [item for item in records if item.get("type") == "metric"]
        return {
            "trace_count": len(traces),
            "trace_ids": sorted({item["trace_id"] for item in traces if item.get("trace_id")}),
            "dependency_metrics": [
                {"service_id": item["service_id"], "dependency": item.get("dependency"),
                 "status_code": item.get("status_code"), "trace_id": item.get("trace_id")}
                for item in metrics if item.get("dependency")
            ],
        }

    def test_all_fault_scenarios_end_to_end_with_isolated_state(self) -> None:
        self.assertTrue(self.model_status["ready"])
        self.assertEqual(self.application_database.parent, self.application_temp_path)
        self.assertTrue(self.application_temp_path.is_relative_to(PROJECT_ROOT / "data" / "simulated"))
        self.assertEqual(self.monitoring_database.parent, self.temp_path / "monitoring")
        deadline = time.monotonic() + TOTAL_EVALUATION_DEADLINE_SECONDS

        for scenario_id in SCENARIO_IDS:
            with self.subTest(scenario=scenario_id):
                if time.monotonic() >= deadline:
                    result = {"scenario_id": scenario_id, "status": "incomplete", "reason": "overall harness deadline exceeded"}
                else:
                    result = self._evaluate_scenario(scenario_id)
                self.scenario_results.append(result)
                self.assertEqual(
                    result["status"], "complete",
                    f"Evaluation incomplete for {scenario_id}: {json.dumps(result, sort_keys=True)}",
                )
                self.assertTrue(result["telemetry_received"])
                self.assertTrue(result["telemetry_processed"])
                self.assertTrue(result["recovery_succeeded"])


if __name__ == "__main__":
    unittest.main()
