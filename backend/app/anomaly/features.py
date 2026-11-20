"""Build UTC service-minute features from persisted metric observations."""

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


FEATURE_NAMES = (
    "request_count",
    "successful_request_count",
    "failed_request_count",
    "error_rate",
    "timeout_count",
    "average_latency_ms",
    "median_latency_ms",
    "p95_latency_ms",
    "latency_deviation_from_baseline_ms",
    "request_count_change",
    "dependency_failure_rate",
)


@dataclass(frozen=True)
class ServiceWindowFeatures:
    service_id: str
    window_start: str
    values: dict[str, float | None]
    dependency_request_count: int
    previous_request_count: int | None
    baseline_window_count: int


class FeatureExtractor:
    """Extracts one completed service window without fabricating observations."""

    def extract(
        self,
        service_id: str,
        window_start: str,
        records: list[dict[str, Any]],
        historical_windows: list[dict[str, Any]],
    ) -> ServiceWindowFeatures:
        frame = pd.DataFrame.from_records(records)
        if frame.empty:
            frame = pd.DataFrame(columns=[
                "type", "metric_name", "timestamp", "status_code", "duration_ms",
                "timed_out", "dependency",
            ])
        if "type" not in frame:
            frame["type"] = "metric"
        frame = frame[frame["type"] == "metric"].copy()
        if "timestamp" in frame and not frame.empty:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
            frame = frame.dropna(subset=["timestamp"])
            frame["window_start"] = frame["timestamp"].dt.floor("min").map(lambda value: value.isoformat())
            target = pd.Timestamp(window_start).tz_convert("UTC").floor("min")
            frame = frame[frame["window_start"] == target.isoformat()]

        metric_names = frame.get("metric_name", pd.Series(index=frame.index, dtype="object"))
        server = frame[metric_names == "http.server.request"].copy()
        client = frame[metric_names == "http.client.request"].copy()
        status = pd.to_numeric(server.get("status_code"), errors="coerce")
        timeout = server.get("timed_out", pd.Series(False, index=server.index)).fillna(False).astype(bool)
        request_count = int(len(server))
        failed = (status.ge(400).fillna(False) | timeout) if request_count else pd.Series(dtype=bool)
        successful = status.between(200, 399).fillna(False) & ~timeout if request_count else pd.Series(dtype=bool)
        durations = pd.to_numeric(server.get("duration_ms"), errors="coerce")
        durations = durations.replace([np.inf, -np.inf], np.nan).dropna()
        average_latency = float(durations.mean()) if len(durations) else None
        median_latency = float(durations.median()) if len(durations) else None
        p95_latency = float(np.percentile(durations.to_numpy(dtype=float), 95)) if len(durations) else None

        dependency_requests = 0
        dependency_failure_rate = None
        if not client.empty:
            dependency = client.get("dependency", pd.Series(index=client.index, dtype="object"))
            dependency_client = client[dependency.notna()]
            dependency_requests = int(len(dependency_client))
            if dependency_requests:
                client_status = pd.to_numeric(dependency_client.get("status_code"), errors="coerce")
                client_timeout = dependency_client.get(
                    "timed_out", pd.Series(False, index=dependency_client.index)
                ).fillna(False).astype(bool)
                failures = client_status.ge(400).fillna(False) | client_timeout
                dependency_failure_rate = float(failures.sum() / dependency_requests)

        history = pd.DataFrame.from_records(historical_windows)
        if history.empty:
            history = pd.DataFrame(columns=["window_start", "record_count", "total_latency_ms"])
        else:
            history["window_start"] = pd.to_datetime(history["window_start"], utc=True, errors="coerce")
            history = history.dropna(subset=["window_start"]).sort_values("window_start", ascending=False)
            history["record_count"] = pd.to_numeric(history["record_count"], errors="coerce").fillna(0)
            history["total_latency_ms"] = pd.to_numeric(history["total_latency_ms"], errors="coerce").fillna(0)
            history = history[history["window_start"] < pd.Timestamp(window_start).tz_convert("UTC")]
            history = history[history["record_count"] > 0].head(60)

        history_counts = history["record_count"].to_numpy(dtype=float)
        history_latency_totals = history["total_latency_ms"].to_numpy(dtype=float)
        history_averages = np.divide(
            history_latency_totals,
            history_counts,
            out=np.full(len(history), np.nan, dtype=float),
            where=history_counts > 0,
        )
        valid_baseline = history_averages[np.isfinite(history_averages)]
        baseline_window_count = int(len(valid_baseline))
        baseline_latency = float(np.median(valid_baseline)) if baseline_window_count else None
        latency_deviation = (
            average_latency - baseline_latency
            if average_latency is not None and baseline_latency is not None else None
        )
        previous_request_count = int(history_counts[0]) if baseline_window_count else None
        request_change = (
            (request_count - previous_request_count) / max(previous_request_count, 1)
            if previous_request_count is not None else None
        )

        values: dict[str, float | None] = {
            "request_count": float(request_count),
            "successful_request_count": float(successful.sum()) if request_count else 0.0,
            "failed_request_count": float(failed.sum()) if request_count else 0.0,
            "error_rate": float(failed.sum() / request_count) if request_count else 0.0,
            "timeout_count": float(timeout.sum()) if request_count else 0.0,
            "average_latency_ms": average_latency,
            "median_latency_ms": median_latency,
            "p95_latency_ms": p95_latency,
            "latency_deviation_from_baseline_ms": latency_deviation,
            "request_count_change": request_change,
            "dependency_failure_rate": dependency_failure_rate,
        }
        return ServiceWindowFeatures(
            service_id=service_id,
            window_start=pd.Timestamp(window_start).tz_convert("UTC").floor("min").isoformat(),
            values=values,
            dependency_request_count=dependency_requests,
            previous_request_count=previous_request_count,
            baseline_window_count=baseline_window_count,
        )
