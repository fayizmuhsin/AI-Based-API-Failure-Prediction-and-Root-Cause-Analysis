from __future__ import annotations

import tempfile
import unittest
import os
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.anomaly.repository import AnomalyRepository
from app.api.routes import rca as rca_routes
from app.api.routes.rca import CandidateResponse, InvestigationDetailResponse
from app.core.config import Settings
from app.rca.engine import RootCauseAnalysisEngine
from app.rca.repository import RcaRepository, RcaStorageError
from app.telemetry.storage import TelemetryRepository


class Phase6RcaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.monitoring_path = root / "monitoring" / "monitor.db"
        self.application_path = root / "simulated" / "application.db"
        self.settings = Settings(
            monitoring_database_path=self.monitoring_path,
            application_database_path=self.application_path,
            rca_correlation_window_seconds=180,
            rca_candidate_min_score=30,
        )
        self.telemetry = TelemetryRepository(self.monitoring_path, self.application_path)
        self.telemetry.initialize()
        self.anomalies = AnomalyRepository(self.monitoring_path, self.application_path)
        self.anomalies.initialize()
        self.repository = RcaRepository(self.monitoring_path, self.application_path)
        self.repository.initialize()
        self.engine = RootCauseAnalysisEngine(self.repository, self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def ts(minute: int = 0, second: int = 0) -> str:
        return datetime(2026, 5, 1, 12, minute, second, tzinfo=timezone.utc).isoformat()

    def add_metric(
        self, service: str, timestamp: str, *, metric: str = "http.server.request",
        status: int = 200, duration: float = 25, timeout: bool = False,
        dependency: str | None = None, trace_id: str | None = None,
    ) -> None:
        self.telemetry.insert_batch(service, [{
            "type": "metric", "metric_name": metric, "timestamp": timestamp,
            "method": "GET", "route": "/api/test", "status_code": status,
            "duration_ms": duration, "timed_out": timeout,
            "dependency": dependency, "trace_id": trace_id,
        }])

    def add_log(self, service: str, timestamp: str, event_type: str, level: str = "ERROR") -> None:
        self.telemetry.insert_batch(service, [{
            "type": "log", "timestamp": timestamp, "level": level,
            "event_type": event_type, "trace_id": None, "method": None,
            "route": None, "status_code": None, "duration_ms": None,
            "error_details": "observed test failure", "dependency": None,
        }])

    def add_anomaly(self, service: str, minute: int = 0, severity: str = "high") -> int:
        start = datetime(2026, 5, 1, 12, minute, tzinfo=timezone.utc).isoformat()
        inserted, anomaly_id = self.anomalies.record_detection(
            service_id=service,
            window_start=start,
            model_version=None,
            model_score=0.82,
            model_decision="anomaly",
            threshold_matches=[{"rule": "observed_failure", "observed": 1, "threshold": 0.25}],
            feature_values={"request_count": 10, "error_rate": 0.9},
            is_anomaly=True,
            severity=severity,
        )
        self.assertTrue(inserted)
        return anomaly_id

    def run_report(self) -> dict:
        self.assertEqual(self.engine.process_batch(), 1)
        items = self.repository.recent_investigations()
        self.assertEqual(len(items), 1)
        return self.repository.investigation_detail(items[0]["investigation_id"])

    def test_database_failure_ranks_dependency_and_finds_upstream_callers(self) -> None:
        self.add_anomaly("database-service")
        self.add_anomaly("user-service")
        self.add_anomaly("order-service")
        self.add_anomaly("api-gateway")
        for caller, dependency in (
            ("user-service", "database-service"),
            ("order-service", "database-service"),
            ("api-gateway", "user-service"),
            ("api-gateway", "order-service"),
        ):
            self.add_metric(caller, self.ts(second=1), metric="http.client.request",
                            status=503, timeout=True, dependency=dependency)
        self.add_metric("database-service", self.ts(second=2), status=503)
        self.add_log("database-service", self.ts(second=3), "database_connection_failure")
        report = self.run_report()
        self.assertEqual(report["root_causes"][0]["service_id"], "database-service")
        self.assertEqual(
            set(report["root_causes"][0]["affected_services"]),
            {"database-service", "user-service", "order-service", "api-gateway"},
        )
        self.assertGreaterEqual(len(report["root_causes"][0]["evidence"]), 3)
        self.assertIn("reverse edges", report["methodology"]["dependency_direction"])

    def _assert_direct_service_failure(self, service: str) -> None:
        self.add_anomaly(service)
        for second in range(1, 7):
            self.add_metric(service, self.ts(second=second), status=503, timeout=second == 6)
        report = self.run_report()
        self.assertEqual(report["root_causes"][0]["service_id"], service)
        self.assertIn("server_metrics", report["root_causes"][0]["supporting_signals"])

    def test_user_service_failure_supported_by_observed_metrics(self) -> None:
        self._assert_direct_service_failure("user-service")

    def test_order_service_failure_supported_by_observed_metrics(self) -> None:
        self._assert_direct_service_failure("order-service")

    def test_gateway_failure_supported_by_observed_metrics(self) -> None:
        self._assert_direct_service_failure("api-gateway")

    def test_multiple_simultaneous_anomalies_form_one_investigation(self) -> None:
        anomaly_ids = [self.add_anomaly(service) for service in ("user-service", "order-service", "api-gateway")]
        self.add_metric("api-gateway", self.ts(second=1), status=503)
        report = self.run_report()
        self.assertEqual(set(report["triggering_anomaly_ids"]), set(anomaly_ids))
        self.assertEqual(report["trigger_count"], 3)

    def test_missing_telemetry_produces_inconclusive_report_without_probable_cause(self) -> None:
        self.add_anomaly("order-service", severity="low")
        report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["root_causes"], [])
        self.assertFalse(any(item["selected_as_probable_root_cause"] for item in report["candidates"]))

    def test_observed_logs_traces_health_restart_and_deployment_are_collected(self) -> None:
        self.add_anomaly("user-service")
        self.add_log("user-service", self.ts(second=1), "health_check_failed")
        self.add_log("user-service", self.ts(second=2), "service_restart", level="INFO")
        self.add_log("user-service", self.ts(second=3), "deployment_started", level="INFO")
        trace_id = "a" * 32
        self.telemetry.insert_batch("user-service", [{
            "type": "trace", "timestamp": self.ts(second=4),
            "end_time": self.ts(second=5), "trace_id": trace_id,
            "span_id": "1" * 16, "parent_span_id": None, "name": "database query",
            "kind": "CLIENT", "status": "ERROR", "status_description": "connection refused",
            "attributes": {"peer.service": "Database Service"}, "events": [],
        }])
        self.telemetry.insert_batch("database-service", [{
            "type": "trace", "timestamp": self.ts(second=5),
            "end_time": self.ts(second=6), "trace_id": trace_id,
            "span_id": "2" * 16, "parent_span_id": "1" * 16, "name": "query",
            "kind": "SERVER", "status": "ERROR", "status_description": "failed",
            "attributes": {}, "events": [],
        }])
        report = self.run_report()
        self.assertGreaterEqual(report["evidence_summary"]["correlated_trace_groups"], 1)
        self.assertTrue(any(
            candidate["service_id"] == "database-service"
            and "trace_dependency_failure" in candidate["supporting_signals"]
            for candidate in report["candidates"]
        ))
        user_candidate = next(item for item in report["candidates"] if item["service_id"] == "user-service")
        self.assertIn("health_event", user_candidate["supporting_signals"])
        self.assertIn("restart_marker", user_candidate["supporting_signals"])
        self.assertIn("deployment_marker", user_candidate["supporting_signals"])

    def test_simulator_expected_labels_cannot_change_candidate_ranking(self) -> None:
        anomaly_id = self.add_anomaly("order-service")
        self.add_metric("order-service", self.ts(second=1), status=503)
        self.repository.enqueue_new_anomalies()
        anomaly = self.repository.pending_anomalies(1)[0]
        anomaly["expected_root_cause"] = "database-service"
        report_with_label = self.engine.investigate([anomaly])
        anomaly["expected_root_cause"] = "api-gateway"
        report_with_other_label = self.engine.investigate([anomaly])
        self.assertEqual(
            [(item["service_id"], item["rank_score"]) for item in report_with_label["candidates"]],
            [(item["service_id"], item["rank_score"]) for item in report_with_other_label["candidates"]],
        )
        self.assertEqual(report_with_label["triggering_anomaly_ids"], [anomaly_id])
        self.assertFalse(report_with_label["evidence_summary"]["expected_simulator_metadata_used"])

    def test_reprocessing_does_not_duplicate_investigation_or_alert(self) -> None:
        self.add_anomaly("database-service")
        self.add_metric("database-service", self.ts(second=1), status=503)
        self.assertEqual(self.engine.process_batch(), 1)
        self.assertEqual(self.engine.process_batch(), 0)
        self.assertEqual(len(self.repository.recent_investigations()), 1)
        self.assertEqual(self.repository.investigation_summary()["total_investigations"], 1)

    def test_transaction_rollback_preserves_pending_queue_for_retry(self) -> None:
        anomaly_id = self.add_anomaly("order-service")
        self.add_metric("order-service", self.ts(second=1), status=503)
        self.repository.enqueue_new_anomalies()
        report = self.engine.investigate(self.repository.pending_anomalies(10))
        import sqlite3
        connection = sqlite3.connect(self.monitoring_path)
        connection.execute(
            "CREATE TRIGGER fail_rca_insert BEFORE INSERT ON investigations "
            "BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END"
        )
        connection.close()
        with self.assertRaises(RcaStorageError):
            self.repository.persist_investigation(report, [anomaly_id])
        self.assertEqual(self.repository.recent_investigations(), [])
        self.assertEqual(self.repository.pending_anomalies(10)[0]["attempts"], 0)
        connection = sqlite3.connect(self.monitoring_path)
        connection.execute("DROP TRIGGER fail_rca_insert")
        connection.close()
        self.assertEqual(self.engine.process_batch(), 1)
        self.assertEqual(len(self.repository.recent_investigations()), 1)

    def test_retry_attempt_and_restart_recovery(self) -> None:
        anomaly_id = self.add_anomaly("user-service")
        self.add_metric("user-service", self.ts(second=1), status=503)
        self.repository.enqueue_new_anomalies()
        self.repository.record_attempt_failure([anomaly_id], "temporary telemetry read error")
        restarted_repository = RcaRepository(self.monitoring_path, self.application_path)
        restarted_engine = RootCauseAnalysisEngine(restarted_repository, self.settings)
        self.assertEqual(restarted_engine.process_batch(), 1)
        self.assertEqual(restarted_repository.investigation_summary()["retrying_anomalies"], 0)
        self.assertEqual(len(restarted_repository.recent_investigations()), 1)

    def test_topology_and_rca_storage_never_open_simulated_database(self) -> None:
        self.add_anomaly("database-service")
        self.assertFalse(self.application_path.exists())
        self.engine.process_batch()
        self.assertTrue(self.monitoring_path.exists())
        self.assertFalse(self.application_path.exists())
        self.assertEqual(
            set(self.engine.topology.affected_upstream_callers("database-service")),
            {"user-service", "order-service", "api-gateway"},
        )

    def test_api_lists_details_candidates_summary_and_not_found(self) -> None:
        self.add_anomaly("api-gateway")
        self.add_metric("api-gateway", self.ts(second=1), status=503)
        self.engine.process_batch()
        paths = {route.path for route in rca_routes.router.routes}
        self.assertEqual(paths, {"/rca/recent", "/rca/summary", "/rca/{investigation_id}/candidates",
                                "/rca/{investigation_id}"})
        with patch.object(rca_routes, "rca_repository", self.repository):
            recent = rca_routes.recent_investigations(limit=50)
            investigation_id = recent["items"][0]["investigation_id"]
            detail = rca_routes.investigation_detail(investigation_id)
            candidates = rca_routes.investigation_candidates(investigation_id)
            summary = rca_routes.investigation_summary()
            with self.assertRaises(HTTPException) as missing:
                rca_routes.investigation_detail("00000000-0000-0000-0000-000000000000")
        self.assertEqual(detail["trigger_count"], 1)
        self.assertTrue(candidates)
        self.assertEqual(summary["total_investigations"], 1)
        self.assertEqual(missing.exception.status_code, 404)
        InvestigationDetailResponse.model_validate(detail)
        [CandidateResponse.model_validate(item) for item in candidates]

    def test_local_backend_starts_with_rca_routes_and_isolated_database_paths(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = os.environ.copy()
        env["APP_MONITORING_DATABASE_PATH"] = str(self.monitoring_path)
        env["APP_APPLICATION_DATABASE_PATH"] = str(self.application_path)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(f"Monitoring backend exited with code {process.returncode}")
                try:
                    with urllib.request.urlopen(base + "/api/health", timeout=1) as response:
                        self.assertEqual(response.status, 200)
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            else:
                self.fail("Monitoring backend did not become ready")
            with urllib.request.urlopen(base + "/api/rca/summary", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["total_investigations"], 0)
            with urllib.request.urlopen(base + "/api/rca/recent", timeout=3) as response:
                self.assertEqual(response.status, 200)
            start = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(second=0, microsecond=0)
            items = [{
                "type": "metric", "metric_name": "http.server.request",
                "timestamp": (start + timedelta(seconds=second)).isoformat(),
                "method": "GET", "route": "/api/test", "status_code": 503,
                "duration_ms": 45, "timed_out": False, "dependency": None,
            } for second in range(1, 8)]
            body = json.dumps({"service_id": "api-gateway", "items": items}).encode("utf-8")
            request = urllib.request.Request(
                base + "/api/telemetry/ingest", data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 202)
            deadline = time.monotonic() + 10
            detail = None
            while time.monotonic() < deadline:
                with urllib.request.urlopen(base + "/api/rca/recent?limit=5", timeout=3) as response:
                    recent = json.loads(response.read())
                if recent["items"]:
                    investigation_id = recent["items"][0]["investigation_id"]
                    with urllib.request.urlopen(base + f"/api/rca/{investigation_id}", timeout=3) as response:
                        detail = json.loads(response.read())
                    break
                time.sleep(0.1)
            self.assertIsNotNone(detail, "Telemetry -> anomaly detection -> RCA did not complete")
            self.assertEqual(detail["root_causes"][0]["service_id"], "api-gateway")
            with urllib.request.urlopen(base + "/openapi.json", timeout=3) as response:
                paths = json.loads(response.read())["paths"]
            self.assertIn("/api/rca/{investigation_id}/candidates", paths)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.assertTrue(self.monitoring_path.exists())
        self.assertFalse(self.application_path.exists())


if __name__ == "__main__":
    unittest.main()
