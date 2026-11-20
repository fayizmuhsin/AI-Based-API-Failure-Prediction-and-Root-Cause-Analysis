"""Threshold and Isolation Forest detection over completed service windows."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from app.anomaly.features import FEATURE_NAMES, FeatureExtractor, ServiceWindowFeatures
from app.anomaly.model import IsolationForestModel
from app.anomaly.repository import AnomalyRepository
from app.anomaly.rules import ThresholdRules
from app.core.config import Settings
from app.telemetry.storage import TelemetryRepository

logger = logging.getLogger(__name__)


class AnomalyDetectionService:
    def __init__(
        self,
        telemetry: TelemetryRepository,
        repository: AnomalyRepository,
        settings: Settings,
    ):
        self.telemetry = telemetry
        self.repository = repository
        self.settings = settings
        self.extractor = FeatureExtractor()
        self.rules = ThresholdRules(settings)
        self.model = IsolationForestModel(settings)
        self.model_load_failed = False

    def load_active_model(self) -> None:
        try:
            stored = self.repository.active_model()
            if stored:
                self.model.activate(stored["model_blob"], stored["metadata"])
                logger.info("Loaded Isolation Forest model %s", stored["metadata"]["model_version"])
            self.model_load_failed = False
        except Exception:
            self.model_load_failed = True
            logger.exception("Could not load saved Isolation Forest; threshold detection remains enabled")

    def process_batch(self, limit: int | None = None) -> int:
        now = datetime.now(timezone.utc) - timedelta(seconds=self.settings.anomaly_window_lateness_seconds)
        cutoff = now.replace(second=0, microsecond=0)
        keys = self.repository.completed_window_keys(
            cutoff,
            limit=limit or self.settings.anomaly_detection_batch_size,
        )
        if not keys:
            return 0
        grouped = self._group_records(self.repository.records_for_windows(keys))
        processed = 0
        for service_id, window_start in keys:
            records = grouped.get((service_id, window_start), [])
            history = self.repository.historical_aggregates(service_id, window_start)
            feature_window = self.extractor.extract(service_id, window_start, records, history)
            threshold_matches = self.rules.evaluate(feature_window)
            model_result = self._score_safely(feature_window)
            is_anomaly = bool(threshold_matches or model_result["decision"] == "anomaly")
            severity = self._severity(threshold_matches, model_result["decision"])
            inserted, _ = self.repository.record_detection(
                service_id=service_id,
                window_start=window_start,
                model_version=self.model.metadata["model_version"] if self.model.metadata else None,
                model_score=model_result["score"],
                model_decision=model_result["decision"],
                threshold_matches=threshold_matches,
                feature_values=feature_window.values,
                is_anomaly=is_anomaly,
                severity=severity,
            )
            processed += int(inserted)
            if is_anomaly and inserted:
                logger.warning(
                    "Anomaly detected for %s window %s (model=%s rules=%s)",
                    service_id, window_start, model_result["decision"],
                    [match["rule"] for match in threshold_matches],
                )
        return processed

    def train(self, history_limit: int = 5000) -> dict[str, Any]:
        """Explicit controlled training from completed, observed healthy windows."""
        now = datetime.now(timezone.utc) - timedelta(seconds=self.settings.anomaly_window_lateness_seconds)
        cutoff = now.replace(second=0, microsecond=0)
        keys = self.repository.historical_window_keys(cutoff, limit=history_limit)
        known_anomalies = self.repository.known_anomalous_windows()
        healthy_rows: list[dict[str, float | None]] = []
        healthy_keys: list[tuple[str, str]] = []
        for offset in range(0, len(keys), self.settings.anomaly_detection_batch_size):
            chunk = keys[offset:offset + self.settings.anomaly_detection_batch_size]
            grouped = self._group_records(self.repository.records_for_windows(chunk))
            for service_id, window_start in chunk:
                if (service_id, window_start) in known_anomalies:
                    continue
                feature_window = self.extractor.extract(
                    service_id,
                    window_start,
                    grouped.get((service_id, window_start), []),
                    self.repository.historical_aggregates(service_id, window_start),
                )
                matches = self.rules.evaluate(feature_window)
                if (
                    feature_window.values["request_count"] is not None
                    and feature_window.values["request_count"] >= self.settings.anomaly_min_training_requests
                    and self.rules.healthy_for_training(feature_window, matches)
                ):
                    healthy_rows.append(feature_window.values)
                    healthy_keys.append((service_id, window_start))

        sample_count = len(healthy_rows)
        required = self.settings.anomaly_min_training_samples
        if sample_count < required:
            return {
                "state": "not_ready",
                "training_sample_count": sample_count,
                "required_training_samples": required,
                "reason": "Not enough threshold-clean historical service windows to train.",
            }
        model_bytes, metadata = self.model.fit(healthy_rows)
        training_end = max((key[1] for key in healthy_keys), default=None)
        self.repository.save_model(model_bytes, metadata, training_end)
        logger.info(
            "Trained Isolation Forest %s on %d threshold-clean observed windows",
            metadata["model_version"], sample_count,
        )
        return {"state": "ready", **metadata, "training_data_end": training_end}

    def model_status(self) -> dict[str, Any]:
        metadata = self.repository.model_status()
        if metadata and metadata["state"] == "ready" and self.model.ready:
            return {
                "ready": True,
                "state": "ready",
                "algorithm": "IsolationForest",
                **metadata,
            }
        return {
            "ready": False,
            "state": "not_ready",
            "algorithm": "IsolationForest",
            "feature_names": list(FEATURE_NAMES),
            "required_training_samples": self.settings.anomaly_min_training_samples,
            "contamination": self.settings.anomaly_contamination,
            "reason": (
                "The saved model could not be loaded; threshold detection remains enabled."
                if self.model_load_failed
                else "No trained model is available. Train explicitly after enough healthy telemetry windows accumulate."
            ),
        }

    def _score_safely(self, window: ServiceWindowFeatures) -> dict[str, Any]:
        try:
            return self.model.score(window.values)
        except Exception:
            # Threshold rules still run and persist their evidence when model scoring fails.
            logger.exception("Isolation Forest scoring failed for %s %s", window.service_id, window.window_start)
            return {"score": None, "decision": "error"}

    @staticmethod
    def _severity(matches: list[dict], model_decision: str) -> str:
        rule_names = {item["rule"] for item in matches}
        if "high_error_rate" in rule_names and any(
            item["observed"] >= 0.8 for item in matches if item["rule"] == "high_error_rate"
        ):
            return "critical"
        if len(matches) >= 2 or (matches and model_decision == "anomaly"):
            return "high"
        return "medium" if matches else "low"

    @staticmethod
    def _group_records(records: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        if not records:
            return grouped
        frame = pd.DataFrame.from_records(records)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"])
        frame["window_start"] = frame["timestamp"].dt.floor("min").map(lambda value: value.isoformat())
        for row in frame.to_dict(orient="records"):
            grouped[(row["service_id"], row["window_start"])].append(row)
        return grouped
