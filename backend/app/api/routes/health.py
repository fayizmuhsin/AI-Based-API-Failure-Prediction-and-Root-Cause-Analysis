from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.core.config import settings

router = APIRouter(tags=["system"])


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
