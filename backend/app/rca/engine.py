"""Observed-evidence correlation and explainable root-cause ranking."""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.rca.repository import RcaRepository

logger = logging.getLogger(__name__)

SERVICE_NAMES = {
    "api-gateway": "API Gateway",
    "user-service": "User Service",
    "order-service": "Order Service",
    "database-service": "Database Service",
}


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ServiceTopology:
    """Caller-to-dependency graph built from the approved JSON configuration."""

    def __init__(self, config_path: Path):
        data = json.loads(config_path.read_text(encoding="utf-8"))
        services = data.get("services")
        dependencies = data.get("dependencies")
        if not isinstance(services, list) or not isinstance(dependencies, list):
            raise ValueError("Service topology must define services and dependencies arrays")
        self.service_ids = {item["id"] for item in services if isinstance(item, dict) and item.get("id")}
        self.graph: dict[str, set[str]] = {service_id: set() for service_id in self.service_ids}
        for edge in dependencies:
            if not isinstance(edge, dict):
                raise ValueError("Service dependency entry is malformed")
            caller, dependency = edge.get("caller"), edge.get("dependency")
            if caller not in self.service_ids or dependency not in self.service_ids:
                raise ValueError("Service dependency references an unknown service")
            self.graph[caller].add(dependency)
        self.reverse: dict[str, set[str]] = {service_id: set() for service_id in self.service_ids}
        for caller, dependencies_for_caller in self.graph.items():
            for dependency in dependencies_for_caller:
                self.reverse[dependency].add(caller)

    def affected_upstream_callers(self, failed_service: str) -> list[str]:
        """Traverse reverse edges: failures flow from a dependency to its callers."""
        if failed_service not in self.service_ids:
            return []
        visited: set[str] = set()
        pending = deque(self.reverse[failed_service])
        while pending:
            caller = pending.popleft()
            if caller in visited:
                continue
            visited.add(caller)
            pending.extend(self.reverse[caller] - visited)
        return sorted(visited)


class RootCauseAnalysisEngine:
    def __init__(self, repository: RcaRepository, settings: Settings):
        self.repository = repository
        self.settings = settings
        self.topology = ServiceTopology(settings.service_config_path)

    def process_batch(self, limit: int | None = None) -> int:
        self.repository.enqueue_new_anomalies()
        pending = self.repository.pending_anomalies(limit or self.settings.rca_processing_batch_size)
        if not pending:
            return 0
        groups = self._episodes(pending)
        completed = 0
        for group in groups:
            ids = [int(item["anomaly_id"]) for item in group]
            try:
                report = self.investigate(group)
                completed += int(self.repository.persist_investigation(report, ids))
            except Exception as exc:
                logger.exception("RCA processing failed for anomaly IDs %s; queued for retry", ids)
                try:
                    self.repository.record_attempt_failure(ids, f"{type(exc).__name__}: {exc}")
                except Exception:
                    logger.exception("Could not persist RCA retry state for anomaly IDs %s", ids)
        return completed

    def investigate(self, anomalies: list[dict[str, Any]]) -> dict[str, Any]:
        if not anomalies:
            raise ValueError("An RCA episode requires at least one triggering anomaly")
        ordered = sorted(anomalies, key=lambda item: (item["window_start"], item["anomaly_id"]))
        episode_start = parse_time(ordered[0]["window_start"])
        episode_end = parse_time(ordered[-1]["window_start"]) + timedelta(minutes=1)
        padding = timedelta(seconds=self.settings.rca_correlation_window_seconds)
        observations = self.repository.telemetry_between(
            (episode_start - padding).isoformat(), (episode_end + padding).isoformat(),
        )
        builders: dict[str, dict[str, Any]] = {}
        self._add_anomaly_signals(builders, ordered)
        self._add_observed_signals(builders, observations)

        anomalous_services = {item["service_id"] for item in ordered}
        all_signal_times = [
            parse_time(evidence["timestamp"])
            for builder in builders.values()
            for evidence in builder["evidence"]
            if evidence.get("timestamp")
        ]
        first_signal = min(all_signal_times, default=episode_start)
        last_signal = max(all_signal_times, default=episode_end)
        total_span = max((last_signal - first_signal).total_seconds(), 1.0)

        candidates = []
        for service_id, builder in builders.items():
            upstream = self.topology.affected_upstream_callers(service_id)
            affected = sorted({service_id, *upstream})
            overlap = len(set(affected) & anomalous_services)
            impact_points = 12.0 * overlap / max(1, len(anomalous_services))
            topology_impact_points = min(8.0, max(0, len(affected) - 1) * 2.0)
            earliest = min(
                (parse_time(item["timestamp"]) for item in builder["evidence"] if item.get("timestamp")),
                default=episode_start,
            )
            onset_fraction = min(1.0, max(0.0, (earliest - first_signal).total_seconds() / total_span))
            onset_points = 10.0 * (1.0 - onset_fraction)
            independent_signals = len(builder["signals"])
            corroboration_points = min(9.0, max(0, independent_signals - 1) * 3.0)
            score = min(100.0, builder["points"] + impact_points + topology_impact_points
                        + onset_points + corroboration_points)
            direct_signals = builder["signals"] - {"anomaly_detection"}
            evidence_strength = self._evidence_strength(score, independent_signals)
            selected = score >= self.settings.rca_candidate_min_score and bool(direct_signals)
            category = self._category(builder["categories"])
            explanation = self._explanation(
                service_id, category, score, affected, builder["signals"], selected,
            )
            candidates.append({
                "service_id": service_id,
                "service_name": SERVICE_NAMES.get(service_id, service_id),
                "category": category,
                "rank_score": round(score, 2),
                "score_semantics": "explainable ranking points; not a failure probability",
                "evidence_strength": evidence_strength,
                "selected_as_probable_root_cause": selected,
                "supporting_signal_count": independent_signals,
                "supporting_signals": sorted(builder["signals"]),
                "score_breakdown": {
                    "observed_signal_points": round(builder["points"], 2),
                    "affected_anomalous_service_points": round(impact_points, 2),
                    "topology_impact_points": round(topology_impact_points, 2),
                    "earlier_onset_points": round(onset_points, 2),
                    "independent_signal_corroboration_points": round(corroboration_points, 2),
                    "uncapped_total": round(builder["points"] + impact_points + topology_impact_points
                                             + onset_points + corroboration_points, 2),
                },
                "affected_services": affected,
                "affected_anomalous_services": sorted(set(affected) & anomalous_services),
                "evidence": builder["evidence"][:50],
                "explanation": explanation,
            })
        candidates.sort(key=lambda item: (-item["rank_score"], item["service_id"], item["category"]))
        for rank, candidate in enumerate(candidates, start=1):
            candidate["rank"] = rank
        root_causes = [item for item in candidates if item["selected_as_probable_root_cause"]]
        root_service_ids = {item["service_id"] for item in root_causes}
        affected_services = sorted({
            service for candidate in root_causes for service in candidate["affected_services"]
        })
        correlated_trace_groups = self._correlate_trace_evidence(observations)
        return {
            "investigation_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "episode_start": episode_start.isoformat(),
            "episode_end": episode_end.isoformat(),
            "status": "completed" if root_causes else "inconclusive",
            "conclusion": (
                "One or more probable root causes are supported by observed telemetry."
                if root_causes else
                "No probable root cause met the evidence threshold; the anomalies remain unexplained."
            ),
            "triggering_anomaly_ids": [int(item["anomaly_id"]) for item in ordered],
            "triggering_anomalies": [self._public_anomaly(item) for item in ordered],
            "root_causes": root_causes,
            "candidates": candidates,
            "affected_services": affected_services,
            "evidence_summary": {
                "telemetry_records_examined": len(observations),
                "evidence_items": sum(len(item["evidence"]) for item in candidates),
                "correlated_trace_groups": correlated_trace_groups,
                "expected_simulator_metadata_used": False,
            },
            "methodology": {
                "dependency_direction": "caller -> dependency; reverse edges are traversed to find upstream callers",
                "score_range": "0-100 ranking points; not a failure probability",
                "candidate_minimum_score": self.settings.rca_candidate_min_score,
                "selection_rule": "minimum ranking points plus at least one observed telemetry signal beyond the triggering anomaly",
                "ranking_components": {
                    "observed_signal_points": "severity and observed metric, dependency, log, event, or trace weights",
                    "affected_anomalous_service_points": "up to 12 points proportional to triggered services reachable through reverse dependencies",
                    "topology_impact_points": "2 points per upstream caller, capped at 8",
                    "earlier_onset_points": "up to 10 points for the earliest observed signal in the episode",
                    "independent_signal_corroboration_points": "3 points per additional distinct signal channel, capped at 9",
                    "maximum": 100,
                },
                "signals_considered": ["metrics", "logs", "trace spans", "service events", "anomalies", "dependency failures"],
            },
        }

    def _add_anomaly_signals(self, builders: dict[str, dict[str, Any]], anomalies: list[dict]) -> None:
        severity_points = {"critical": 14.0, "high": 11.0, "medium": 8.0, "low": 4.0}
        for anomaly in anomalies:
            service_id = anomaly["service_id"]
            builder = self._builder(builders, service_id)
            builder["points"] += severity_points.get(anomaly.get("severity"), 4.0)
            builder["signals"].add("anomaly_detection")
            builder["categories"].add("anomaly_signal")
            builder["evidence"].append({
                "source": "anomaly_result",
                "reference_id": str(anomaly["anomaly_id"]),
                "timestamp": anomaly["window_start"],
                "signal": "anomaly_detection",
                "summary": (
                    f"Phase 5 detected a {anomaly.get('severity', 'unknown')} anomaly; "
                    f"model decision={anomaly.get('model_decision')}."
                ),
                "observed": {
                    "model_score": anomaly.get("model_score"),
                    "feature_values": anomaly.get("feature_values", {}),
                    "threshold_matches": anomaly.get("threshold_matches", []),
                },
            })

    def _add_observed_signals(self, builders: dict[str, dict[str, Any]], observations: list[dict]) -> None:
        service_metrics: dict[str, list[dict]] = defaultdict(list)
        dependency_calls: dict[str, list[dict]] = defaultdict(list)
        spans_by_id = {
            record.get("span_id"): record for record in observations
            if record["record_type"] == "trace" and record.get("span_id")
        }
        for record in observations:
            payload = record["payload"]
            service_id = record["service_id"]
            kind = record["record_type"]
            if kind == "metric":
                if payload.get("metric_name") == "http.server.request":
                    service_metrics[service_id].append(record)
                elif payload.get("metric_name") == "http.client.request" and payload.get("dependency"):
                    dependency_calls[payload["dependency"]].append(record)
            elif kind == "log":
                self._add_log_signal(builders, record)
            elif kind == "trace":
                self._add_trace_signal(builders, record, spans_by_id)

        for service_id, records in service_metrics.items():
            failed = [r for r in records if (r["payload"].get("status_code") or 0) >= 500]
            timed_out = [r for r in records if r["payload"].get("timed_out")]
            durations = [
                float(r["payload"]["duration_ms"]) for r in records
                if isinstance(r["payload"].get("duration_ms"), (int, float))
            ]
            if failed or timed_out:
                builder = self._builder(builders, service_id)
                rate = len({r["id"] for r in failed + timed_out}) / max(1, len(records))
                builder["points"] += min(28.0, rate * 24.0 + min(4.0, len(timed_out) * 2.0))
                builder["signals"].add("server_metrics")
                builder["categories"].add("timeout_failure" if timed_out and not failed else "service_errors")
                refs = failed + timed_out
                self._add_evidence(builder, {
                    "source": "telemetry_metric",
                    "reference_id": str(records[0]["id"]),
                    "timestamp": min(r["timestamp"] for r in refs),
                    "signal": "server_metrics",
                    "summary": f"Observed {len(failed)} server errors and {len(timed_out)} timeouts in {len(records)} requests.",
                    "observed": {"request_count": len(records), "server_error_count": len(failed),
                                 "timeout_count": len(timed_out), "failure_rate": round(rate, 4),
                                 "telemetry_record_ids": [r["id"] for r in refs[:20]]},
                    "trace_id": refs[0].get("trace_id"),
                })
            if durations:
                p95 = self._percentile(durations, 0.95)
                if p95 >= self.settings.anomaly_p95_latency_threshold_ms:
                    builder = self._builder(builders, service_id)
                    builder["points"] += 8.0
                    builder["signals"].add("latency_metrics")
                    builder["categories"].add("latency_degradation")
                    self._add_evidence(builder, {
                        "source": "telemetry_metric", "reference_id": str(records[0]["id"]),
                        "timestamp": min(r["timestamp"] for r in records), "signal": "latency_metrics",
                        "summary": f"Observed P95 latency {p95:.1f} ms exceeds the configured limit.",
                        "observed": {"p95_latency_ms": round(p95, 3),
                                     "threshold_ms": self.settings.anomaly_p95_latency_threshold_ms},
                    })

        for dependency, records in dependency_calls.items():
            failures = [
                item for item in records
                if (item["payload"].get("status_code") or 0) >= 500 or item["payload"].get("timed_out")
            ]
            if not failures or dependency not in self.topology.service_ids:
                continue
            callers = sorted({item["service_id"] for item in failures})
            rate = len(failures) / max(1, len(records))
            builder = self._builder(builders, dependency)
            builder["points"] += min(36.0, 18.0 + rate * 18.0)
            builder["signals"].add("dependency_failures")
            builder["categories"].add("dependency_failure")
            self._add_evidence(builder, {
                "source": "dependency_metric", "reference_id": str(failures[0]["id"]),
                "timestamp": min(item["timestamp"] for item in failures), "signal": "dependency_failures",
                "summary": f"Callers {', '.join(callers)} observed failures calling {SERVICE_NAMES.get(dependency, dependency)}.",
                "observed": {"dependency_service_id": dependency, "caller_service_ids": callers,
                             "failed_calls": len(failures), "call_count": len(records),
                             "failure_rate": round(rate, 4),
                             "telemetry_record_ids": [item["id"] for item in failures[:20]]},
                "trace_id": failures[0].get("trace_id"),
            })

    def _add_log_signal(self, builders: dict[str, dict[str, Any]], record: dict) -> None:
        payload = record["payload"]
        level = str(payload.get("level", "")).upper()
        event_type = str(payload.get("event_type", "")).casefold()
        error_detail = str(payload.get("error_details") or "")
        combined = f"{event_type} {error_detail}".casefold()
        markers = (
            "unhealthy", "health_check_failed", "service_down", "restart", "startup", "deploy", "release",
        )
        if level not in {"ERROR", "CRITICAL"} and not any(marker in combined for marker in markers):
            return
        builder = self._builder(builders, record["service_id"])
        category = "service_log_error"
        points = 12.0
        signal = "error_log"
        if any(marker in combined for marker in ("health", "unhealthy", "service_down")):
            category, points, signal = "health_check_failure", 22.0, "health_event"
        elif "restart" in combined or "startup" in combined:
            category, points, signal = "restart_event", 10.0, "restart_marker"
        elif "deploy" in combined or "release" in combined:
            category, points, signal = "deployment_event", 10.0, "deployment_marker"
        builder["points"] += points
        builder["signals"].add(signal)
        builder["categories"].add(category)
        self._add_evidence(builder, {
            "source": "structured_log", "reference_id": str(record["id"]),
            "timestamp": record["timestamp"], "signal": signal,
            "summary": f"Observed {level or 'event'} log: {payload.get('event_type', 'unknown event')}.",
            "observed": {"level": level, "event_type": payload.get("event_type"),
                         "error_details": payload.get("error_details"),
                         "dependency": payload.get("dependency")},
            "trace_id": record.get("trace_id"),
        })

    def _add_trace_signal(
        self, builders: dict[str, dict[str, Any]], record: dict, spans_by_id: dict[str, dict]
    ) -> None:
        payload = record["payload"]
        events = payload.get("events", [])
        exception_events = [
            event for event in events if "exception" in str(event.get("name", "")).casefold()
        ]
        if payload.get("status") != "ERROR" and not exception_events:
            return
        builder = self._builder(builders, record["service_id"])
        builder["points"] += 12.0
        builder["signals"].add("trace_errors")
        builder["categories"].add("trace_error")
        parent = spans_by_id.get(payload.get("parent_span_id"))
        parent_service = parent.get("service_id") if parent else None
        self._add_evidence(builder, {
            "source": "trace_span", "reference_id": str(record["id"]),
            "timestamp": record["timestamp"], "signal": "trace_errors",
            "summary": f"Observed an ERROR {payload.get('kind', 'trace')} span: {payload.get('name', 'unnamed span')}.",
            "observed": {"span_id": record.get("span_id"),
                         "parent_span_id": payload.get("parent_span_id"),
                         "parent_service_id": parent_service,
                         "trace_id": record.get("trace_id"),
                         "status_description": payload.get("status_description"),
                         "exception_events": exception_events[:5]},
            "trace_id": record.get("trace_id"),
        })
        if payload.get("kind") == "CLIENT":
            attributes = payload.get("attributes", {})
            target = next((attributes.get(key) for key in (
                "peer.service", "server.service", "service.name", "net.peer.name"
            ) if attributes.get(key)), None)
            dependency_id = self._service_id_for_name(target)
            if dependency_id and dependency_id in self.topology.service_ids:
                cause = self._builder(builders, dependency_id)
                cause["points"] += 18.0
                cause["signals"].add("trace_dependency_failure")
                cause["categories"].add("dependency_failure")
                self._add_evidence(cause, {
                    "source": "trace_dependency", "reference_id": str(record["id"]),
                    "timestamp": record["timestamp"], "signal": "trace_dependency_failure",
                    "summary": f"Caller {SERVICE_NAMES.get(record['service_id'], record['service_id'])} recorded an ERROR client span to {SERVICE_NAMES[dependency_id]}.",
                    "observed": {"caller_service_id": record["service_id"],
                                 "dependency_service_id": dependency_id,
                                 "span_id": record.get("span_id"), "parent_service_id": parent_service},
                    "trace_id": record.get("trace_id"),
                })

    @staticmethod
    def _service_id_for_name(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold()
        for service_id, name in SERVICE_NAMES.items():
            if normalized in {service_id.casefold(), name.casefold()}:
                return service_id
        return None

    def _builder(self, builders: dict[str, dict[str, Any]], service_id: str) -> dict[str, Any]:
        return builders.setdefault(service_id, {"points": 0.0, "signals": set(), "categories": set(), "evidence": []})

    @staticmethod
    def _add_evidence(builder: dict, evidence: dict) -> None:
        signature = (evidence.get("source"), evidence.get("reference_id"), evidence.get("signal"))
        if all((item.get("source"), item.get("reference_id"), item.get("signal")) != signature
               for item in builder["evidence"]):
            builder["evidence"].append(evidence)

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        ordered = sorted(values)
        position = (len(ordered) - 1) * quantile
        low, high = int(position), math_ceil(position)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    @staticmethod
    def _evidence_strength(score: float, signal_count: int) -> str:
        if score >= 60 and signal_count >= 3:
            return "strong"
        if score >= 30 and signal_count >= 2:
            return "moderate"
        return "limited"

    @staticmethod
    def _category(categories: set[str]) -> str:
        priority = ["health_check_failure", "dependency_failure", "service_errors", "timeout_failure",
                    "restart_event", "deployment_event", "trace_error", "service_log_error",
                    "latency_degradation", "anomaly_signal"]
        return next((item for item in priority if item in categories), "observed_anomaly")

    @staticmethod
    def _explanation(service_id: str, category: str, score: float, affected: list[str], signals: set[str], selected: bool) -> str:
        name = SERVICE_NAMES.get(service_id, service_id)
        callers = [SERVICE_NAMES.get(item, item) for item in affected if item != service_id]
        impact = f" Reverse dependency traversal identifies upstream callers: {', '.join(callers)}." if callers else " No upstream callers are defined in the configured topology."
        decision = "It is ranked as a probable cause" if selected else "It is retained as a lower-strength candidate, not selected as probable cause"
        return (
            f"{decision} with {score:.2f}/100 ranking points based on observed {category.replace('_', ' ')} "
            f"and signals ({', '.join(sorted(signals)) or 'none'}).{impact} "
            "The score is a ranking aid, not a probability or confirmation."
        )

    @staticmethod
    def _public_anomaly(item: dict) -> dict:
        return {key: item.get(key) for key in (
            "anomaly_id", "service_id", "window_start", "severity", "model_score", "model_decision",
            "threshold_matches", "feature_values",
        )}

    @staticmethod
    def _correlate_trace_evidence(observations: list[dict]) -> int:
        by_trace: dict[str, list[str]] = defaultdict(list)
        for record in observations:
            if record.get("trace_id"):
                by_trace[record["trace_id"]].append(record["record_type"])
        return sum(1 for kinds in by_trace.values() if len(kinds) > 1)

    def _episodes(self, anomalies: list[dict]) -> list[list[dict]]:
        groups: list[list[dict]] = []
        gap = timedelta(seconds=self.settings.rca_correlation_window_seconds)
        for anomaly in anomalies:
            if not groups:
                groups.append([anomaly])
                continue
            current_end = parse_time(groups[-1][-1]["window_start"])
            timestamp = parse_time(anomaly["window_start"])
            if timestamp - current_end <= gap:
                groups[-1].append(anomaly)
            else:
                groups.append([anomaly])
        return groups


def math_ceil(value: float) -> int:
    integer = int(value)
    return integer if integer == value else integer + 1
