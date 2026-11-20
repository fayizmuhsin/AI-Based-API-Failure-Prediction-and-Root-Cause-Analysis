"""Read-only endpoints for model state and persisted anomaly evidence."""

from fastapi import APIRouter, HTTPException, Path, Query

from app.anomaly.runtime import anomaly_repository, anomaly_service
from app.api.routes.telemetry import ServiceId
from app.telemetry.storage import TelemetryStorageError

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


@router.get("/model", summary="Get Isolation Forest model readiness and training metadata")
def model_status() -> dict:
    try:
        return anomaly_service.model_status()
    except TelemetryStorageError as exc:
        raise HTTPException(status_code=503, detail="Anomaly storage is unavailable") from exc


@router.get("/recent", summary="List recent persisted anomaly detections")
def recent_anomalies(
    limit: int = Query(default=100, ge=1, le=500),
    service_id: ServiceId | None = None,
) -> dict:
    try:
        items = anomaly_repository.recent_anomalies(limit, service_id)
        return {"count": len(items), "items": items}
    except TelemetryStorageError as exc:
        raise HTTPException(status_code=503, detail="Anomaly storage is unavailable") from exc


@router.get("/summary", summary="Summarize anomaly detections by service")
def anomaly_summary() -> dict:
    try:
        return anomaly_repository.anomaly_summary()
    except TelemetryStorageError as exc:
        raise HTTPException(status_code=503, detail="Anomaly storage is unavailable") from exc


@router.get("/{anomaly_id}", summary="Get anomaly features and detection evidence")
def anomaly_detail(anomaly_id: int = Path(ge=1)) -> dict:
    try:
        result = anomaly_repository.anomaly_detail(anomaly_id)
    except TelemetryStorageError as exc:
        raise HTTPException(status_code=503, detail="Anomaly storage is unavailable") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Anomaly detection was not found")
    return result
