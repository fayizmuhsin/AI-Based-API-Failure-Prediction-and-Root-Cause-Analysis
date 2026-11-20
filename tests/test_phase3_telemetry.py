import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_MODULES = {
    "database-service": "services.database.main",
    "user-service": "services.user.main",
    "order-service": "services.order.main",
    "api-gateway": "services.gateway.main",
}


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def json_request(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=4) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class Phase3TelemetryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        simulated_dir = PROJECT_ROOT / "data" / "simulated"
        simulated_dir.mkdir(parents=True, exist_ok=True)
        cls.temp_dir = tempfile.TemporaryDirectory(prefix=".phase3-test-", dir=simulated_dir)
        cls.database_path = Path(cls.temp_dir.name) / "application-test.db"
        cls.monitoring_database = Path(cls.temp_dir.name) / "monitoring-test.db"
        cls.monitor_port = unused_port()
        cls.ports = {service_id: unused_port() for service_id in SERVICE_MODULES}
        cls.monitor_process: subprocess.Popen | None = None
        cls.service_processes: list[subprocess.Popen] = []

        env = os.environ.copy()
        env["APP_APPLICATION_DATABASE_PATH"] = str(cls.database_path)
        env["APP_MONITORING_DATABASE_PATH"] = str(cls.monitoring_database)
        env["TELEMETRY_INGEST_URL"] = f"http://127.0.0.1:{cls.monitor_port}/api/telemetry/ingest"
        for service_id, port in cls.ports.items():
            env[service_id.replace("-", "_").upper() + "_PORT"] = str(port)
        env["DATABASE_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['database-service']}"
        env["USER_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['user-service']}"
        env["ORDER_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['order-service']}"

        try:
            cls.monitor_process = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
                    "--host", "127.0.0.1", "--port", str(cls.monitor_port),
                ],
                cwd=PROJECT_ROOT,
                env=env.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            cls._wait_http(cls.monitor_process, cls.base_monitor() + "/api/health", "monitoring backend")
            # The monitoring API must not create or open the configured simulated DB.
            if cls.database_path.exists():
                raise AssertionError("Monitoring backend created the simulated application database")

            for service_id, module in SERVICE_MODULES.items():
                process = subprocess.Popen(
                    [sys.executable, "-m", module],
                    cwd=PROJECT_ROOT,
                    env=env.copy(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                cls.service_processes.append(process)
                cls._wait_http(
                    process,
                    f"http://127.0.0.1:{cls.ports[service_id]}/health",
                    service_id,
                )
        except Exception:
            cls._stop_processes()
            cls.temp_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_processes()
        cls.temp_dir.cleanup()

    @staticmethod
    def _snapshot(path: Path):
        if not path.exists():
            return None
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    @classmethod
    def base_monitor(cls) -> str:
        return f"http://127.0.0.1:{cls.monitor_port}"

    @classmethod
    def service_url(cls, service_id: str) -> str:
        return f"http://127.0.0.1:{cls.ports[service_id]}"

    @classmethod
    def _wait_http(cls, process: subprocess.Popen, url: str, label: str) -> None:
        deadline = time.monotonic() + 20
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{label} exited with code {process.returncode}")
            try:
                with urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError) as exc:
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(f"{label} did not become available: {last_error}")

    @classmethod
    def _stop_processes(cls) -> None:
        processes = list(reversed(getattr(cls, "service_processes", [])))
        if getattr(cls, "monitor_process", None) is not None:
            processes.append(cls.monitor_process)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def _eventually_recent(self, predicate, timeout: float = 8.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, response = json_request(self.base_monitor() + "/api/telemetry/recent?limit=500")
            self.assertEqual(status, 200)
            records = response["items"]
            if predicate(records):
                return records
            time.sleep(0.1)
        self.fail("Expected telemetry did not arrive at the monitoring ingestion API")

    def test_existing_health_endpoints_and_monitor_health_are_unchanged(self) -> None:
        status, result = json_request(self.base_monitor() + "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "ok")
        for service_id in SERVICE_MODULES:
            with self.subTest(service=service_id):
                status, result = json_request(self.service_url(service_id) + "/health")
                self.assertEqual(status, 200)
                self.assertEqual(result["service_id"], service_id)

    def test_phase5_read_only_anomaly_api_contracts(self) -> None:
        status, model = json_request(self.base_monitor() + "/api/anomalies/model")
        self.assertEqual(status, 200)
        self.assertFalse(model["ready"])
        self.assertEqual(model["state"], "not_ready")

        status, recent = json_request(self.base_monitor() + "/api/anomalies/recent?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(recent["count"], len(recent["items"]))
        self.assertEqual(recent["items"], [])

        status, summary = json_request(self.base_monitor() + "/api/anomalies/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["total_anomalies"], 0)
        self.assertEqual(summary["by_service"], [])

        status, missing = json_request(self.base_monitor() + "/api/anomalies/999")
        self.assertEqual(status, 404)
        self.assertIn("not found", missing["detail"].lower())

        status, invalid = json_request(self.base_monitor() + "/api/anomalies/recent?limit=0")
        self.assertEqual(status, 422)

    def test_ingestion_validates_normalizes_and_rejects_invalid_batches(self) -> None:
        payload = {
            "service_id": "API Gateway",
            "items": [{
                "type": "metric",
                "metric_name": "http.server.request",
                "timestamp": "2026-11-19T12:00:00+05:30",
                "method": "GET",
                "route": "/api/users/{user_id}",
                "status_code": 200,
                "duration_ms": 12.5,
                "timed_out": False,
                "trace_id": "a" * 32,
            }],
        }
        status, accepted = json_request(
            self.base_monitor() + "/api/telemetry/ingest", "POST", payload
        )
        self.assertEqual(status, 202)
        self.assertEqual(accepted["service_id"], "api-gateway")
        self.assertEqual(accepted["accepted"], 1)
        status, retry = json_request(
            self.base_monitor() + "/api/telemetry/ingest", "POST", payload
        )
        self.assertEqual(status, 202)
        self.assertEqual(retry["accepted"], 1)
        self.assertEqual(retry["duplicate_count"], 1)

        status, recent = json_request(
            self.base_monitor() + "/api/telemetry/recent?limit=10&service_id=api-gateway"
        )
        self.assertEqual(status, 200)
        item = next(
            record for record in recent["items"]
            if record["type"] == "metric" and record["metric_name"] == "http.server.request"
        )
        self.assertEqual(item["service_name"], "API Gateway")
        self.assertEqual(
            datetime.fromisoformat(item["timestamp"]),
            datetime(2026, 11, 19, 6, 30, tzinfo=timezone.utc),
        )

        invalid_payloads = [
            {"service_id": "unknown-service", "items": payload["items"]},
            {"service_id": "user-service", "items": [{"type": "unsupported", "timestamp": "now"}]},
            {"service_id": "user-service", "items": [{
                **payload["items"][0], "trace_id": "bad-trace-id",
            }]},
            {"service_id": "user-service", "items": [{
                **payload["items"][0], "timestamp": "2026-11-19T12:00:00",
            }]},
        ]
        for invalid in invalid_payloads:
            status, error = json_request(self.base_monitor() + "/api/telemetry/ingest", "POST", invalid)
            self.assertEqual(status, 422, error)

    def test_background_worker_processes_persisted_telemetry(self) -> None:
        payload = {
            "service_id": "user-service",
            "items": [{
                "type": "metric",
                "metric_name": "http.server.request",
                "timestamp": "2026-11-19T12:01:00+00:00",
                "method": "GET",
                "route": "/phase4-worker-check",
                "status_code": 503,
                "duration_ms": 45.0,
                "timed_out": False,
                "trace_id": "d" * 32,
            }],
        }
        status, accepted = json_request(self.base_monitor() + "/api/telemetry/ingest", "POST", payload)
        self.assertEqual(status, 202)
        self.assertEqual(accepted["accepted"], 1)

        records = self._eventually_recent(
            lambda items: any(
                item["type"] == "metric" and item["route"] == "/phase4-worker-check"
                and item["processing_status"] == "processed"
                for item in items
            )
        )
        processed = next(item for item in records if item.get("route") == "/phase4-worker-check")
        self.assertTrue(processed["event_id"])
        status, summary = json_request(self.base_monitor() + "/api/telemetry/summary?service_id=user-service")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(summary["processed_records"], 1)

    def test_all_services_emit_metrics_traces_and_structured_events(self) -> None:
        json_request(self.service_url("api-gateway") + "/api/users/user-1001")
        json_request(
            self.service_url("api-gateway") + "/api/orders",
            method="POST",
            body={"user_id": "user-1002", "item": "Telemetry demo", "quantity": 1},
        )

        records = self._eventually_recent(
            lambda items: {item["service_id"] for item in items}
            >= {"api-gateway", "user-service", "order-service", "database-service"}
            and {item["type"] for item in items} >= {"metric", "trace", "log"}
        )
        by_service = {item["service_id"] for item in records}
        self.assertTrue({"api-gateway", "user-service", "order-service", "database-service"} <= by_service)
        self.assertTrue({"metric", "trace", "log"} <= {item["type"] for item in records})

        metric = next(item for item in records if item["type"] == "metric" and item["metric_name"] == "http.server.request")
        self.assertIn("route", metric)
        self.assertIn("status_code", metric)
        self.assertGreaterEqual(metric["duration_ms"], 0)
        structured_log = next(item for item in records if item["type"] == "log")
        for field in ("timestamp", "service_name", "level", "event_type", "method", "route", "status_code", "duration_ms"):
            self.assertIn(field, structured_log)

        status, summary = json_request(self.base_monitor() + "/api/telemetry/summary")
        self.assertEqual(status, 200)
        self.assertTrue(any(metric["p50_latency_ms"] >= 0 and metric["p95_latency_ms"] >= 0 for metric in summary["metrics"]))

    def test_http_errors_and_cross_service_trace_context_are_recorded(self) -> None:
        status, _ = json_request(self.service_url("api-gateway") + "/api/users/absent-user")
        self.assertEqual(status, 404)
        records = self._eventually_recent(
            lambda items: any(
                item["type"] == "log" and item["service_id"] == "api-gateway"
                and item.get("status_code") == 404
                for item in items
            )
        )
        metric = next(
            item for item in records
            if item["type"] == "metric" and item["status_code"] == 404
        )
        self.assertEqual(metric["status_code"], 404)

        json_request(self.service_url("api-gateway") + "/api/users/user-1001")

        def has_propagated_trace(items):
            root = next((
                item for item in items
                if item["type"] == "metric" and item["service_id"] == "api-gateway"
                and item["metric_name"] == "http.server.request"
                and item["route"] == "/api/users/{user_id}" and item["status_code"] == 200
                and item.get("trace_id")
            ), None)
            if root is None:
                return False
            services = {
                item["service_id"] for item in items
                if item["type"] == "trace" and item["trace_id"] == root["trace_id"]
            }
            return {"api-gateway", "user-service", "database-service"} <= services

        records = self._eventually_recent(has_propagated_trace)
        root_metric = next(
            item for item in records
            if item["type"] == "metric" and item["service_id"] == "api-gateway"
            and item["metric_name"] == "http.server.request"
            and item["route"] == "/api/users/{user_id}" and item["status_code"] == 200
            and item.get("trace_id")
        )
        trace_spans = [item for item in records if item["type"] == "trace" and item["trace_id"] == root_metric["trace_id"]]
        self.assertTrue({"api-gateway", "user-service", "database-service"} <= {item["service_id"] for item in trace_spans})
        self.assertTrue(any(item["parent_span_id"] for item in trace_spans))

    def test_ingestion_does_not_generate_recursive_telemetry_and_uses_separate_database(self) -> None:
        # Service exporters can still be flushing spans from the preceding test.
        # Establish a quiet baseline before checking that monitor requests add nothing.
        simulated_database_snapshot = self._snapshot(self.database_path)
        deadline = time.monotonic() + 5.0
        previous_count = None
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            _, current = json_request(self.base_monitor() + "/api/telemetry/recent?limit=500")
            count = current["stored_records"]
            if count != previous_count:
                previous_count = count
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 0.75:
                break
            time.sleep(0.1)
        else:
            self.fail("Service telemetry did not settle before recursion check")

        _, before = json_request(self.base_monitor() + "/api/telemetry/recent?limit=500")
        _, health = json_request(self.base_monitor() + "/api/health")
        self.assertEqual(health["status"], "ok")
        _, after = json_request(self.base_monitor() + "/api/telemetry/recent?limit=500")
        self.assertEqual(before["stored_records"], after["stored_records"])
        self.assertFalse(any(item["service_id"] == "monitoring-platform" for item in after["items"]))
        self.assertNotEqual(self.database_path.resolve(), self.monitoring_database.resolve())
        self.assertTrue(self.monitoring_database.exists())
        self.assertEqual(self._snapshot(self.database_path), simulated_database_snapshot)

    def test_failed_telemetry_delivery_does_not_break_service_requests(self) -> None:
        port = unused_port()
        ingest_port = unused_port()
        env = os.environ.copy()
        env["APP_APPLICATION_DATABASE_PATH"] = str(self.database_path)
        env["APP_MONITORING_DATABASE_PATH"] = str(self.monitoring_database)
        env["API_GATEWAY_PORT"] = str(port)
        env["TELEMETRY_INGEST_URL"] = f"http://127.0.0.1:{ingest_port}/api/telemetry/ingest"
        failed_delivery = subprocess.Popen(
            [sys.executable, "-m", "services.gateway.main"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self._wait_http(failed_delivery, f"http://127.0.0.1:{port}/health", "offline-export gateway")
            started = time.perf_counter()
            for _ in range(3):
                status, result = json_request(f"http://127.0.0.1:{port}/health")
                self.assertEqual(status, 200)
                self.assertEqual(result["status"], "ok")
            self.assertLess(time.perf_counter() - started, 1.0)
            self.assertIsNone(failed_delivery.poll())
        finally:
            failed_delivery.terminate()
            try:
                failed_delivery.wait(timeout=5)
            except subprocess.TimeoutExpired:
                failed_delivery.kill()
                failed_delivery.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
