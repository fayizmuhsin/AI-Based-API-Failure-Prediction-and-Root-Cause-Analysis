import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.anomaly.repository import AnomalyRepository
from app.telemetry.storage import TelemetryRepository


class Phase5PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app_path = root / "simulated" / "application.db"
        self.monitor_path = root / "monitoring" / "monitor.db"
        self.telemetry = TelemetryRepository(self.monitor_path, self.app_path)
        self.telemetry.initialize()
        self.anomalies = AnomalyRepository(self.monitor_path, self.app_path)
        self.anomalies.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _persist_processed_window(self) -> tuple[str, str]:
        window = datetime(2020, 6, 1, 10, 0, tzinfo=timezone.utc)
        self.telemetry.insert_batch("api-gateway", [{
            "type": "metric",
            "metric_name": "http.server.request",
            "timestamp": window.replace(second=1).isoformat(),
            "method": "GET",
            "route": "/api/orders",
            "status_code": 500,
            "duration_ms": 1250.0,
            "timed_out": False,
            "dependency": None,
            "trace_id": "a" * 32,
        }])
        self.telemetry.process_batch(10)
        return "api-gateway", window.isoformat()

    def test_detection_run_and_anomaly_are_idempotent_and_preserve_evidence(self) -> None:
        service_id, window_start = self._persist_processed_window()
        feature_values = {"request_count": 1.0, "error_rate": 1.0, "p95_latency_ms": 1250.0}
        rules = [{"rule": "high_error_rate", "observed": 1.0, "threshold": 0.25}]
        args = dict(
            service_id=service_id,
            window_start=window_start,
            model_version=None,
            model_score=None,
            model_decision="not_ready",
            threshold_matches=rules,
            feature_values=feature_values,
            is_anomaly=True,
            severity="high",
        )
        inserted, anomaly_id = self.anomalies.record_detection(**args)
        self.assertTrue(inserted)
        self.assertGreater(anomaly_id, 0)
        self.assertEqual(self.anomalies.record_detection(**args), (False, None))

        items = self.anomalies.recent_anomalies(10, "api-gateway")
        self.assertEqual(len(items), 1)
        detail = self.anomalies.anomaly_detail(anomaly_id)
        self.assertEqual(detail["feature_values"], feature_values)
        self.assertEqual(detail["threshold_matches"], rules)
        summary = self.anomalies.anomaly_summary()
        self.assertEqual(summary["total_anomalies"], 1)
        self.assertEqual(summary["by_service"][0]["high_count"], 1)
        self.assertEqual(self.anomalies.known_anomalous_windows(), {(service_id, window_start)})

    def test_detection_candidates_skip_completed_runs_and_keep_monitoring_database_separate(self) -> None:
        key = self._persist_processed_window()
        cutoff = datetime(2020, 6, 1, 10, 2, tzinfo=timezone.utc)
        self.assertEqual(self.anomalies.completed_window_keys(cutoff), [key])
        self.anomalies.record_detection(
            service_id=key[0],
            window_start=key[1],
            model_version=None,
            model_score=None,
            model_decision="not_ready",
            threshold_matches=[],
            feature_values={},
            is_anomaly=False,
            severity="low",
        )
        self.assertEqual(self.anomalies.completed_window_keys(cutoff), [])
        self.assertTrue(self.monitor_path.exists())
        self.assertFalse(self.app_path.exists())

    def test_model_metadata_is_persisted_without_exposing_model_blob_in_status(self) -> None:
        metadata = {
            "model_version": "test-model-version",
            "trained_at": "2026-06-01T10:00:00+00:00",
            "feature_names": ["request_count", "error_rate"],
            "training_sample_count": 40,
            "model_config": {"algorithm": "IsolationForest", "random_state": 42, "contamination": 0.05},
        }
        self.anomalies.save_model(b"private model bytes", metadata, "2026-06-01T09:59:00+00:00")
        stored = self.anomalies.active_model()
        status = self.anomalies.model_status()
        self.assertEqual(stored["model_blob"], b"private model bytes")
        self.assertEqual(status["training_sample_count"], 40)
        self.assertEqual(status["feature_names"], metadata["feature_names"])
        self.assertNotIn("model_blob", status)
        self.assertNotIn("path", status)


if __name__ == "__main__":
    unittest.main()
