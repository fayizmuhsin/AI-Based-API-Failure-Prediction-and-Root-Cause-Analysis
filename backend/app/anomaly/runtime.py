"""Application-scoped anomaly service, loaded safely after backend startup."""

import logging
from typing import Any

from app.anomaly.repository import AnomalyRepository
from app.api.routes.telemetry import telemetry_repository
from app.core.config import settings

logger = logging.getLogger(__name__)

anomaly_repository = AnomalyRepository(
    settings.monitoring_database_path,
    settings.application_database_path,
)

class AnomalyServiceRuntime:
    """Lazy ML loading keeps health and ingestion available if native ML modules fail."""

    def __init__(self):
        self._service = None
        self._load_error = False

    def load_active_model(self) -> None:
        if self._service is None:
            try:
                from app.anomaly.engine import AnomalyDetectionService

                self._service = AnomalyDetectionService(
                    telemetry_repository,
                    anomaly_repository,
                    settings,
                )
            except Exception:
                self._load_error = True
                logger.exception(
                    "Anomaly dependencies could not be loaded; telemetry storage and health remain available"
                )
                return
        self._service.load_active_model()

    def process_batch(self) -> int:
        if self._service is None:
            return 0
        return self._service.process_batch()

    def model_status(self) -> dict[str, Any]:
        if self._service is not None:
            return self._service.model_status()
        if self._load_error:
            return {
                "ready": False,
                "state": "not_ready",
                "algorithm": "IsolationForest",
                "reason": "Anomaly dependencies could not be loaded; telemetry ingestion remains available.",
            }
        metadata = anomaly_repository.model_status()
        if metadata and metadata["state"] == "ready":
            return {
                "ready": False,
                "state": "not_ready",
                "algorithm": "IsolationForest",
                **metadata,
                "reason": "Anomaly model components have not initialized.",
            }
        return {
            "ready": False,
            "state": "not_ready",
            "algorithm": "IsolationForest",
            "reason": "No trained model is available. Train explicitly after enough healthy telemetry windows accumulate.",
            "required_training_samples": settings.anomaly_min_training_samples,
            "contamination": settings.anomaly_contamination,
        }

    def train(self) -> dict[str, Any]:
        if self._service is None:
            self.load_active_model()
        if self._service is None:
            return {
                "state": "not_ready",
                "training_sample_count": 0,
                "required_training_samples": settings.anomaly_min_training_samples,
                "reason": "Anomaly dependencies could not be loaded.",
            }
        return self._service.train()


anomaly_service = AnomalyServiceRuntime()
