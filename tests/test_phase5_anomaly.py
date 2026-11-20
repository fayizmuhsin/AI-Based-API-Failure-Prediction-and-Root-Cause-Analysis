import tempfile
import unittest
import math
import random
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.anomaly.engine import AnomalyDetectionService
from app.anomaly.features import FEATURE_NAMES, FeatureExtractor
from app.anomaly.model import IsolationForestModel
from app.anomaly.repository import AnomalyRepository
from app.anomaly.rules import ThresholdRules
from app.core.config import Settings
from app.telemetry.storage import TelemetryRepository


def request_metric(timestamp: str, status: int = 200, duration: float = 30.0, timeout: bool = False) -> dict:
    return {
        "type": "metric",
        "metric_name": "http.server.request",
        "timestamp": timestamp,
        "method": "GET",
        "route": "/api/users/{user_id}",
        "status_code": status,
        "duration_ms": duration,
        "timed_out": timeout,
        "dependency": None,
        "trace_id": None,
    }


def client_metric(timestamp: str, status: int = 200, timeout: bool = False) -> dict:
    return {
        "type": "metric",
        "metric_name": "http.client.request",
        "timestamp": timestamp,
        "method": "GET",
        "route": "/internal/users/{user_id}",
        "status_code": status,
        "duration_ms": 15.0,
        "timed_out": timeout,
        "dependency": "database-service",
        "trace_id": "a" * 32,
    }


class Phase5AnomalyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.application_path = root / "simulated" / "application.db"
        self.monitoring_path = root / "monitoring" / "monitoring.db"
        self.settings = Settings(
            application_database_path=self.application_path,
            monitoring_database_path=self.monitoring_path,
            anomaly_min_training_samples=8,
            anomaly_min_training_requests=3,
            anomaly_rule_min_requests=3,
            anomaly_error_rate_threshold=0.25,
            anomaly_p95_latency_threshold_ms=1000,
            anomaly_timeout_count_threshold=2,
            anomaly_latency_spike_min_history=2,
            anomaly_latency_spike_ratio=2.5,
            anomaly_dependency_failure_rate_threshold=0.3,
            anomaly_request_change_ratio=3,
            anomaly_window_lateness_seconds=0,
        )
        self.telemetry = TelemetryRepository(self.monitoring_path, self.application_path)
        self.telemetry.initialize()
        self.repository = AnomalyRepository(self.monitoring_path, self.application_path)
        self.repository.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_features_use_observed_metrics_and_utc_baselines(self) -> None:
        window = "2020-04-01T10:01:00+00:00"
        records = [
            request_metric("2020-04-01T10:01:01+00:00", 200, 10),
            request_metric("2020-04-01T10:01:02+00:00", 500, 20),
            request_metric("2020-04-01T10:01:03+00:00", 200, 400),
            request_metric("2020-04-01T10:01:04+00:00", 200, 500, timeout=True),
            client_metric("2020-04-01T10:01:05+00:00", 200),
            client_metric("2020-04-01T10:01:06+00:00", 503),
        ]
        history = [{
            "window_start": "2020-04-01T10:00:00+00:00",
            "record_count": 4,
            "total_latency_ms": 200,
        }]
        result = FeatureExtractor().extract("user-service", window, records, history)
        self.assertEqual(result.window_start, window)
        self.assertEqual(tuple(result.values), FEATURE_NAMES)
        self.assertEqual(result.values["request_count"], 4)
        self.assertEqual(result.values["successful_request_count"], 2)
        self.assertEqual(result.values["failed_request_count"], 2)
        self.assertEqual(result.values["error_rate"], 0.5)
        self.assertEqual(result.values["timeout_count"], 1)
        self.assertEqual(result.values["average_latency_ms"], 232.5)
        self.assertEqual(result.values["median_latency_ms"], 210)
        self.assertAlmostEqual(result.values["dependency_failure_rate"], 0.5)
        self.assertEqual(result.dependency_request_count, 2)
        self.assertEqual(result.previous_request_count, 4)

    def test_missing_windows_and_incomplete_baselines_are_safe(self) -> None:
        result = FeatureExtractor().extract(
            "user-service", "2020-04-01T10:01:00+00:00", [], []
        )
        self.assertEqual(result.values["request_count"], 0)
        self.assertEqual(result.values["error_rate"], 0)
        self.assertIsNone(result.values["average_latency_ms"])
        self.assertIsNone(result.values["dependency_failure_rate"])
        self.assertIsNone(result.values["latency_deviation_from_baseline_ms"])
        self.assertIsNone(result.values["request_count_change"])
        self.assertEqual(ThresholdRules(self.settings).evaluate(result), [])
        malformed = FeatureExtractor().extract(
            "user-service",
            "2020-04-01T10:01:00+00:00",
            [request_metric("not-a-timestamp")],
            [],
        )
        self.assertEqual(malformed.values["request_count"], 0)
        self.assertIsNone(malformed.values["p95_latency_ms"])

    def test_threshold_rules_preserve_observed_values_and_limits(self) -> None:
        window = FeatureExtractor().extract(
            "user-service",
            "2020-04-01T10:01:00+00:00",
            [
                request_metric(f"2020-04-01T10:01:0{i}+00:00", 503, 1100)
                for i in range(1, 5)
            ] + [
                client_metric(f"2020-04-01T10:01:0{i}+00:00", 503)
                for i in range(1, 5)
            ],
            [],
        )
        matches = ThresholdRules(self.settings).evaluate(window)
        names = {match["rule"] for match in matches}
        self.assertIn("high_error_rate", names)
        self.assertIn("high_p95_latency_ms", names)
        self.assertIn("dependency_failure_rate", names)
        evidence = next(match for match in matches if match["rule"] == "high_error_rate")
        self.assertEqual(evidence["observed"], 1.0)
        self.assertEqual(evidence["threshold"], 0.25)

    def test_timeout_latency_spike_and_request_volume_rules(self) -> None:
        history = [
            {
                "window_start": f"2020-04-01T10:0{minute}:00+00:00",
                "record_count": 5,
                "total_latency_ms": 500,
            }
            for minute in (0, 1, 2)
        ]
        current = [
            request_metric(f"2020-04-01T10:03:{second:02d}+00:00", 200, 400, timeout=second <= 2)
            for second in range(1, 26)
        ]
        window = FeatureExtractor().extract(
            "order-service", "2020-04-01T10:03:00+00:00", current, history
        )
        matches = ThresholdRules(self.settings).evaluate(window)
        names = {match["rule"] for match in matches}
        self.assertIn("high_timeout_count", names)
        self.assertIn("latency_spike", names)
        self.assertIn("request_volume_change", names)

    def test_isolation_forest_requires_minimum_and_scores_outliers(self) -> None:
        model = IsolationForestModel(self.settings)
        with self.assertRaises(ValueError):
            model.fit([{name: 0.0 for name in FEATURE_NAMES}] * 7)
        healthy = []
        for index in range(40):
            healthy.append({
                name: float((index % 5) + 1) if name in {
                    "request_count", "successful_request_count"
                } else float(index % 3) if name in {
                    "average_latency_ms", "median_latency_ms", "p95_latency_ms"
                } else 0.0
                for name in FEATURE_NAMES
            })
        _, metadata = model.fit(healthy)
        self.assertEqual(metadata["training_sample_count"], 40)
        self.assertEqual(metadata["feature_names"], list(FEATURE_NAMES))
        normal = model.score(healthy[0])
        unusual = model.score({
            **healthy[0], "error_rate": 1.0, "failed_request_count": 100,
            "average_latency_ms": 100000, "p95_latency_ms": 200000,
        })
        self.assertIn(normal["decision"], {"normal", "anomaly"})
        self.assertIn(unusual["decision"], {"normal", "anomaly"})
        self.assertIsInstance(unusual["score"], float)
        self.assertGreater(unusual["score"], normal["score"])
        self.assertGreaterEqual(unusual["score"], 0.0)
        self.assertLessEqual(unusual["score"], 1.0)
        self.assertNotIn("probability", unusual)

    def test_tree_construction_selects_varying_feature_and_in_range_split(self) -> None:
        rows = []
        for value in range(8):
            row = [0.0] * len(FEATURE_NAMES)
            row[2] = float(value)
            row[7] = float(value * 2)
            rows.append(row)
        tree = IsolationForestModel._build_tree(rows, 0, 3, random.Random(7))
        self.assertIn(tree["feature"], {2, 7})
        chosen = tree["feature"]
        self.assertGreaterEqual(tree["split"], 0.0)
        self.assertLessEqual(tree["split"], max(row[chosen] for row in rows))
        self.assertTrue("feature" in tree["left"] or tree["left"].get("size", 0) > 0)
        self.assertTrue("feature" in tree["right"] or tree["right"].get("size", 0) > 0)
        self.assertEqual(
            tree,
            IsolationForestModel._build_tree(rows, 0, 3, random.Random(7)),
        )

    def test_path_length_and_standard_isolation_score_formula(self) -> None:
        self.assertEqual(IsolationForestModel.expected_path_length(1), 0.0)
        self.assertEqual(IsolationForestModel.expected_path_length(2), 1.0)
        self.assertAlmostEqual(IsolationForestModel.expected_path_length(3), 5.0 / 3.0)
        leaf = {"size": 2}
        self.assertEqual(IsolationForestModel._path_length([0.0] * len(FEATURE_NAMES), leaf), 1.0)
        model = IsolationForestModel(self.settings)
        model.pipeline = {
            "medians": [0.0] * len(FEATURE_NAMES),
            "trees": [{"feature": 0, "split": 1.0, "left": {"size": 1}, "right": {"size": 1}}],
            "sample_size": 2,
            "max_height": 1,
            "decision_threshold": 0.5,
        }
        self.assertAlmostEqual(model._score_vector([0.0] * len(FEATURE_NAMES)), 0.5)

    def test_model_handles_constant_features_missing_values_and_invalid_inputs(self) -> None:
        model = IsolationForestModel(self.settings)
        constant = [{name: 4.0 for name in FEATURE_NAMES} for _ in range(8)]
        model.fit(constant)
        self.assertTrue(model.ready)
        self.assertEqual(model.score({"request_count": None})["decision"], "normal")
        self.assertTrue(math.isfinite(model.score({"request_count": float("nan")})["score"]))
        with self.assertRaises(ValueError):
            model.score({"request_count": "not-a-number"})
        with self.assertRaises(ValueError):
            model.fit([None] * 8)

    def test_training_and_scores_are_reproducible_and_serialization_is_safe(self) -> None:
        rows = [
            {name: float((index * (position + 2)) % 11) for position, name in enumerate(FEATURE_NAMES)}
            for index in range(30)
        ]
        first = IsolationForestModel(self.settings)
        second = IsolationForestModel(self.settings)
        first_blob, _ = first.fit(rows)
        second_blob, _ = second.fit(rows)
        self.assertEqual(first_blob, second_blob)
        query = rows[-1]
        self.assertEqual(first.score(query), second.score(query))
        restored = IsolationForestModel(self.settings)
        restored.activate(first_blob, first.metadata)
        self.assertEqual(restored.score(query), first.score(query))
        with self.assertRaises(ValueError):
            restored.activate(b"__import__('os').system('whoami')", first.metadata)

    def test_isolated_observation_gets_higher_anomaly_score(self) -> None:
        rows = []
        for index in range(80):
            rows.append({
                name: (float(index % 7) if position == 0 else 0.0)
                for position, name in enumerate(FEATURE_NAMES)
            })
        model = IsolationForestModel(self.settings)
        model.fit(rows)
        common = {name: 2.0 if name == FEATURE_NAMES[0] else 0.0 for name in FEATURE_NAMES}
        isolated = {name: 10000.0 if name == FEATURE_NAMES[0] else 0.0 for name in FEATURE_NAMES}
        self.assertGreater(model.score(isolated)["score"], model.score(common)["score"])

    def test_detection_engine_trains_and_scores_real_forest(self) -> None:
        self._insert_healthy_history(12)
        service = AnomalyDetectionService(self.telemetry, self.repository, self.settings)
        trained = service.train()
        self.assertEqual(trained["state"], "ready")
        model_score = service.model.score({name: 0.0 for name in FEATURE_NAMES})
        self.assertIsInstance(model_score["score"], float)
        self.assertIn(model_score["decision"], {"normal", "anomaly"})

    def _insert_healthy_history(self, count: int) -> list[tuple[str, str]]:
        rows = []
        keys = []
        for minute in range(count):
            start = datetime(2020, 4, 1, 10, minute, tzinfo=timezone.utc)
            keys.append(("user-service", start.isoformat()))
            for second in range(5):
                rows.append(request_metric(
                    (start.replace(second=second + 1)).isoformat(),
                    200,
                    30 + (minute % 3) + second,
                ))
        self.telemetry.insert_batch("user-service", rows)
        self.telemetry.process_batch(1000)
        return keys

    def test_explicit_training_uses_healthy_observed_history_and_persists_model(self) -> None:
        self._insert_healthy_history(10)
        service = AnomalyDetectionService(self.telemetry, self.repository, self.settings)
        result = service.train()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["training_sample_count"], 10)
        status = service.model_status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["training_sample_count"], 10)
        stored = self.repository.active_model()
        self.assertTrue(stored["model_blob"])
        restarted = AnomalyDetectionService(self.telemetry, self.repository, self.settings)
        restarted.load_active_model()
        self.assertTrue(restarted.model.ready)
        self.assertEqual(restarted.model.metadata["model_version"], result["model_version"])

    def test_insufficient_training_history_is_reported_without_crashing(self) -> None:
        self._insert_healthy_history(4)
        service = AnomalyDetectionService(self.telemetry, self.repository, self.settings)
        result = service.train()
        self.assertEqual(result["state"], "not_ready")
        self.assertEqual(result["training_sample_count"], 4)
        self.assertEqual(result["required_training_samples"], 8)

    def test_training_excludes_previously_detected_anomalous_windows(self) -> None:
        keys = self._insert_healthy_history(10)
        for service_id, window_start in keys[:3]:
            self.repository.record_detection(
                service_id=service_id,
                window_start=window_start,
                model_version=None,
                model_score=None,
                model_decision="not_ready",
                threshold_matches=[{"rule": "known_anomaly", "observed": 1, "threshold": 0}],
                feature_values={"request_count": 5},
                is_anomaly=True,
                severity="high",
            )
        service = AnomalyDetectionService(self.telemetry, self.repository, self.settings)
        result = service.train()
        self.assertEqual(result["state"], "not_ready")
        self.assertEqual(result["training_sample_count"], 7)

    def test_detection_persists_combined_evidence_once_and_keeps_database_separate(self) -> None:
        window_start = datetime(2020, 5, 1, 12, 0, tzinfo=timezone.utc)
        items = [
            request_metric(
                window_start.replace(second=second).isoformat(),
                503,
                1200,
            )
            for second in range(1, 7)
        ]
        self.telemetry.insert_batch("order-service", items)
        self.telemetry.process_batch(100)
        service = AnomalyDetectionService(self.telemetry, self.repository, self.settings)
        processed = service.process_batch()
        self.assertEqual(processed, 1)
        self.assertEqual(service.process_batch(), 0)
        anomalies = self.repository.recent_anomalies(10, "order-service")
        self.assertEqual(len(anomalies), 1)
        detail = self.repository.anomaly_detail(anomalies[0]["anomaly_id"])
        self.assertEqual(detail["model_decision"], "not_ready")
        self.assertEqual(detail["feature_values"]["error_rate"], 1.0)
        self.assertIn("high_error_rate", {item["rule"] for item in detail["threshold_matches"]})
        self.assertEqual(self.repository.anomaly_summary()["total_anomalies"], 1)
        self.assertFalse(self.application_path.exists())
        self.assertTrue(self.monitoring_path.exists())

    def test_model_only_detection_is_persisted_when_thresholds_do_not_match(self) -> None:
        window_start = datetime(2020, 5, 2, 12, 0, tzinfo=timezone.utc)
        self.telemetry.insert_batch(
            "user-service",
            [request_metric(window_start.replace(second=1).isoformat(), 200, 30)],
        )
        self.telemetry.process_batch(100)
        service = AnomalyDetectionService(self.telemetry, self.repository, self.settings)
        with patch.object(
            service.model,
            "score",
            return_value={"score": -0.9, "decision": "anomaly"},
        ):
            self.assertEqual(service.process_batch(), 1)
        anomalies = self.repository.recent_anomalies(10, "user-service")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["model_decision"], "anomaly")
        self.assertEqual(anomalies[0]["model_score"], -0.9)
        self.assertEqual(anomalies[0]["threshold_matches"], [])


if __name__ == "__main__":
    unittest.main()
