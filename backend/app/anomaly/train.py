"""Explicit model training command; run from the backend directory."""

import json
import logging

from app.anomaly.runtime import anomaly_repository, anomaly_service
from app.api.routes.telemetry import telemetry_repository
from app.core.logging import configure_logging


def main() -> None:
    configure_logging()
    telemetry_repository.initialize()
    anomaly_repository.initialize()
    anomaly_service.load_active_model()
    result = anomaly_service.train()
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["state"] != "ready":
        logging.getLogger(__name__).warning(
            "Isolation Forest remains not ready; threshold-based detections remain enabled."
        )


if __name__ == "__main__":
    main()
