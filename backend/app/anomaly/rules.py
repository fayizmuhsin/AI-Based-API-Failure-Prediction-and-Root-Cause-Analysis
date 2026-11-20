"""Explicit, configurable detection rules with inspectable evidence."""

import math
from typing import Any

from app.anomaly.features import ServiceWindowFeatures
from app.core.config import Settings


class ThresholdRules:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, window: ServiceWindowFeatures) -> list[dict[str, Any]]:
        values = window.values
        requests = values["request_count"] or 0.0
        matches: list[dict[str, Any]] = []

        if requests >= self.settings.anomaly_rule_min_requests:
            error_rate = values["error_rate"]
            if error_rate is not None and error_rate >= self.settings.anomaly_error_rate_threshold:
                matches.append(self._match(
                    "high_error_rate", error_rate, self.settings.anomaly_error_rate_threshold,
                    "Error rate reached the configured limit.",
                ))
            p95 = values["p95_latency_ms"]
            if p95 is not None and p95 >= self.settings.anomaly_p95_latency_threshold_ms:
                matches.append(self._match(
                    "high_p95_latency_ms", p95, self.settings.anomaly_p95_latency_threshold_ms,
                    "P95 request latency reached the configured limit.",
                ))

        timeouts = values["timeout_count"] or 0.0
        if timeouts >= self.settings.anomaly_timeout_count_threshold:
            matches.append(self._match(
                "high_timeout_count", timeouts, self.settings.anomaly_timeout_count_threshold,
                "Timed out requests reached the configured count.",
            ))

        average = values["average_latency_ms"]
        if (
            average is not None
            and window.baseline_window_count >= self.settings.anomaly_latency_spike_min_history
        ):
            baseline = average - (values["latency_deviation_from_baseline_ms"] or 0.0)
            threshold = baseline * self.settings.anomaly_latency_spike_ratio
            if baseline > 0 and average >= threshold:
                matches.append(self._match(
                    "latency_spike", average, threshold,
                    "Average latency increased sharply against the historical service baseline.",
                    baseline_latency_ms=baseline,
                    ratio=self.settings.anomaly_latency_spike_ratio,
                ))

        dependency_rate = values["dependency_failure_rate"]
        if (
            dependency_rate is not None
            and window.dependency_request_count >= self.settings.anomaly_rule_min_requests
            and dependency_rate >= self.settings.anomaly_dependency_failure_rate_threshold
        ):
            matches.append(self._match(
                "dependency_failure_rate", dependency_rate,
                self.settings.anomaly_dependency_failure_rate_threshold,
                "Observed downstream call failures reached the configured rate.",
                observations=window.dependency_request_count,
            ))

        request_change = values["request_count_change"]
        if (
            request_change is not None
            and window.previous_request_count is not None
            and window.previous_request_count >= self.settings.anomaly_rule_min_requests
            and abs(request_change) >= self.settings.anomaly_request_change_ratio
        ):
            matches.append(self._match(
                "request_volume_change", request_change,
                self.settings.anomaly_request_change_ratio,
                "Request volume changed sharply compared with the preceding observed window.",
                comparison="absolute relative change",
                previous_request_count=window.previous_request_count,
            ))
        return matches

    @staticmethod
    def _match(rule: str, observed: float, threshold: float, explanation: str, **extra) -> dict:
        return {
            "rule": rule,
            "observed": round(float(observed), 6),
            "threshold": round(float(threshold), 6),
            "explanation": explanation,
            **extra,
        }

    @staticmethod
    def healthy_for_training(window: ServiceWindowFeatures, matches: list[dict]) -> bool:
        return (
            (window.values["request_count"] or 0) > 0
            and not matches
            and all(
                value is None or math.isfinite(value)
                for value in window.values.values()
            )
        )
