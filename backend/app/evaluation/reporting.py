"""Metrics and durable artifacts for observed Phase 8 evaluation runs."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import uuid4


LIMITATIONS = [
    "This bounded fault-injection evaluation is not a production workload.",
    "Expected scenario labels are post-hoc evaluation metadata and are not detector or RCA inputs.",
    "Isolation Forest scores are anomaly scores, not probabilities; RCA rank scores are ranking points, not probabilities or causal proof.",
    "Accuracy, precision, recall, and F1 are not reported because this run is not a labeled statistical evaluation dataset.",
    "Detection latency is measured from the first observed fault request to the earliest anomaly record; it does not establish causality.",
    "Recovery time is the elapsed time from explicit fault stop to successful recovery verification, an upper bound rather than the exact restoration instant.",
    "Anomaly records without a usable ID are excluded from the unique-ID count and reported separately.",
    "Expected-label leakage checks are conclusive only when every required observation source is available; null marks an unavailable check.",
]
EXPECTED_LABEL_LEAK_CHECK_SOURCES = (
    "service_process_stdout_stderr",
    "fault_simulator_process_log",
    "captured_api_response_bodies",
    "telemetry_and_trace_records",
    "rca_inputs",
    "rca_results",
)


def new_evaluation_run_id() -> str:
    return str(uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def check_expected_label_leaks(
    expected_label: str, observed_sources: dict[str, Any]
) -> dict[str, bool | None]:
    """Search captured observation surfaces; None means the source was unavailable."""
    if not isinstance(expected_label, str) or not expected_label:
        raise ValueError("Expected scenario label must be a non-empty string")
    return {
        source: (
            None if value is None
            else expected_label.casefold()
            in json.dumps(value, sort_keys=True, ensure_ascii=False).casefold()
        )
        for source, value in observed_sources.items()
    }


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _http_results(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for key in ("baseline_http", "http_results", "recovery_http"):
        value = scenario.get(key)
        if isinstance(value, list):
            results.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            results.append(value)
    return results


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _valid_model_decision(value: Any) -> bool:
    return isinstance(value, str) and value in {"anomaly", "normal"}


def _reported_model_rows(records: list[Any]) -> list[dict[str, Any]]:
    reported = []
    for row in records:
        if not isinstance(row, dict):
            reported.append({
                "service_id": None,
                "window_start": None,
                "score": None,
                "decision": None,
                "model_version": None,
            })
            continue
        decision = row.get("decision")
        reported.append({
            "service_id": row.get("service_id") if isinstance(row.get("service_id"), str) else None,
            "window_start": row.get("window_start") if isinstance(row.get("window_start"), str) else None,
            "score": row.get("score") if _finite_number(row.get("score")) else None,
            "decision": decision if _valid_model_decision(decision) else None,
            "model_version": row.get("model_version") if isinstance(row.get("model_version"), str) else None,
        })
    return reported


def _valid_threshold_match(match: Any) -> bool:
    return (
        isinstance(match, dict)
        and isinstance(match.get("rule"), str)
        and bool(match["rule"].strip())
        and _finite_number(match.get("observed"))
        and _finite_number(match.get("threshold"))
    )


def _reported_threshold_rows(records: list[Any]) -> list[dict[str, Any]]:
    reported = []
    for row in records:
        if not isinstance(row, dict):
            reported.append({"service_id": None, "window_start": None, "matches": None})
            continue
        matches = row.get("matches")
        if isinstance(matches, list):
            safe_matches = [
                dict(match) if _valid_threshold_match(match) else {"invalid_match": True}
                for match in matches
            ]
        else:
            safe_matches = None
        reported.append({
            "service_id": row.get("service_id") if isinstance(row.get("service_id"), str) else None,
            "window_start": row.get("window_start") if isinstance(row.get("window_start"), str) else None,
            "matches": safe_matches,
        })
    return reported


def _anomaly_record_counts(records: list[Any]) -> tuple[int, int, int]:
    unique_ids: set[int | str] = set()
    duplicates = 0
    missing_ids = 0
    for record in records:
        anomaly_id = record.get("anomaly_id") if isinstance(record, dict) else None
        valid_id = (
            isinstance(anomaly_id, int) and not isinstance(anomaly_id, bool) and anomaly_id > 0
        ) or (isinstance(anomaly_id, str) and bool(anomaly_id.strip()))
        if not valid_id:
            missing_ids += 1
        elif anomaly_id in unique_ids:
            duplicates += 1
        else:
            unique_ids.add(anomaly_id)
    return len(unique_ids), duplicates, missing_ids


def _detection_metrics(scenario: dict[str, Any]) -> dict[str, Any]:
    raw_scores = scenario.get("isolation_forest_scores_and_decisions")
    raw_thresholds = scenario.get("threshold_rule_matches")
    scores = raw_scores if isinstance(raw_scores, list) else []
    thresholds = raw_thresholds if isinstance(raw_thresholds, list) else []
    reported_scores = _reported_model_rows(scores)
    reported_thresholds = _reported_threshold_rows(thresholds)
    valid_model_decisions = [
        row.get("decision") for row in scores
        if isinstance(row, dict) and _valid_model_decision(row.get("decision"))
    ]
    if "anomaly" in valid_model_decisions:
        model_detected = True
    elif (
        scenario.get("status") == "complete"
        and bool(scores)
        and len(valid_model_decisions) == len(scores)
        and all(row.get("decision") == "normal" and _finite_number(row.get("score")) for row in scores)
    ):
        model_detected = False
    else:
        model_detected = None

    valid_rule_found = any(
        isinstance(row, dict)
        and isinstance(row.get("matches"), list)
        and bool(row["matches"])
        and all(_valid_threshold_match(match) for match in row["matches"])
        for row in thresholds
    )
    all_rules_empty_and_valid = (
        scenario.get("status") == "complete"
        and bool(thresholds)
        and all(
            isinstance(row, dict)
            and isinstance(row.get("matches"), list)
            and not row["matches"]
            for row in thresholds
        )
    )
    if valid_rule_found:
        rules_detected = True
    elif all_rules_empty_and_valid:
        rules_detected = False
    else:
        rules_detected = None

    anomaly_records = scenario.get("anomalies")
    anomaly_records = anomaly_records if isinstance(anomaly_records, list) else []
    unique_anomaly_count, duplicate_anomaly_count, missing_anomaly_id_count = (
        _anomaly_record_counts(anomaly_records)
    )
    fault_requests = scenario.get("http_results")
    fault_requests = fault_requests if isinstance(fault_requests, list) else []
    first_request = min(
        (parsed for item in fault_requests
         if isinstance(item, dict)
         for parsed in [_parse_utc(item.get("request_started_at"))]
         if parsed is not None),
        default=None,
    )
    first_detection = min(
        (parsed for item in anomaly_records
         if isinstance(item, dict)
         for parsed in [_parse_utc(item.get("detected_at"))]
         if parsed is not None),
        default=None,
    )
    latency_ms = None
    if first_request is not None and first_detection is not None:
        elapsed = (first_detection - first_request).total_seconds() * 1000
        if elapsed >= 0:
            latency_ms = round(elapsed, 3)
    if model_detected is True or rules_detected is True:
        combined_detected = True
    elif model_detected is False and rules_detected is False:
        combined_detected = False
    else:
        combined_detected = None
    return {
        "isolation_forest_detected": model_detected,
        "threshold_rules_detected": rules_detected,
        "either_mechanism_detected": combined_detected,
        "scoring_rows": len(scores),
        "threshold_rows": len(thresholds),
        "valid_model_scores": [
            {"service_id": row["service_id"], "window_start": row["window_start"], "score": row["score"]}
            for row in reported_scores if row["score"] is not None
        ],
        "reported_model_rows": reported_scores,
        "reported_threshold_rows": reported_thresholds,
        "anomaly_record_count": unique_anomaly_count,
        "anomaly_record_count_semantics": (
            "unique usable anomaly_id values; entries with missing or invalid IDs are excluded "
            "and counted separately"
        ),
        "duplicate_anomaly_record_count": duplicate_anomaly_count,
        "anomaly_records_without_id_count": missing_anomaly_id_count,
        "anomaly_detection_latency_ms": latency_ms,
    }


def _investigation_outcome(investigation: dict[str, Any]) -> str:
    status = investigation.get("status")
    candidates = []
    for field in ("root_causes", "ranked_candidates"):
        values = investigation.get(field)
        if isinstance(values, list):
            candidates.extend(item for item in values if isinstance(item, dict))
    if status == "completed" and any(
        item.get("selected_as_probable_root_cause") is True
        and (bool(item.get("evidence")) or bool(item.get("supporting_signals")))
        for item in candidates
    ):
        return "probable"
    if (
        status == "inconclusive"
        and isinstance(investigation.get("ranked_candidates"), list)
        and isinstance(investigation.get("root_causes"), list)
        and not investigation["root_causes"]
        and not any(item.get("selected_as_probable_root_cause") is True for item in candidates)
    ):
        return "inconclusive"
    return "unavailable"


def _rca_metrics(scenario: dict[str, Any]) -> dict[str, Any]:
    investigations = scenario.get("rca_investigations")
    investigations = investigations if isinstance(investigations, list) else []
    triggered = scenario.get("rca_triggered")
    processing_complete = scenario.get("rca_processing_complete")
    details_complete = bool(investigations) and all(
        isinstance(item, dict)
        and isinstance(item.get("status"), str)
        and item.get("status") in {"completed", "inconclusive"}
        and isinstance(item.get("ranked_candidates"), list)
        for item in investigations
    )
    complete = bool(triggered) and bool(processing_complete) and details_complete
    valid_investigations = [item for item in investigations if isinstance(item, dict)]
    outcomes = [_investigation_outcome(item) for item in valid_investigations]
    statuses = [item.get("status") for item in valid_investigations]
    if any(outcome == "probable" for outcome in outcomes):
        outcome = "probable"
    elif outcomes and all(item == "inconclusive" for item in outcomes):
        outcome = "inconclusive"
    else:
        outcome = "unavailable"
    return {
        "investigation_triggered": triggered if isinstance(triggered, bool) else None,
        "investigation_completed": complete if isinstance(triggered, bool) else None,
        "investigation_status": statuses[0] if len(statuses) == 1 else None,
        "investigation_statuses": statuses,
        "outcome": outcome,
        "outcomes": outcomes,
        "probable_count": outcomes.count("probable"),
        "inconclusive_count": outcomes.count("inconclusive"),
        "unavailable_outcome_count": outcomes.count("unavailable"),
        "investigations": investigations,
    }


def _recovery_metrics(scenario: dict[str, Any]) -> dict[str, Any]:
    recovery = scenario.get("recovery_http")
    started = _parse_utc(scenario.get("recovery_started_at"))
    completed = _parse_utc(scenario.get("recovery_completed_at"))
    latency_ms = None
    if scenario.get("recovery_succeeded") is True and started is not None and completed is not None:
        elapsed = (completed - started).total_seconds() * 1000
        if elapsed >= 0:
            latency_ms = round(elapsed, 3)
    return {
        "succeeded": scenario.get("recovery_succeeded"),
        "health_checks": scenario.get("recovery_health"),
        "business_request": recovery,
        "recovery_time_ms": latency_ms,
        "recovery_time_semantics": (
            "elapsed from explicit fault stop to successful recovery verification; upper bound"
            if latency_ms is not None else None
        ),
    }


def _missing_measurements(scenario: dict[str, Any], detection: dict[str, Any],
                          rca: dict[str, Any], recovery: dict[str, Any]) -> list[str]:
    missing = []
    if not _http_results(scenario):
        missing.append("http_results")
    if "observed_error_rates_and_latency" not in scenario:
        missing.append("telemetry_metrics")
    if detection["isolation_forest_detected"] is None:
        missing.append("isolation_forest_scores_or_decisions")
    if detection["threshold_rules_detected"] is None:
        missing.append("threshold_rule_matches")
    if detection["anomaly_detection_latency_ms"] is None:
        missing.append("anomaly_detection_latency")
    if not rca["investigations"]:
        missing.append("rca_investigation_details")
    if recovery["succeeded"] is None:
        missing.append("recovery_outcome")
    if recovery["recovery_time_ms"] is None:
        missing.append("recovery_time")
    return missing


def _scenario_artifact(scenario: dict[str, Any], expected_label: Any) -> dict[str, Any]:
    detection = _detection_metrics(scenario)
    rca = _rca_metrics(scenario)
    recovery = _recovery_metrics(scenario)
    http = _http_results(scenario)
    outcomes = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "network_or_unknown": 0}
    for item in http:
        code = item.get("status_code")
        if isinstance(code, int) and 200 <= code < 300:
            outcomes["2xx"] += 1
        elif isinstance(code, int) and 300 <= code < 400:
            outcomes["3xx"] += 1
        elif isinstance(code, int) and 400 <= code < 500:
            outcomes["4xx"] += 1
        elif isinstance(code, int) and 500 <= code < 600:
            outcomes["5xx"] += 1
        else:
            outcomes["network_or_unknown"] += 1
    metrics = scenario.get("observed_error_rates_and_latency")
    reported_model_rows = detection.pop("reported_model_rows")
    reported_threshold_rows = detection.pop("reported_threshold_rows")
    leak_checks = scenario.get("expected_label_leak_checks")
    leak_checks = {
        source: leak_checks.get(source) if isinstance(leak_checks, dict) else None
        for source in EXPECTED_LABEL_LEAK_CHECK_SOURCES
    }
    return {
        "scenario_id": scenario.get("scenario_id"),
        "expected_scenario_label": expected_label,
        "status": scenario.get("status", "incomplete"),
        "reason": scenario.get("reason"),
        "run_id": scenario.get("run_id"),
        "http": {
            "baseline_requests": scenario.get("baseline_http", []),
            "fault_requests": scenario.get("http_results", []),
            "recovery_request": scenario.get("recovery_http"),
            "outcome_counts": outcomes,
        },
        "fault_observation": {
            "activation_verified": scenario.get("fault_activation_verified"),
            "behavior_verified": scenario.get("fault_behavior_verified"),
            "stopped_at": scenario.get("fault_stopped_at"),
        },
        "telemetry": {
            "received": scenario.get("telemetry_received"),
            "processed": scenario.get("telemetry_processed"),
            "record_count": scenario.get("telemetry_records"),
            "affected_services_observed": scenario.get("affected_service_ids_observed", []),
            "service_metrics": metrics if isinstance(metrics, list) else None,
            "trace_and_dependency_evidence": scenario.get("trace_and_dependency_evidence"),
        },
        "detection": {
            **detection,
            "isolation_forest_scores_and_decisions": reported_model_rows,
            "threshold_rule_matches": reported_threshold_rows,
            "anomaly_records": scenario.get("anomalies", []),
        },
        "expected_label_leak_checks": leak_checks,
        "rca": rca,
        "recovery": recovery,
        "missing_measurements": _missing_measurements(scenario, detection, rca, recovery),
    }


def _summarize(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    completed = sum(item.get("status") == "complete" for item in scenarios)
    detection = [item["detection"] for item in scenarios]
    rca = [item["rca"] for item in scenarios]
    recovery = [item["recovery"] for item in scenarios]
    latencies = [
        item["anomaly_detection_latency_ms"] for item in detection
        if item["anomaly_detection_latency_ms"] is not None
    ]
    recovery_times = [
        item["recovery_time_ms"] for item in recovery
        if item["recovery_time_ms"] is not None
    ]
    http_counts = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "network_or_unknown": 0}
    for scenario in scenarios:
        for key, count in scenario["http"]["outcome_counts"].items():
            http_counts[key] += count
    def detection_count(key: str) -> dict[str, int]:
        values = [item[key] for item in detection if item[key] is not None]
        return {"detected": sum(values), "measured_scenarios": len(values)}

    return {
        "scenario_count": len(scenarios),
        "completed_scenarios": completed,
        "failed_or_incomplete_scenarios": len(scenarios) - completed,
        "http_request_count": sum(http_counts.values()),
        "http_outcome_counts": http_counts,
        "detection": {
            "isolation_forest": detection_count("isolation_forest_detected"),
            "threshold_rules": detection_count("threshold_rules_detected"),
            "either_mechanism": detection_count("either_mechanism_detected"),
            "anomaly_records": sum(item["anomaly_record_count"] for item in detection),
            "duplicate_anomaly_records": sum(
                item["duplicate_anomaly_record_count"] for item in detection
            ),
            "anomaly_records_without_id": sum(
                item["anomaly_records_without_id_count"] for item in detection
            ),
            "anomaly_detection_latency_ms": {
                "measurable_scenarios": len(latencies),
                "mean": round(mean(latencies), 3) if latencies else None,
                "values": latencies,
            },
        },
        "rca": {
            "triggered_scenarios": sum(item["investigation_triggered"] is True for item in rca),
            "completed_scenarios": sum(item["investigation_completed"] is True for item in rca),
            "probable_outcomes": sum(item["probable_count"] for item in rca),
            "inconclusive_outcomes": sum(item["inconclusive_count"] for item in rca),
            "unavailable_outcomes": sum(item["unavailable_outcome_count"] for item in rca),
        },
        "recovery": {
            "successful_scenarios": sum(item["succeeded"] is True for item in recovery),
            "measured_scenarios": sum(item["succeeded"] is not None for item in recovery),
            "recovery_time_ms": {
                "measurable_scenarios": len(recovery_times),
                "mean": round(mean(recovery_times), 3) if recovery_times else None,
                "values": recovery_times,
            },
        },
    }


def build_evaluation_artifact(
    scenario_results: list[dict[str, Any]],
    expected_labels: dict[str, Any],
    started_at: str,
    completed_at: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create metrics from captured results; labels are attached only after capture."""
    scenarios = [
        _scenario_artifact(item, expected_labels.get(item.get("scenario_id")))
        for item in scenario_results
    ]
    return {
        "evaluation_run_id": run_id or new_evaluation_run_id(),
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "summary": _summarize(scenarios),
        "scenarios": scenarios,
        "limitations": LIMITATIONS,
    }


def render_summary(artifact: dict[str, Any]) -> str:
    summary = artifact["summary"]
    lines = [
        "# Phase 8 evaluation summary",
        "",
        f"- Run ID: `{artifact['evaluation_run_id']}`",
        f"- Started (UTC): {artifact['started_at_utc']}",
        f"- Completed (UTC): {artifact['completed_at_utc']}",
        f"- Scenarios: {summary['scenario_count']} total; {summary['completed_scenarios']} completed; "
        f"{summary['failed_or_incomplete_scenarios']} failed or incomplete.",
        f"- HTTP requests: {summary['http_request_count']} "
        f"({', '.join(f'{key}={value}' for key, value in summary['http_outcome_counts'].items())}).",
        "",
        "## Detection",
        "",
    ]
    for name, key in (
        ("Isolation Forest", "isolation_forest"),
        ("Threshold rules", "threshold_rules"),
        ("Either mechanism", "either_mechanism"),
    ):
        metric = summary["detection"][key]
        lines.append(f"- {name}: detected in {metric['detected']} of {metric['measured_scenarios']} measured scenarios.")
    latency = summary["detection"]["anomaly_detection_latency_ms"]
    lines.extend([
        f"- Anomaly records: {summary['detection']['anomaly_records']} unique IDs; "
        f"{summary['detection']['duplicate_anomaly_records']} duplicate entries and "
        f"{summary['detection']['anomaly_records_without_id']} entries without usable IDs.",
        f"- Anomaly detection latency: {latency['mean']} ms mean across "
        f"{latency['measurable_scenarios']} measurable scenarios.",
        "",
        "## RCA and recovery",
        "",
        f"- RCA: {summary['rca']['triggered_scenarios']} scenarios triggered investigation; "
        f"{summary['rca']['completed_scenarios']} completed; "
        f"{summary['rca']['probable_outcomes']} probable and "
        f"{summary['rca']['inconclusive_outcomes']} inconclusive and "
        f"{summary['rca']['unavailable_outcomes']} unavailable outcome(s).",
        f"- Recovery: {summary['recovery']['successful_scenarios']} successful of "
        f"{summary['recovery']['measured_scenarios']} measured scenarios; mean measured time "
        f"{summary['recovery']['recovery_time_ms']['mean']} ms.",
        "",
        "## Scenario results",
        "",
    ])
    for scenario in artifact["scenarios"]:
        detection = scenario["detection"]
        lines.append(
            f"- `{scenario['scenario_id']}`: {scenario['status']}; "
            f"HTTP {scenario['http']['outcome_counts']}; "
            f"Isolation Forest={detection['isolation_forest_detected']}, "
            f"thresholds={detection['threshold_rules_detected']}, "
            f"either={detection['either_mechanism_detected']}; "
            f"RCA status={scenario['rca']['investigation_status']}, "
            f"outcome={scenario['rca']['outcome']}; "
            f"recovery={scenario['recovery']['succeeded']}."
        )
        if scenario["missing_measurements"]:
            lines.append(f"  Missing/unavailable: {', '.join(scenario['missing_measurements'])}.")
        unavailable_checks = [
            source for source, value in scenario["expected_label_leak_checks"].items()
            if value is None
        ]
        if unavailable_checks:
            lines.append(f"  Expected-label checks unavailable: {', '.join(unavailable_checks)}.")
        elif any(scenario["expected_label_leak_checks"].values()):
            lines.append("  Expected-label leakage detected in an observed source.")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in artifact["limitations"])
    return "\n".join(lines) + "\n"


def write_evaluation_artifact(
    artifact: dict[str, Any], output_root: Path | str
) -> tuple[Path, Path]:
    """Write JSON and summary under a newly-created run directory without overwrite."""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    run_directory = root / artifact["evaluation_run_id"]
    run_directory.mkdir(exist_ok=False)
    json_path = run_directory / "evaluation.json"
    summary_path = run_directory / "summary.md"
    encoded = json.dumps(artifact, sort_keys=True, indent=2, allow_nan=False) + "\n"
    with json_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
    with summary_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(render_summary(artifact))
    return json_path, summary_path
