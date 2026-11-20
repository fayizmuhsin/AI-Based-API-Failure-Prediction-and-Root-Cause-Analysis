"""Loopback-only orchestration for service-local Phase 8 fault leases."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request as FastAPIRequest
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.shared.config import service_port
from services.shared.faults import ALLOWED_SERVICE_IDS, FAULT_MODES_BY_SERVICE, MAX_LEASE_SECONDS

logger = logging.getLogger("fault-simulator")

CONTROLLER_HOST = "127.0.0.1"
CONTROLLER_PORT = 8110
DEFAULT_DURATION_SECONDS = 30.0
SERVICE_REQUEST_TIMEOUT_SECONDS = 3.0
REAPER_INTERVAL_SECONDS = 0.2
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_CONFIG_PATH = PROJECT_ROOT / "config" / "fault_scenarios.json"
APPROVED_SCENARIO_IDS = frozenset({
    "database_connection_failure",
    "api_latency_injection",
    "upstream_service_unavailable",
    "cascading_service_failure",
})

# The cascade uses an unknown user ID so a broken failpoint cannot create an
# order in the simulated application database. No scenario marker is sent.
CASCADE_PROBES = (
    ("GET", "/api/users/user-1001", None),
    (
        "POST",
        "/api/orders",
        {
            "user_id": "phase8-probe-user-does-not-exist",
            "item": "Dependency probe",
            "quantity": 1,
        },
    ),
)


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: float = Field(default=DEFAULT_DURATION_SECONDS, strict=True)


class FaultSimulatorError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class FaultRun:
    run_id: str
    scenario_id: str
    duration_seconds: float
    started_at: datetime
    expires_at: datetime
    expires_monotonic: float
    status: str = "starting"
    leases: list[dict] = field(default_factory=list)
    exercise_results: list[dict] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)
    stopped_at: datetime | None = None


class FaultScenarioController:
    """Serialize fault runs and only call approved loopback service routes."""

    def __init__(
        self,
        *,
        request_service: Callable[[str, str, str, dict | None], tuple[int, dict]] | None = None,
        clock: Callable[[], float] | None = None,
        scenarios: dict[str, dict] | None = None,
    ) -> None:
        self._request_service = request_service or self._request_local_service
        self._clock = clock or time.monotonic
        self._lock = RLock()
        self._runs: dict[str, FaultRun] = {}
        self.scenarios = scenarios or self._load_scenarios()

    @staticmethod
    def _load_scenarios() -> dict[str, dict]:
        try:
            payload = json.loads(SCENARIO_CONFIG_PATH.read_text(encoding="utf-8"))
            items = payload["scenarios"]
            if not isinstance(items, list):
                raise ValueError("scenarios must be an array")
            scenarios = {item["id"]: item for item in items}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Invalid fault scenario configuration: {exc}") from exc
        if set(scenarios) != APPROVED_SCENARIO_IDS:
            raise RuntimeError("Fault scenarios must match the four approved scenario IDs")

        for scenario_id, scenario in scenarios.items():
            if not isinstance(scenario.get("faults"), list) or not scenario["faults"]:
                raise RuntimeError(f"Scenario {scenario_id} must declare at least one fault")
            for fault in scenario["faults"]:
                target, mode = fault.get("target"), fault.get("mode")
                if target not in ALLOWED_SERVICE_IDS:
                    raise RuntimeError(f"Scenario {scenario_id} has an unapproved target")
                if mode not in FAULT_MODES_BY_SERVICE[target]:
                    raise RuntimeError(f"Scenario {scenario_id} has an unapproved fault mode")
            if scenario.get("exercise_cascade") and scenario_id != "cascading_service_failure":
                raise RuntimeError("Only the cascading scenario may exercise Gateway probes")
        return scenarios

    def list_scenarios(self) -> list[dict]:
        return [
            {
                "scenario_id": scenario_id,
                "description": definition["description"],
                "permitted_targets": sorted({fault["target"] for fault in definition["faults"]}),
                "faults": [dict(fault) for fault in definition["faults"]],
            }
            for scenario_id, definition in sorted(self.scenarios.items())
        ]

    def start(self, scenario_id: str, duration_seconds: float) -> dict:
        self._validate_scenario(scenario_id)
        duration = self._validate_duration(duration_seconds)
        with self._lock:
            self._cleanup_expired_locked()
            if any(run.status in {"starting", "active", "stopping", "cleanup_pending"}
                   for run in self._runs.values()):
                raise FaultSimulatorError(409, "Another fault scenario run is already active")

            now = datetime.now(timezone.utc)
            run = FaultRun(
                run_id=str(uuid4()),
                scenario_id=scenario_id,
                duration_seconds=duration,
                started_at=now,
                expires_at=now + timedelta(seconds=duration),
                expires_monotonic=self._clock() + duration,
            )
            self._runs[run.run_id] = run

            try:
                for fault in self.scenarios[scenario_id]["faults"]:
                    target = fault["target"]
                    status, response = self._request_service(
                        target,
                        "POST",
                        "/__control/faults",
                        {"mode": fault["mode"], "duration_seconds": duration},
                    )
                    if status != 200 or not isinstance(response.get("lease_id"), str):
                        raise FaultSimulatorError(
                            self._failure_status(status),
                            f"Could not activate the {target} fault lease (HTTP {status})",
                        )
                    run.leases.append({
                        "target": target,
                        "mode": fault["mode"],
                        "lease_id": response["lease_id"],
                        "active": True,
                    })

                if self.scenarios[scenario_id].get("exercise_cascade"):
                    for method, path, body in CASCADE_PROBES:
                        status, _ = self._request_service("api-gateway", method, path, body)
                        run.exercise_results.append({"method": method, "path": path, "status_code": status})
                        if status == 0:
                            raise FaultSimulatorError(503, "Could not reach API Gateway for cascade probes")

                run.status = "active"
                return self._run_snapshot_locked(run, query_services=False)
            except Exception as exc:
                self._rollback_locked(run)
                if not run.cleanup_errors:
                    run.status = "failed"
                if isinstance(exc, FaultSimulatorError):
                    raise
                logger.exception("Fault scenario activation failed for run %s", run.run_id)
                raise FaultSimulatorError(502, "Fault scenario activation failed; activated leases were rolled back") from exc

    def stop(self, scenario_id: str, run_id: str | None = None) -> dict:
        self._validate_scenario(scenario_id)
        with self._lock:
            self._cleanup_expired_locked()
            run = self._find_run_locked(scenario_id, run_id)
            if run is None:
                raise FaultSimulatorError(404, "No matching fault scenario run was found")
            if run.status in {"stopped", "expired", "failed"}:
                return self._run_snapshot_locked(run, query_services=False)

            run.status = "stopping"
            failures = self._stop_leases_locked(run)
            if failures:
                run.status = "cleanup_pending"
                run.cleanup_errors = failures
                raise FaultSimulatorError(502, "Some leases could not be stopped; automatic cleanup will retry")

            run.status = "stopped"
            run.stopped_at = datetime.now(timezone.utc)
            run.cleanup_errors = []
            return self._run_snapshot_locked(run, query_services=False)

    def get_run(self, run_id: str) -> dict:
        try:
            UUID(run_id)
        except (ValueError, TypeError) as exc:
            raise FaultSimulatorError(404, "Fault scenario run was not found") from exc
        with self._lock:
            self._cleanup_expired_locked()
            run = self._runs.get(run_id)
            if run is None:
                raise FaultSimulatorError(404, "Fault scenario run was not found")
            return self._run_snapshot_locked(run, query_services=run.status in {"active", "cleanup_pending"})

    def cleanup_expired(self) -> None:
        with self._lock:
            self._cleanup_expired_locked()

    def _cleanup_expired_locked(self) -> None:
        now = self._clock()
        for run in self._runs.values():
            expired = now >= run.expires_monotonic
            if run.status == "active" and expired:
                run.status = "stopping"
            if run.status not in {"stopping", "cleanup_pending"}:
                continue
            failures = self._stop_leases_locked(run)
            if failures:
                run.status = "cleanup_pending"
                run.cleanup_errors = failures
                continue
            run.cleanup_errors = []
            run.status = "expired" if expired else "stopped"
            run.stopped_at = run.stopped_at or datetime.now(timezone.utc)

    def _rollback_locked(self, run: FaultRun) -> None:
        failures = self._stop_leases_locked(run)
        run.cleanup_errors = failures
        if failures:
            run.status = "cleanup_pending"

    def _stop_leases_locked(self, run: FaultRun) -> list[str]:
        failures = []
        for lease in run.leases:
            if not lease.get("active", True):
                continue
            try:
                status, response = self._request_service(
                    lease["target"],
                    "DELETE",
                    f"/__control/faults/{lease['lease_id']}",
                    None,
                )
                if status != 200:
                    raise FaultSimulatorError(status, f"Stop returned HTTP {status}")
                lease["active"] = False
            except Exception as exc:
                failures.append(f"{lease['target']}: {type(exc).__name__}")
                logger.warning("Could not stop fault lease for %s; it will expire automatically", lease["target"])
        return failures

    def _run_snapshot_locked(self, run: FaultRun, *, query_services: bool) -> dict:
        leases = []
        for saved in run.leases:
            active = bool(saved.get("active", False))
            remaining = max(0.0, run.expires_monotonic - self._clock()) if active else 0.0
            if query_services and active:
                try:
                    status, response = self._request_service(
                        saved["target"], "GET", "/__control/faults", None,
                    )
                    current = response.get("active_lease") if status == 200 else None
                    active = bool(current and current.get("lease_id") == saved["lease_id"])
                    remaining = float(current.get("remaining_seconds", 0.0)) if active else 0.0
                except Exception:
                    active = False
                    remaining = 0.0
            leases.append({
                "target": saved["target"],
                "mode": saved["mode"],
                "lease_id": saved["lease_id"],
                "active": active,
                "remaining_seconds": remaining,
            })
        return {
            "run_id": run.run_id,
            "scenario_id": run.scenario_id,
            "status": run.status,
            "duration_seconds": run.duration_seconds,
            "started_at": run.started_at.isoformat(),
            "expires_at": run.expires_at.isoformat(),
            "stopped_at": run.stopped_at.isoformat() if run.stopped_at else None,
            "active_leases": leases,
            "exercise_results": list(run.exercise_results),
            "cleanup_errors": list(run.cleanup_errors),
        }

    def _find_run_locked(self, scenario_id: str, run_id: str | None) -> FaultRun | None:
        if run_id is not None:
            run = self._runs.get(run_id)
            return run if run and run.scenario_id == scenario_id else None
        candidates = [run for run in self._runs.values() if run.scenario_id == scenario_id]
        return max(candidates, key=lambda item: item.started_at) if candidates else None

    def _validate_scenario(self, scenario_id: str) -> None:
        if scenario_id not in self.scenarios:
            raise FaultSimulatorError(404, f"Unknown scenario: {scenario_id}")

    @staticmethod
    def _validate_duration(duration_seconds: float) -> float:
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
            raise FaultSimulatorError(422, "Duration must be a finite number of seconds")
        duration = float(duration_seconds)
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_LEASE_SECONDS:
            raise FaultSimulatorError(422, f"Duration must be greater than 0 and at most {MAX_LEASE_SECONDS:g} seconds")
        return duration

    @staticmethod
    def _failure_status(status: int) -> int:
        return status if status in {409, 422, 503} else 502

    @staticmethod
    def _request_local_service(
        service_id: str, method: str, path: str, payload: dict | None,
    ) -> tuple[int, dict]:
        if service_id not in ALLOWED_SERVICE_IDS:
            raise FaultSimulatorError(400, "Service target is not allowlisted")
        if not path.startswith("/") or urlsplit(path).scheme or urlsplit(path).netloc:
            raise FaultSimulatorError(400, "Only local service paths are permitted")
        port = service_port(service_id)
        url = f"http://127.0.0.1:{port}{path}"
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port != port:
            raise FaultSimulatorError(400, "Service requests must target an allowlisted loopback URL")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=SERVICE_REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                status = response.status
        except HTTPError as exc:
            with exc:
                status = exc.code
                raw = exc.read()
        except (URLError, TimeoutError, OSError) as exc:
            raise FaultSimulatorError(503, f"Could not reach local {service_id}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = {}
        return status, decoded if isinstance(decoded, dict) else {}


def _require_loopback(request: FastAPIRequest) -> None:
    host = request.client.host if request.client is not None else None
    try:
        local = host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise HTTPException(status_code=403, detail="Fault simulator is available only from loopback")


def create_app(controller: FaultScenarioController | None = None) -> FastAPI:
    scenario_controller = controller or FaultScenarioController()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def reap_loop() -> None:
            while True:
                await asyncio.to_thread(scenario_controller.cleanup_expired)
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)

        task = asyncio.create_task(reap_loop(), name="fault-run-expiry-cleanup")
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.to_thread(scenario_controller.cleanup_expired)

    app = FastAPI(
        title="Local Fault Simulator",
        version="0.1.0",
        description="Loopback-only control plane for bounded local fault leases.",
        lifespan=lifespan,
    )
    app.state.fault_controller = scenario_controller

    @app.exception_handler(FaultSimulatorError)
    async def simulator_error_handler(_: FastAPIRequest, exc: FaultSimulatorError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health")
    def health(request: FastAPIRequest) -> dict:
        _require_loopback(request)
        return {"status": "ok", "service": "fault-simulator", "host": CONTROLLER_HOST, "port": CONTROLLER_PORT}

    @app.get("/scenarios")
    def scenarios(request: FastAPIRequest) -> dict:
        _require_loopback(request)
        return {"count": len(scenario_controller.scenarios), "items": scenario_controller.list_scenarios()}

    @app.post("/scenarios/{scenario_id}/start")
    def start_scenario(
        scenario_id: str,
        request: FastAPIRequest,
        body: StartRunRequest | None = None,
    ) -> dict:
        _require_loopback(request)
        duration = body.duration_seconds if body is not None else DEFAULT_DURATION_SECONDS
        return scenario_controller.start(scenario_id, duration)

    @app.post("/scenarios/{scenario_id}/stop")
    def stop_scenario(
        scenario_id: str,
        request: FastAPIRequest,
        run_id: str | None = Query(default=None),
    ) -> dict:
        _require_loopback(request)
        return scenario_controller.stop(scenario_id, run_id)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, request: FastAPIRequest) -> dict:
        _require_loopback(request)
        return scenario_controller.get_run(run_id)

    return app


app = create_app()


def run_controller() -> None:
    uvicorn.run(app, host=CONTROLLER_HOST, port=CONTROLLER_PORT, reload=False)


if __name__ == "__main__":
    run_controller()
