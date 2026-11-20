import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.anomaly.runtime import anomaly_repository, anomaly_service
from app.api.routes.anomalies import router as anomalies_router
from app.api.routes.health import router as health_router
from app.api.routes.rca import router as rca_router
from app.api.routes.telemetry import router as telemetry_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.api.routes.telemetry import telemetry_repository
from app.telemetry.worker import TelemetryWorker
from app.rca.runtime import rca_service

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting %s v%s (%s)", settings.app_name, settings.app_version, settings.environment)
    telemetry_repository.initialize()
    anomaly_repository.initialize()
    try:
        rca_service.initialize()
    except Exception:
        logger.exception("RCA initialization failed; telemetry and anomaly processing will remain available")
    anomaly_service.load_active_model()

    def process_post_ingestion() -> int:
        detected = 0
        try:
            detected = anomaly_service.process_batch()
        except Exception:
            logger.exception("Anomaly processing failed; telemetry ingestion remains available")
        try:
            rca_service.process_batch()
        except Exception:
            logger.exception("RCA processing failed; it will retry on a later worker poll")
        return detected

    worker = TelemetryWorker(
        telemetry_repository,
        post_processor=process_post_ingestion,
    )
    worker.start()
    try:
        yield
    finally:
        worker.stop()
        logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Local project foundation for API reliability monitoring.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def bound_telemetry_request_size(request: Request, call_next):
    """Bound ingestion batches before JSON parsing; other platform routes are unaffected."""
    if request.url.path == f"{settings.api_prefix}/telemetry/ingest":
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                oversized = int(content_length) > 1_048_576
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
            if oversized:
                return JSONResponse(status_code=413, content={"detail": "Telemetry batch exceeds 1 MiB"})
    return await call_next(request)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error while handling %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred.", "status": "error"},
    )


app.include_router(health_router, prefix=settings.api_prefix)
app.include_router(telemetry_router, prefix=settings.api_prefix)
app.include_router(anomalies_router, prefix=settings.api_prefix)
app.include_router(rca_router, prefix=settings.api_prefix)
