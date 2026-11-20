import math
import hashlib
import json
import os
import socket
import subprocess
import sys
import shutil
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest

from services.shared.app import _require_loopback_control_client
from services.fault_simulator.main import (
    APPROVED_SCENARIO_IDS,
    CONTROLLER_HOST,
    CONTROLLER_PORT,
    FaultScenarioController,
    FaultSimulatorError,
    StartRunRequest,
    create_app as create_fault_simulator_app,
    run_controller,
)

from services.database import storage as database_storage
from services.shared.faults import (
    ALLOWED_SERVICE_IDS,
    FAULT_MODES_BY_SERVICE,
    MAX_LEASE_SECONDS,
    FaultLeaseConflict,
    FaultLeaseManager,
    InvalidFaultMode,
    InvalidFaultTarget,
    InvalidLeaseDuration,
    get_active_fault_manager,
    set_active_fault_manager,
)


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.value += seconds


class FakeFaultServiceTransport:
    """Service-control stand-in for controller tests; never starts app services."""

    def __init__(self) -> None:
        self.calls = []
        self.leases = {}
        self.fail_target = None

    def __call__(self, service_id, method, path, payload):
        self.calls.append((service_id, method, path, payload))
        if service_id == self.fail_target and method == "POST" and path == "/__control/faults":
            return 503, {"detail": "simulated activation failure"}
        if service_id == "api-gateway":
            return 503, {"detail": "dependency unavailable"}
        if method == "POST" and path == "/__control/faults":
            lease_id = f"lease-{len(self.leases) + 1}"
            self.leases[(service_id, lease_id)] = dict(payload)
            return 200, {"lease_id": lease_id}
        if method == "GET" and path == "/__control/faults":
            active = next((
                {"lease_id": lease_id, **lease, "remaining_seconds": 10}
                for (target, lease_id), lease in self.leases.items() if target == service_id
            ), None)
            return 200, {"active_lease": active}
        if method == "DELETE":
            lease_id = path.rsplit("/", 1)[-1]
            self.leases.pop((service_id, lease_id), None)
            return 200, {"stopped": True}
        return 404, {}


class FaultSimulatorControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeMonotonicClock()
        self.transport = FakeFaultServiceTransport()
        self.controller = FaultScenarioController(request_service=self.transport, clock=self.clock)

    def test_controller_is_loopback_only_and_uses_approved_port(self) -> None:
        self.assertEqual(CONTROLLER_HOST, "127.0.0.1")
        self.assertEqual(CONTROLLER_PORT, 8110)
        with patch("services.fault_simulator.main.uvicorn.run") as run:
            run_controller()
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 8110)

        request = StarletteRequest({
            "type": "http", "method": "GET", "path": "/health", "headers": [],
            "client": ("192.0.2.10", 1234), "server": ("127.0.0.1", 8110),
        })
        from services.fault_simulator.main import _require_loopback
        with self.assertRaises(Exception) as raised:
            _require_loopback(request)
        self.assertEqual(getattr(raised.exception, "status_code", None), 403)

    def test_four_scenarios_are_listed_with_permitted_targets(self) -> None:
        scenarios = self.controller.list_scenarios()
        self.assertEqual({item["scenario_id"] for item in scenarios}, set(APPROVED_SCENARIO_IDS))
        self.assertEqual(next(s["permitted_targets"] for s in scenarios if s["scenario_id"] == "database_connection_failure"), ["database-service"])
        self.assertEqual(next(s["permitted_targets"] for s in scenarios if s["scenario_id"] == "api_latency_injection"), ["user-service"])

    def test_unknown_scenario_and_invalid_duration_are_rejected(self) -> None:
        for scenario, duration in (("unknown", 10), ("database_connection_failure", 61), ("database_connection_failure", 0), ("database_connection_failure", float("inf"))):
            with self.subTest(scenario=scenario, duration=duration):
                with self.assertRaises(FaultSimulatorError):
                    self.controller.start(scenario, duration)

    def test_all_valid_scenarios_activate_only_the_configured_local_lease(self) -> None:
        expected = {
            "database_connection_failure": ("database-service", "database_connection_failure"),
            "api_latency_injection": ("user-service", "latency_injection"),
            "upstream_service_unavailable": ("user-service", "service_unavailable"),
            "cascading_service_failure": ("database-service", "database_connection_failure"),
        }
        for scenario_id, (target, mode) in expected.items():
            with self.subTest(scenario=scenario_id):
                run = self.controller.start(scenario_id, 30)
                self.assertEqual(run["status"], "active")
                self.assertEqual(run["duration_seconds"], 30)
                self.assertEqual(run["active_leases"][0]["target"], target)
                self.assertEqual(run["active_leases"][0]["mode"], mode)
                self.assertTrue(run["run_id"])
                self.controller.stop(scenario_id, run["run_id"])

    def test_stop_clears_all_leases_belonging_to_a_run(self) -> None:
        self.controller.scenarios["cascading_service_failure"]["faults"].append(
            {"target": "user-service", "mode": "latency_injection"}
        )
        run = self.controller.start("cascading_service_failure", 20)
        self.assertEqual(len(run["active_leases"]), 2)
        stopped = self.controller.stop("cascading_service_failure", run["run_id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(self.transport.leases)

    def test_partial_activation_failure_rolls_back_prior_leases(self) -> None:
        self.controller.scenarios["database_connection_failure"]["faults"].append(
            {"target": "user-service", "mode": "latency_injection"}
        )
        self.transport.fail_target = "user-service"
        with self.assertRaises(FaultSimulatorError):
            self.controller.start("database_connection_failure", 20)
        self.assertFalse(self.transport.leases)
        deletes = [call for call in self.transport.calls if call[1] == "DELETE"]
        self.assertEqual(len(deletes), 1)

    def test_conflicting_active_runs_are_rejected(self) -> None:
        run = self.controller.start("api_latency_injection", 20)
        with self.assertRaises(FaultSimulatorError) as raised:
            self.controller.start("upstream_service_unavailable", 20)
        self.assertEqual(raised.exception.status_code, 409)
        self.controller.stop("api_latency_injection", run["run_id"])

    def test_expired_run_is_cleaned_up_automatically(self) -> None:
        run = self.controller.start("database_connection_failure", 2)
        self.clock.advance(2)
        self.controller.cleanup_expired()
        status = self.controller.get_run(run["run_id"])
        self.assertEqual(status["status"], "expired")
        self.assertFalse(status["active_leases"][0]["active"])
        self.assertFalse(self.transport.leases)

    def test_external_service_url_input_is_not_accepted(self) -> None:
        with self.assertRaises(FaultSimulatorError):
            self.controller.start("http://127.0.0.1:9999", 10)
        self.assertFalse(any(call[2].startswith("http") for call in self.transport.calls))

    def test_controller_status_remains_available_during_active_fault(self) -> None:
        run = self.controller.start("upstream_service_unavailable", 20)
        current = self.controller.get_run(run["run_id"])
        self.assertEqual(current["status"], "active")
        self.assertTrue(current["active_leases"][0]["active"])
        self.assertEqual(len(self.controller.list_scenarios()), 4)
        self.controller.stop("upstream_service_unavailable", run["run_id"])

    def test_local_rest_routes_health_scenarios_start_run_and_stop(self) -> None:
        app = create_fault_simulator_app(self.controller)
        endpoints = {
            (route.path, tuple(route.methods)): route.endpoint
            for route in app.routes if getattr(route, "methods", None)
        }
        request = StarletteRequest({
            "type": "http", "method": "GET", "path": "/health", "headers": [],
            "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8110),
        })
        self.assertEqual(endpoints[("/health", ("GET",))](request)["status"], "ok")
        self.assertEqual(endpoints[("/scenarios", ("GET",))](request)["count"], 4)
        start_endpoint = endpoints[("/scenarios/{scenario_id}/start", ("POST",))]
        started = start_endpoint("database_connection_failure", request)
        self.assertEqual(started["duration_seconds"], 30)
        run_endpoint = endpoints[("/runs/{run_id}", ("GET",))]
        self.assertEqual(run_endpoint(started["run_id"], request)["status"], "active")
        stop_endpoint = endpoints[("/scenarios/{scenario_id}/stop", ("POST",))]
        stopped = stop_endpoint("database_connection_failure", request, started["run_id"])
        self.assertEqual(stopped["status"], "stopped")

        with self.assertRaises(FaultSimulatorError) as rejected:
            start_endpoint("database_connection_failure", request, StartRunRequest(duration_seconds=61))
        # The controller maps bounded-duration rejection to a clear REST 422.
        self.assertEqual(rejected.exception.status_code, 422)


class FaultLeasePrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeMonotonicClock()
        self.manager = FaultLeaseManager(clock=self.clock)

    def test_faults_are_inactive_by_default(self) -> None:
        self.assertEqual(set(self.manager.status()), set(ALLOWED_SERVICE_IDS))
        for target in ALLOWED_SERVICE_IDS:
            self.assertFalse(self.manager.is_active(target))
            self.assertIsNone(self.manager.active_lease(target))
            self.assertIsNone(self.manager.status(target))

    def test_allowlisted_targets_and_modes_are_accepted(self) -> None:
        for target, modes in FAULT_MODES_BY_SERVICE.items():
            for mode in modes:
                with self.subTest(target=target, mode=mode):
                    lease = self.manager.start(target, mode, 10)
                    self.assertEqual(lease.target, target)
                    self.assertEqual(lease.mode, mode)
                    self.assertTrue(self.manager.is_active(target, mode))
                    self.assertTrue(self.manager.stop(target, lease.lease_id))

    def test_unknown_targets_and_modes_are_rejected(self) -> None:
        with self.assertRaises(InvalidFaultTarget):
            self.manager.start("unknown-service", "latency_injection", 5)
        with self.assertRaises(InvalidFaultMode):
            self.manager.start("user-service", "unknown-mode", 5)
        with self.assertRaises(InvalidFaultMode):
            self.manager.start("user-service", "database_connection_failure", 5)
        with self.assertRaises(InvalidFaultTarget):
            self.manager.status("unknown-service")

    def test_durations_above_maximum_are_rejected(self) -> None:
        with self.assertRaises(InvalidLeaseDuration):
            self.manager.start("user-service", "latency_injection", MAX_LEASE_SECONDS + 0.01)

    def test_invalid_durations_are_rejected(self) -> None:
        for duration in (0, -1, True, False, "5", math.nan, math.inf, -math.inf):
            with self.subTest(duration=duration):
                with self.assertRaises(InvalidLeaseDuration):
                    self.manager.start("user-service", "latency_injection", duration)

    def test_maximum_duration_is_accepted(self) -> None:
        lease = self.manager.start("user-service", "latency_injection", MAX_LEASE_SECONDS)
        self.assertEqual(lease.duration_seconds, MAX_LEASE_SECONDS)

    def test_explicit_stop_clears_lease_immediately_and_stale_stop_is_safe(self) -> None:
        first = self.manager.start("user-service", "latency_injection", 10)
        self.assertTrue(self.manager.stop("user-service", first.lease_id))
        self.assertFalse(self.manager.is_active("user-service"))
        self.assertFalse(self.manager.stop("user-service", first.lease_id))

        second = self.manager.start("user-service", "service_unavailable", 10)
        self.assertFalse(self.manager.stop("user-service", first.lease_id))
        self.assertTrue(self.manager.is_active("user-service", "service_unavailable"))
        self.assertTrue(self.manager.stop("user-service", second.lease_id))

    def test_automatic_expiry_prevents_fault_from_affecting_later_requests(self) -> None:
        first = self.manager.start("database-service", "database_connection_failure", 5)
        self.clock.advance(4.99)
        self.assertTrue(self.manager.is_active("database-service"))
        self.clock.advance(0.01)
        self.assertFalse(self.manager.is_active("database-service"))
        self.assertIsNone(self.manager.active_lease("database-service"))

        second = self.manager.start("database-service", "latency_injection", 5)
        self.assertNotEqual(first.lease_id, second.lease_id)
        self.assertTrue(self.manager.is_active("database-service", "latency_injection"))

    def test_conflicting_active_leases_are_rejected_per_target(self) -> None:
        self.manager.start("user-service", "latency_injection", 10)
        with self.assertRaises(FaultLeaseConflict):
            self.manager.start("user-service", "service_unavailable", 10)
        other = self.manager.start("order-service", "latency_injection", 10)
        self.assertEqual(other.target, "order-service")

    def test_status_returns_a_snapshot_and_triggers_expiry(self) -> None:
        lease = self.manager.start("order-service", "latency_injection", 8)
        status = self.manager.status("order-service")
        self.assertEqual(status["lease_id"], lease.lease_id)
        self.assertEqual(status["remaining_seconds"], 8)
        self.clock.advance(3)
        self.assertEqual(self.manager.status("order-service")["remaining_seconds"], 5)
        self.clock.advance(5)
        self.assertIsNone(self.manager.status("order-service"))

    def test_concurrent_start_status_and_stop_keep_one_consistent_lease(self) -> None:
        workers = 16
        barrier = threading.Barrier(workers)

        def try_start(_index: int):
            barrier.wait()
            try:
                return self.manager.start("order-service", "service_unavailable", 30)
            except FaultLeaseConflict:
                return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(try_start, range(workers)))
        leases = [item for item in results if item is not None]
        self.assertEqual(len(leases), 1)
        self.assertEqual(self.manager.active_lease("order-service"), leases[0])
        self.assertTrue(self.manager.is_active("order-service", "service_unavailable"))

        stop_barrier = threading.Barrier(workers)

        def read_and_stop(_index: int):
            stop_barrier.wait()
            snapshot = self.manager.status("order-service")
            stopped = self.manager.stop("order-service", leases[0].lease_id)
            return snapshot, stopped

        with ThreadPoolExecutor(max_workers=workers) as pool:
            stop_results = list(pool.map(read_and_stop, range(workers)))
        self.assertEqual(sum(stopped for _, stopped in stop_results), 1)
        self.assertFalse(self.manager.is_active("order-service"))
        self.assertIsNone(self.manager.status("order-service"))

    def test_database_failpoint_raises_before_sqlite_connect(self) -> None:
        original_manager = get_active_fault_manager()
        manager = FaultLeaseManager()
        set_active_fault_manager(manager)
        lease = manager.start("database-service", "database_connection_failure", 5)
        try:
            with patch.object(database_storage.sqlite3, "connect") as sqlite_connect:
                with self.assertRaises(database_storage.SimulatedDatabaseUnavailable):
                    with database_storage.connect():
                        self.fail("Database connection should fail before opening SQLite")
                sqlite_connect.assert_not_called()
        finally:
            manager.stop("database-service", lease.lease_id)
            set_active_fault_manager(original_manager)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_MODULES = {
    "database-service": "services.database.main",
    "user-service": "services.user.main",
    "order-service": "services.order.main",
    "api-gateway": "services.gateway.main",
}


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def json_request(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=payload,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=4) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class Phase8FaultHookIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        simulated_root = PROJECT_ROOT / "data" / "simulated"
        simulated_root.mkdir(parents=True, exist_ok=True)
        cls.temp_dir = tempfile.TemporaryDirectory(
            prefix=".phase8-step2-", dir=simulated_root,
        )
        temp_root = Path(cls.temp_dir.name)
        cls.application_database = temp_root / "application-test.db"
        cls.monitoring_database = temp_root / "monitoring-test.db"
        cls.monitor_port = unused_port()
        cls.ports = {service_id: unused_port() for service_id in SERVICE_MODULES}
        cls.monitor_process = None
        cls.service_processes = []

        env = os.environ.copy()
        env["APP_APPLICATION_DATABASE_PATH"] = str(cls.application_database)
        env["APP_MONITORING_DATABASE_PATH"] = str(cls.monitoring_database)
        env["TELEMETRY_INGEST_URL"] = (
            f"http://127.0.0.1:{cls.monitor_port}/api/telemetry/ingest"
        )
        for service_id, port in cls.ports.items():
            env[service_id.replace("-", "_").upper() + "_PORT"] = str(port)
        env["DATABASE_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['database-service']}"
        env["USER_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['user-service']}"
        env["ORDER_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['order-service']}"

        try:
            cls.monitor_process = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
                    "--host", "127.0.0.1", "--port", str(cls.monitor_port),
                ],
                cwd=PROJECT_ROOT,
                env=env.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            cls._wait_http(cls.monitor_process, cls.monitor_url("/api/health"), "monitoring backend")
            for service_id, module in SERVICE_MODULES.items():
                process = subprocess.Popen(
                    [sys.executable, "-m", module],
                    cwd=PROJECT_ROOT,
                    env=env.copy(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                cls.service_processes.append(process)
                cls._wait_http(
                    process,
                    cls.service_url(service_id, "/health"),
                    service_id,
                )
        except Exception:
            cls._stop_processes()
            cls.temp_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_processes()
        cls._cleanup_temp_dir()

    @classmethod
    def _cleanup_temp_dir(cls) -> None:
        path = Path(cls.temp_dir.name)
        for _ in range(6):
            try:
                if path.exists():
                    shutil.rmtree(path)
                return
            except PermissionError:
                # Windows security scanners can briefly hold a just-closed SQLite file.
                time.sleep(0.2)
        cls.temp_dir.cleanup()

    @classmethod
    def monitor_url(cls, path: str) -> str:
        return f"http://127.0.0.1:{cls.monitor_port}{path}"

    @classmethod
    def service_url(cls, service_id: str, path: str = "") -> str:
        return f"http://127.0.0.1:{cls.ports[service_id]}{path}"

    @classmethod
    def _wait_http(cls, process: subprocess.Popen, url: str, label: str) -> None:
        deadline = time.monotonic() + 20
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{label} exited with code {process.returncode}")
            try:
                with urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError) as exc:
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(f"{label} did not become available: {last_error}")

    @classmethod
    def _stop_processes(cls) -> None:
        processes = list(reversed(getattr(cls, "service_processes", [])))
        if getattr(cls, "monitor_process", None) is not None:
            processes.append(cls.monitor_process)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def control(self, service_id: str, method: str, body: dict | None = None, lease_id: str = ""):
        path = "/__control/faults"
        if lease_id:
            path += f"/{lease_id}"
        return json_request(self.service_url(service_id, path), method, body)

    def wait_for_telemetry(self, predicate, timeout: float = 8.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, result = json_request(self.monitor_url("/api/telemetry/recent?limit=500"))
            self.assertEqual(status, 200)
            records = result["items"]
            if predicate(records):
                return records
            time.sleep(0.1)
        self.fail("Expected service telemetry did not arrive")

    def test_control_routes_are_loopback_only(self) -> None:
        request = StarletteRequest({
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/__control/faults",
            "raw_path": b"/__control/faults",
            "query_string": b"",
            "headers": [],
            "client": ("198.51.100.12", 30000),
            "server": ("127.0.0.1", 8101),
        })
        with self.assertRaises(HTTPException) as raised:
            _require_loopback_control_client(request)
        self.assertEqual(raised.exception.status_code, 403)

    def test_normal_requests_work_and_control_operations_are_not_telemetry(self) -> None:
        for service_id in SERVICE_MODULES:
            status, health = json_request(self.service_url(service_id, "/health"))
            self.assertEqual(status, 200)
            self.assertEqual(health["service_id"], service_id)
            status, control = self.control(service_id, "GET")
            self.assertEqual(status, 200)
            self.assertIsNone(control["active_lease"])

        status, user = json_request(self.service_url("api-gateway", "/api/users/user-1001"))
        self.assertEqual(status, 200)
        self.assertEqual(user["id"], "user-1001")

        records = self.wait_for_telemetry(
            lambda items: any(
                item["service_id"] == "api-gateway" and item.get("route") == "/api/users/{user_id}"
                for item in items
            )
        )
        self.assertFalse(any(
            item.get("route") == "/health" or str(item.get("route", "")).startswith("/__control/")
            or item.get("name", "").endswith("/health")
            or "/__control/" in item.get("name", "")
            for item in records
        ))

    def test_user_unavailable_lease_keeps_health_control_and_observed_503(self) -> None:
        status, started = self.control("user-service", "POST", {
            "mode": "service_unavailable", "duration_seconds": 10,
        })
        self.assertEqual(status, 200, started)
        lease_id = started["lease_id"]
        try:
            status, health = json_request(self.service_url("user-service", "/health"))
            self.assertEqual(status, 200)
            self.assertEqual(health["service_id"], "user-service")
            status, control = self.control("user-service", "GET")
            self.assertEqual(status, 200)
            self.assertEqual(control["active_lease"]["lease_id"], lease_id)

            status, failure = json_request(self.service_url("user-service", "/users/user-1001"))
            self.assertEqual(status, 503)
            self.assertIn("temporarily unavailable", failure["detail"])
        finally:
            status, stopped = self.control("user-service", "DELETE", lease_id=lease_id)
            self.assertEqual(status, 200)
            self.assertTrue(stopped["stopped"])

        status, user = json_request(self.service_url("user-service", "/users/user-1001"))
        self.assertEqual(status, 200)
        self.assertEqual(user["id"], "user-1001")
        records = self.wait_for_telemetry(lambda items: any(
            item["service_id"] == "user-service"
            and item["type"] == "metric"
            and item.get("route") == "/users/user-1001"
            and item.get("status_code") == 503
            for item in items
        ))
        self.assertTrue(any(
            item["service_id"] == "user-service" and item["type"] == "log"
            and item.get("status_code") == 503 for item in records
        ))
        records = self.wait_for_telemetry(lambda items: any(
            item["service_id"] == "user-service" and item["type"] == "trace"
            for item in items
        ))
        self.assertTrue(any(
            item["service_id"] == "user-service" and item["type"] == "trace"
            for item in records
        ))

    def test_latency_lease_is_measured_and_delay_ends_at_lease_expiry(self) -> None:
        status, started = self.control("user-service", "POST", {
            "mode": "latency_injection", "duration_seconds": 5,
        })
        self.assertEqual(status, 200, started)
        lease_id = started["lease_id"]
        try:
            began = time.perf_counter()
            status, _ = json_request(self.service_url("user-service", "/users/user-1001"))
            elapsed = time.perf_counter() - began
            self.assertEqual(status, 200)
            self.assertGreaterEqual(elapsed, 1.1)
            self.assertLess(elapsed, 2.5)
        finally:
            self.control("user-service", "DELETE", lease_id=lease_id)

        records = self.wait_for_telemetry(lambda items: any(
            item["service_id"] == "user-service"
            and item["type"] == "metric"
            and item.get("route") == "/users/{user_id}"
            and item.get("duration_ms", 0) >= 1_000
            for item in items
        ))
        self.assertTrue(any(
            item["service_id"] == "user-service" and item["type"] == "metric"
            and item.get("duration_ms", 0) >= 1_000 for item in records
        ))

        status, started = self.control("user-service", "POST", {
            "mode": "latency_injection", "duration_seconds": 0.15,
        })
        self.assertEqual(status, 200, started)
        began = time.perf_counter()
        status, _ = json_request(self.service_url("user-service", "/users/user-1001"))
        elapsed_after_short_lease = time.perf_counter() - began
        self.assertEqual(status, 200)
        self.assertLess(elapsed_after_short_lease, 0.9)
        self.assertIsNone(self.control("user-service", "GET")[1]["active_lease"])

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def test_database_connection_fault_precedes_sqlite_and_recovers_without_file_changes(self) -> None:
        before = self._sha256(self.application_database)
        status, started = self.control("database-service", "POST", {
            "mode": "database_connection_failure", "duration_seconds": 10,
        })
        self.assertEqual(status, 200, started)
        lease_id = started["lease_id"]
        try:
            status, failure = json_request(
                self.service_url("database-service", "/internal/users/user-1001")
            )
            self.assertEqual(status, 503, failure)
            self.assertIn("connection is unavailable", failure["detail"])
            status, control = self.control("database-service", "GET")
            self.assertEqual(status, 200)
            self.assertEqual(control["active_lease"]["lease_id"], lease_id)
            self.assertEqual(self._sha256(self.application_database), before)
            telemetry_records = self.wait_for_telemetry(lambda items: any(
                item["service_id"] == "database-service" and item["type"] == "metric"
                and item.get("status_code") == 503 for item in items
            ))
            self.assertTrue(any(
                item["service_id"] == "database-service" and item["type"] == "log"
                and item.get("status_code") == 503 for item in telemetry_records
            ))
        finally:
            status, stopped = self.control("database-service", "DELETE", lease_id=lease_id)
            self.assertEqual(status, 200)
            self.assertTrue(stopped["stopped"])

        status, user = json_request(
            self.service_url("database-service", "/internal/users/user-1001")
        )
        self.assertEqual(status, 200)
        self.assertEqual(user["id"], "user-1001")
        self.assertEqual(self._sha256(self.application_database), before)

    def test_control_rejects_invalid_and_conflicting_fault_requests(self) -> None:
        for body in (
            {"mode": "unknown-mode", "duration_seconds": 5},
            {"mode": "database_connection_failure", "duration_seconds": 5},
            {"mode": "service_unavailable", "duration_seconds": 61},
            {"mode": "service_unavailable", "duration_seconds": 0},
            {"mode": "service_unavailable", "duration_seconds": float("inf")},
            {"mode": "service_unavailable", "duration_seconds": 5, "target": "order-service"},
        ):
            status, _ = self.control("user-service", "POST", body)
            self.assertEqual(status, 422, body)

        status, started = self.control("user-service", "POST", {
            "mode": "service_unavailable", "duration_seconds": 10,
        })
        self.assertEqual(status, 200)
        try:
            status, _ = self.control("user-service", "POST", {
                "mode": "latency_injection", "duration_seconds": 10,
            })
            self.assertEqual(status, 409)
        finally:
            self.control("user-service", "DELETE", lease_id=started["lease_id"])

    def test_existing_gateway_order_and_service_contracts_work_after_recovery(self) -> None:
        status, health = json_request(self.service_url("api-gateway", "/health"))
        self.assertEqual(status, 200)
        self.assertEqual(health["service_id"], "api-gateway")

        status, user = json_request(self.service_url("api-gateway", "/api/users/user-1002"))
        self.assertEqual(status, 200)
        self.assertEqual(user["id"], "user-1002")

        status, result = json_request(
            self.service_url("api-gateway", "/api/orders"),
            "POST",
            {"user_id": "user-1001", "item": "Fault lease recovery", "quantity": 1},
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["order"]["user_id"], "user-1001")
        order_id = result["order"]["id"]
        status, retrieved = json_request(
            self.service_url("api-gateway", f"/api/orders/{order_id}")
        )
        self.assertEqual(status, 200)
        self.assertEqual(retrieved["order"]["id"], order_id)

        for service_id in SERVICE_MODULES:
            status, health = json_request(self.service_url(service_id, "/health"))
            self.assertEqual(status, 200)
            self.assertEqual(health["service_id"], service_id)

if __name__ == "__main__":
    unittest.main()
