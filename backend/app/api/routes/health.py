import asyncio
import json
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen
from fastapi import APIRouter, HTTPException, Request
from app.core.config import settings
router = APIRouter(tags=["system"])
SERVICE_HEALTH_ENDPOINTS = {
    "api-gateway": "http://127.0.0.1:8100/health",
    "user-service": "http://127.0.0.1:8101/health",
    "order-service": "http://127.0.0.1:8102/health",
    "database-service": "http://127.0.0.1:8103/health",
}
def _read_service_health(url: str) -> dict:
    with urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))
@router.get("/health", summary="Get backend health")
async def get_health(request: Request) -> dict[str, object]:
    """Report process health and API version; no monitoring data is implied."""
    return {
        "status": "ok",
        "application": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {"api": "available"},
    }
@router.get("/services/{service_id}/health", summary="Get microservice health")
async def get_service_health(service_id: str) -> dict:
    """Check an internal microservice without exposing its port publicly."""
    url = SERVICE_HEALTH_ENDPOINTS.get(service_id)
    if url is None:
        raise HTTPException(status_code=404, detail="Unknown service")
    try:
        return await asyncio.to_thread(_read_service_health, url)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{service_id} is unavailable",
        ) from exc
