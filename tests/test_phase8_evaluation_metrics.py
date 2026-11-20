from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.evaluation.reporting import (
    build_evaluation_artifact,
    check_expected_label_leaks,
    new_evaluation_run_id,
    write_evaluation_artifact,
)


STARTED = "2026-11-20T10:00:00Z"
COMPLETED = "2026-11-20T10:01:00Z"


def scenario(
    scenario_id: str,
    *,
    model_decision: str = "normal",
    threshold_matches: list[dict] | None = None,
    rca_status: str | None = None,
    rca_selected: bool | None = None,
    recovery: bool | None = True,
) -> dict:
    investigations = []
    if rca_status is not None:
        selected = rca_status == "completed" if rca_selected is None else rca_selected
        candidate = {
            "service_id": "user-service",
            "rank": 1,
            "rank_score": 73.0,
            "score_semantics": "ranking points; not a probability",
            "selected_as_probable_root_cause": selected,
            "evidence": [{"signal": "observed error rate"}],
        }
        investigations = [{
            "status": rca_status,
            "root_causes": [candidate] if selected else [],
            "ranked_candidates": [candidate],
            "evidence_summary": {"telemetry_records_examined": 4},
        }]
    return {
        "scenario_id": scenario_id,
        "status": "complete",
        "run_id": "fault-run-1",
        "baseline_http": [{"status_code": 200}],
        "http_results": [{
            "status_code": 503,
            "request_started_at": "2026-11-20T10:00:05Z",
            "completed_at": "2026-11-20T10:00:06Z",
        }],
        "recovery_http": {"status_code": 200},
        "observed_error_rates_and_latency": [{
            "service_id": "user-service",
            "request_count": 5,
            "error_rate": 1.0,
            "average_latency_ms": 25.0,
        }],
        "telemetry_received": True,
        "telemetry_processed": True,
        "telemetry_records": 5,
        "affected_service_ids_observed": ["user-service"],
        "trace_and_dependency_evidence": {"trace_count": 1},
        "isolation_forest_scores_and_decisions": [{
            "service_id": "user-service",
            "score": 0.81,
            "decision": model_decision,
        }],
        "threshold_rule_matches": [{
            "service_id": "user-service",
            "matches": threshold_matches if threshold_matches is not None else [],
        }],
        "anomalies": [{
            "anomaly_id": 8,
            "detected_at": "2026-11-20T10:00:15Z",
            "model_decision": model_decision,
        }] if model_decision == "anomaly" or threshold_matches else [],
        "rca_triggered": bool(investigations),
        "rca_processing_complete": bool(investigations),
        "rca_investigations": investigations,
        "recovery_health": {"user-service": {"status_code": 200, "status": "ok"}},
        "recovery_succeeded": recovery,
        "recovery_started_at": "2026-11-20T10:00:20Z",
        "recovery_completed_at": "2026-11-20T10:00:22Z" if recovery is not None else None,
    }


class Phase8EvaluationMetricsTests(unittest.TestCase):
    def test_detector_mechanisms_are_reported_separately_and_combined(self) -> None:
        results = [
            scenario("model", model_decision="anomaly"),
            scenario("rules", threshold_matches=[{
                "rule": "high_error_rate", "observed": 0.9, "threshold": 0.25,
            }]),
            scenario("neither"),
        ]
        artifact = build_evaluation_artifact(results, {}, STARTED, COMPLETED, "run-metrics")
        model, rules, neither = artifact["scenarios"]

        self.assertTrue(model["detection"]["isolation_forest_detected"])
        self.assertFalse(model["detection"]["threshold_rules_detected"])
        self.assertTrue(model["detection"]["either_mechanism_detected"])
        self.assertFalse(rules["detection"]["isolation_forest_detected"])
        self.assertTrue(rules["detection"]["threshold_rules_detected"])
        self.assertTrue(rules["detection"]["either_mechanism_detected"])
        self.assertFalse(neither["detection"]["either_mechanism_detected"])

    def test_isolation_forest_missing_malformed_decisions_are_unavailable(self) -> None:
        result = scenario("invalid-model")
        result["isolation_forest_scores_and_decisions"] = [
            {"score": 0.3, "service_id": "a"},
            {"score": 0.4, "decision": ["anomaly"]},
            {"score": 0.5, "decision": "unknown"},
        ]
        result["status"] = "incomplete"
        observed = build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-invalid-model",
        )["scenarios"][0]["detection"]
        self.assertIsNone(observed["isolation_forest_detected"])
        self.assertEqual(
            [row["score"] for row in observed["valid_model_scores"]],
            [0.3, 0.4, 0.5],
        )
        self.assertIsNone(observed["isolation_forest_scores_and_decisions"][0]["decision"])

        result["isolation_forest_scores_and_decisions"] = [
            {"score": 0.81, "decision": "anomaly"},
            {"score": None, "decision": "normal"},
        ]
        self.assertTrue(build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-valid-anomaly",
        )["scenarios"][0]["detection"]["isolation_forest_detected"])

        result["status"] = "complete"
        result["isolation_forest_scores_and_decisions"] = [
            {"score": 0.3, "decision": "normal"},
            {"score": 0.4, "decision": "normal"},
        ]
        self.assertFalse(build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-valid-normal",
        )["scenarios"][0]["detection"]["isolation_forest_detected"])
        result["isolation_forest_scores_and_decisions"][0]["score"] = None
        self.assertIsNone(build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-normal-missing-score",
        )["scenarios"][0]["detection"]["isolation_forest_detected"])

    def test_threshold_missing_and_malformed_matches_are_unavailable(self) -> None:
        result = scenario("invalid-rules")
        for rows in (
            [{"service_id": "a"}],
            [{"matches": None}],
            [{"matches": "not-a-list"}],
            [{"matches": [{"rule": "high_error_rate"}]}],
            [{"matches": [
                {"rule": "high_error_rate", "observed": 0.9, "threshold": 0.25},
                {"rule": "invalid_without_threshold"},
            ]}],
        ):
            result["threshold_rule_matches"] = rows
            observed = build_evaluation_artifact(
                [result], {}, STARTED, COMPLETED, f"run-rules-{len(str(rows))}",
            )["scenarios"][0]["detection"]["threshold_rules_detected"]
            self.assertIsNone(observed, rows)

        result["threshold_rule_matches"] = [{
            "matches": [{"rule": "high_error_rate", "observed": 0.9, "threshold": 0.25}],
        }]
        self.assertTrue(build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-valid-rule",
        )["scenarios"][0]["detection"]["threshold_rules_detected"])
        result["threshold_rule_matches"] = [{"matches": []}]
        self.assertFalse(build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-no-rule",
        )["scenarios"][0]["detection"]["threshold_rules_detected"])

    def test_combined_detection_does_not_convert_unavailable_to_negative(self) -> None:
        result = scenario("partial-mechanism")
        result["isolation_forest_scores_and_decisions"] = [
            {"score": 0.3, "decision": "normal"},
        ]
        result["threshold_rule_matches"] = [{"matches": None}]
        detection = build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-combined-unavailable",
        )["scenarios"][0]["detection"]
        self.assertFalse(detection["isolation_forest_detected"])
        self.assertIsNone(detection["threshold_rules_detected"])
        self.assertIsNone(detection["either_mechanism_detected"])

        result["isolation_forest_scores_and_decisions"] = [
            {"score": 0.9, "decision": "anomaly"},
        ]
        detection = build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-combined-positive",
        )["scenarios"][0]["detection"]
        self.assertTrue(detection["either_mechanism_detected"])

    def test_duplicate_anomaly_ids_are_counted_separately(self) -> None:
        result = scenario("duplicate-anomalies")
        result["anomalies"] = [
            {"anomaly_id": 7},
            {"anomaly_id": 7},
            {"anomaly_id": 8},
            {"detected_at": "2026-11-20T10:00:15Z"},
            {"anomaly_id": []},
        ]
        detection = build_evaluation_artifact(
            [result], {}, STARTED, COMPLETED, "run-duplicate-anomalies",
        )["scenarios"][0]["detection"]
        self.assertEqual(detection["anomaly_record_count"], 2)
        self.assertEqual(detection["duplicate_anomaly_record_count"], 1)
        self.assertEqual(detection["anomaly_records_without_id_count"], 2)

    def test_latency_http_and_overall_scenario_metrics(self) -> None:
        artifact = build_evaluation_artifact(
            [scenario("measured", model_decision="anomaly")], {}, STARTED, COMPLETED, "run-latency",
        )
        observed = artifact["scenarios"][0]
        self.assertEqual(observed["detection"]["anomaly_detection_latency_ms"], 10_000.0)
        self.assertEqual(observed["recovery"]["recovery_time_ms"], 2_000.0)
        self.assertEqual(observed["http"]["outcome_counts"]["2xx"], 2)
        self.assertEqual(observed["http"]["outcome_counts"]["5xx"], 1)
        self.assertEqual(artifact["summary"]["completed_scenarios"], 1)

    def test_missing_and_incomplete_scenario_data_remains_unavailable(self) -> None:
        artifact = build_evaluation_artifact(
            [{"scenario_id": "partial", "status": "incomplete", "reason": "telemetry absent"}],
            {},
            STARTED,
            COMPLETED,
            "run-partial",
        )
        observed = artifact["scenarios"][0]
        self.assertIsNone(observed["detection"]["isolation_forest_detected"])
        self.assertIsNone(observed["detection"]["threshold_rules_detected"])
        self.assertIsNone(observed["recovery"]["succeeded"])
        self.assertIsNone(observed["rca"]["investigation_status"])
        self.assertIn("telemetry_metrics", observed["missing_measurements"])
        self.assertEqual(artifact["summary"]["failed_or_incomplete_scenarios"], 1)
        self.assertIsNone(
            artifact["summary"]["detection"]["anomaly_detection_latency_ms"]["mean"]
        )

    def test_rca_probable_and_inconclusive_results_are_distinct(self) -> None:
        results = [
            scenario("probable", rca_status="completed"),
            scenario("completed-no-cause", rca_status="completed", rca_selected=False),
            scenario("inconclusive", rca_status="inconclusive", rca_selected=False),
            scenario("failed", rca_status="failed", rca_selected=True),
        ]
        artifact = build_evaluation_artifact(results, {}, STARTED, COMPLETED, "run-rca")
        probable, completed_without_cause, inconclusive, failed = artifact["scenarios"]
        self.assertEqual(probable["rca"]["outcomes"], ["probable"])
        self.assertEqual(probable["rca"]["investigation_status"], "completed")
        self.assertEqual(completed_without_cause["rca"]["outcome"], "unavailable")
        self.assertEqual(completed_without_cause["rca"]["investigation_status"], "completed")
        self.assertEqual(inconclusive["rca"]["outcomes"], ["inconclusive"])
        self.assertEqual(inconclusive["rca"]["investigation_status"], "inconclusive")
        self.assertEqual(failed["rca"]["investigation_status"], "failed")
        self.assertEqual(failed["rca"]["outcome"], "unavailable")
        self.assertEqual(artifact["summary"]["rca"]["probable_outcomes"], 1)
        self.assertEqual(artifact["summary"]["rca"]["inconclusive_outcomes"], 1)
        self.assertEqual(artifact["summary"]["rca"]["unavailable_outcomes"], 2)

    def test_labels_are_attached_only_to_reporting_fields(self) -> None:
        sentinel = "posthoc-label-only-9238"
        result = scenario("case-1")
        artifact = build_evaluation_artifact(
            [result], {"case-1": sentinel}, STARTED, COMPLETED, "run-label-isolation",
        )
        item = artifact["scenarios"][0]
        self.assertEqual(item["expected_scenario_label"], sentinel)
        self.assertNotIn(sentinel, json.dumps(item["telemetry"], sort_keys=True))
        self.assertNotIn(sentinel, json.dumps(item["rca"], sort_keys=True))
        self.assertNotIn("expected_scenario_label", result)

    def test_expected_label_checks_cover_process_logs_and_observation_boundaries(self) -> None:
        label = "expected-database-failure"
        surfaces = {
            "service_process_stdout_stderr": {"database-service": "database ready"},
            "fault_simulator_process_log": "fault lease stopped",
            "captured_api_response_bodies": [{"detail": "service unavailable"}],
            "telemetry_and_trace_records": [{"type": "trace", "trace_id": "abc"}],
            "rca_inputs": [{"anomaly_id": 3, "observed_signal": "error rate"}],
            "rca_results": [{"status": "inconclusive", "evidence": []}],
        }
        checks = check_expected_label_leaks(label, surfaces)
        self.assertEqual(set(checks), set(surfaces))
        self.assertTrue(all(value is False for value in checks.values()))

        for source in surfaces:
            contaminated = dict(surfaces)
            contaminated[source] = f"observed {label}"
            self.assertTrue(check_expected_label_leaks(label, contaminated)[source], source)
        unavailable = dict(surfaces, service_process_stdout_stderr=None)
        self.assertIsNone(
            check_expected_label_leaks(label, unavailable)["service_process_stdout_stderr"]
        )

    def test_json_artifact_is_valid_and_run_ids_are_unique(self) -> None:
        first_id = new_evaluation_run_id()
        second_id = new_evaluation_run_id()
        self.assertNotEqual(first_id, second_id)
        artifact = build_evaluation_artifact(
            [scenario("case-1")], {}, STARTED, COMPLETED, first_id,
        )
        with tempfile.TemporaryDirectory() as directory:
            json_path, summary_path = write_evaluation_artifact(artifact, Path(directory))
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed, artifact)
            self.assertTrue(summary_path.exists())
            with self.assertRaises(FileExistsError):
                write_evaluation_artifact(artifact, Path(directory))


if __name__ == "__main__":
    unittest.main()
