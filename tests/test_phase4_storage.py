import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
from app.telemetry.storage import TelemetryRepository, TelemetryStorageError
from app.telemetry.worker import TelemetryWorker
from app.api.routes import telemetry as telemetry_routes
from app.core.config import Settings


def metric(timestamp: str = "2026-11-19T10:00:00+00:00", status_code: int = 200) -> dict:
    return {
        "type": "metric",
        "metric_name": "http.server.request",
        "timestamp": timestamp,
        "method": "GET",
        "route": "/api/users/{user_id}",
        "status_code": status_code,
        "duration_ms": 12.5,
        "timed_out": False,
        "dependency": None,
        "trace_id": "a" * 32,
    }


class Phase4TelemetryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app_path = root / "simulated" / "application.db"
        self.monitor_path = root / "monitoring" / "monitoring.db"
        self.repository = TelemetryRepository(self.monitor_path, self.app_path)
        self.repository.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_is_created_under_monitoring_path_only(self) -> None:
        self.assertTrue(self.monitor_path.exists())
        self.assertFalse(self.app_path.exists())
        with closing(sqlite3.connect(self.monitor_path)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertTrue({"telemetry_records", "telemetry_windows", "processing_checkpoints"} <= tables)

    def test_metrics_traces_and_logs_are_persisted_and_duplicates_are_idempotent(self) -> None:
        records = [
            metric(),
            {
                "type": "trace", "timestamp": "2026-11-19T10:00:00+00:00",
                "end_time": "2026-11-19T10:00:00.010000+00:00", "trace_id": "b" * 32,
                "span_id": "c" * 16, "parent_span_id": None, "name": "GET /users",
                "kind": "SERVER", "status": "OK", "status_description": None,
                "attributes": {"http.method": "GET"}, "events": [],
            },
            {
                "type": "log", "timestamp": "2026-11-19T10:00:00+00:00",
                "level": "INFO", "event_type": "http.server.request", "trace_id": None,
                "method": "GET", "route": "/health", "status_code": 200,
                "duration_ms": 1.0, "error_details": None, "dependency": None,
            },
        ]
        self.assertEqual(self.repository.insert_batch("api-gateway", records), (3, 0))
        self.assertEqual(self.repository.insert_batch("api-gateway", records), (0, 3))
        recent, total = self.repository.recent(10)
        self.assertEqual(total, 3)
        self.assertEqual({record["type"] for record in recent}, {"metric", "trace", "log"})
        self.assertTrue(all(record["service_id"] == "api-gateway" for record in recent))
        self.assertTrue(all(record["processing_status"] == "pending" for record in recent))

    def test_processing_checkpoint_and_aggregation_are_transactional(self) -> None:
        self.repository.insert_batch(
            "user-service", [metric(status_code=200), metric(timestamp="2026-11-19T10:00:10+00:00", status_code=503)]
        )
        self.assertEqual(self.repository.process_batch(batch_size=1), 1)
        checkpoint = self.repository.checkpoint()
        self.assertEqual(checkpoint["processed_records"], 1)
        self.assertEqual(self.repository.process_batch(batch_size=10), 1)
        self.assertEqual(self.repository.process_batch(batch_size=10), 0)
        checkpoint = self.repository.checkpoint()
        self.assertEqual(checkpoint["processed_records"], 2)
        with closing(sqlite3.connect(self.monitor_path)) as connection:
            rows = connection.execute(
                "SELECT record_count, failed_count FROM telemetry_windows"
            ).fetchall()
            statuses = connection.execute(
                "SELECT DISTINCT processing_status FROM telemetry_records"
            ).fetchall()
        self.assertEqual(rows, [(2, 1)])
        self.assertEqual(statuses, [("processed",)])

    def test_restart_resumes_pending_records_and_concurrent_workers_do_not_duplicate(self) -> None:
        records = [metric(timestamp=f"2026-11-19T10:{minute:02d}:00+00:00") for minute in range(20)]
        self.repository.insert_batch("order-service", records)
        restarted = TelemetryRepository(self.monitor_path, self.app_path)
        restarted.initialize()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda repo: repo.process_batch(20), [self.repository, restarted]))
        self.assertEqual(sum(results), 20)
        self.assertEqual(restarted.checkpoint()["processed_records"], 20)
        self.assertEqual(restarted.process_batch(), 0)

    def test_worker_lifecycle_processes_pending_records_and_stops_cleanly(self) -> None:
        self.repository.insert_batch("database-service", [metric()])
        worker = TelemetryWorker(self.repository, poll_interval_seconds=0.02)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.repository.checkpoint()["processed_records"] == 1:
                    break
                time.sleep(0.01)
            self.assertEqual(self.repository.checkpoint()["processed_records"], 1)
        finally:
            worker.stop()
        self.assertIsNone(worker._thread)

    def test_failed_batch_write_and_failed_processing_leave_state_unchanged(self) -> None:
        with closing(sqlite3.connect(self.monitor_path)) as connection:
            connection.execute("""CREATE TRIGGER reject_telemetry BEFORE INSERT ON telemetry_records
                BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")
        with self.assertLogs("app.telemetry.storage", level="ERROR"):
            with self.assertRaises(TelemetryStorageError):
                self.repository.insert_batch("api-gateway", [metric()])
        with closing(sqlite3.connect(self.monitor_path)) as connection:
            connection.execute("DROP TRIGGER reject_telemetry")
        self.repository.insert_batch("api-gateway", [metric()])
        with closing(sqlite3.connect(self.monitor_path)) as connection:
            connection.execute("""CREATE TRIGGER reject_window BEFORE INSERT ON telemetry_windows
                BEGIN SELECT RAISE(ABORT, 'test processing failure'); END""")
        with self.assertLogs("app.telemetry.storage", level="ERROR"):
            with self.assertRaises(TelemetryStorageError):
                self.repository.process_batch()
        checkpoint = self.repository.checkpoint()
        recent, _ = self.repository.recent(1)
        self.assertEqual(checkpoint["processed_records"], 0)
        self.assertEqual(recent[0]["processing_status"], "pending")

    def test_same_database_path_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TelemetryRepository(self.monitor_path, self.monitor_path)

    def test_settings_reject_same_monitoring_and_simulated_database_path(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                monitoring_database_path=self.monitor_path,
                application_database_path=self.monitor_path,
            )

    def test_ingestion_returns_storage_error_instead_of_acknowledging(self) -> None:
        payload = {
            "service_id": "api-gateway",
            "items": [metric()],
        }
        batch = telemetry_routes.TelemetryBatch.model_validate(payload)
        with patch.object(
            telemetry_routes.telemetry_repository,
            "insert_batch",
            side_effect=TelemetryStorageError("simulated disk write failure"),
        ):
            with self.assertRaises(HTTPException) as raised:
                telemetry_routes.ingest(batch)
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
