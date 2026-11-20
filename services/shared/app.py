import logging
import asyncio
import ipaddress
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, ConfigDict, Field

from services.shared.faults import (
    FAULT_MODES_BY_SERVICE,
    FaultLeaseConflict,
    FaultLeaseManager,
    InvalidFaultMode,
    InvalidLeaseDuration,
    set_active_fault_manager,
)
from services.telemetry.runtime import TelemetryRuntime, set_active_runtime
from services.shared.topology import service_details

logger = logging.getLogger("simulated-services")
CONTROL_PREFIX = "/__control/"
HEALTH_PATH = "/health"
USER_LATENCY_SECONDS = 1.2
IMPLEMENTED_FAULT_MODES = {
    "user-service": frozenset({"latency_injection", "service_unavailable"}),
    "database-service": frozenset({"database_connection_failure"}),
    "api-gateway": frozenset(),
    "order-service": frozenset(),
}


class FaultLeaseStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = Field(min_length=1, max_length=64)
    duration_seconds: float = Field(strict=True)


def _require_loopback_control_client(request: Request) -> None:
    host = request.client.host if request.client is not None else None
    try:
        is_loopback = host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Fault controls are available only from loopback")


def create_service_app(service_id: str, *, startup=None, health_check=None) -> FastAPI:
    details = service_details(service_id)
    telemetry = TelemetryRuntime(service_id)
    fault_manager = FaultLeaseManager()
    set_active_runtime(telemetry)
    set_active_fault_manager(fault_manager)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info("Starting %s", details["name"])
        telemetry.start()
        try:
            if startup is not None:
                startup()
            yield
        finally:
            telemetry.shutdown()
            logger.info("Stopping %s", details["name"])

    app = FastAPI(
        title=str(details["name"]),
        version="0.1.0",
        description="Local simulated service for the API reliability project.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
    )
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=telemetry.tracer_provider,
        meter_provider=telemetry.meter_provider,
        server_request_hook=telemetry.server_request_hook,
        excluded_urls=r".*/(?:health|__control(?:/.*)?)$",
    )

    async def observed_requests_with_faults(request: Request, call_next):
        path = request.url.path
        if path == HEALTH_PATH or path.startswith(CONTROL_PREFIX):
            # Health and local control traffic must not enter evaluation telemetry.
            return await call_next(request)

        async def apply_fault(active_request: Request):
            if service_id == "user-service":
                if fault_manager.is_active(service_id, "service_unavailable"):
                    return JSONResponse(
                        status_code=503,
                        content={"detail": "User Service is temporarily unavailable", "status": "error"},
                    )
                latency_status = fault_manager.status(service_id)
                if latency_status and latency_status["mode"] == "latency_injection":
                    # Do not let an in-flight delay outlive its lease deadline.
                    await asyncio.sleep(min(
                        USER_LATENCY_SECONDS,
                        latency_status["remaining_seconds"],
                    ))
            return await call_next(active_request)

        return await telemetry.http_middleware(request, apply_fault)

    app.middleware("http")(observed_requests_with_faults)

    def check_local_control(request: Request) -> None:
        _require_loopback_control_client(request)

    def require_supported_mode(mode: str) -> None:
        if mode not in IMPLEMENTED_FAULT_MODES[service_id]:
            raise HTTPException(status_code=422, detail=f"Fault mode {mode!r} is not enabled for {service_id}")
        if mode not in FAULT_MODES_BY_SERVICE[service_id]:
            raise HTTPException(status_code=422, detail=f"Fault mode {mode!r} is not allowlisted for {service_id}")

    @app.get("/__control/faults", tags=["local fault control"])
    def fault_status(request: Request) -> dict:
        check_local_control(request)
        return {"target": service_id, "active_lease": fault_manager.status(service_id)}

    @app.post("/__control/faults", tags=["local fault control"])
    def start_fault_lease(body: FaultLeaseStartRequest, request: Request) -> dict:
        check_local_control(request)
        require_supported_mode(body.mode)
        try:
            lease = fault_manager.start(service_id, body.mode, body.duration_seconds)
        except InvalidLeaseDuration as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except InvalidFaultMode as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FaultLeaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        current = fault_manager.status(service_id)
        return {"target": service_id, "lease_id": lease.lease_id, "active_lease": current}

    @app.delete("/__control/faults/{lease_id}", tags=["local fault control"])
    def stop_fault_lease(lease_id: str, request: Request) -> dict:
        check_local_control(request)
        return {"target": service_id, "lease_id": lease_id,
                "stopped": fault_manager.stop(service_id, lease_id)}

    @app.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        result = {
            "status": "ok",
            "service_id": details["id"],
            "service": details["name"],
            "dependencies": details["dependencies"],
        }
        if health_check is not None:
            result["checks"] = health_check()
        return result

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled service error for %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected service error occurred.", "status": "error"},
        )

    return app
