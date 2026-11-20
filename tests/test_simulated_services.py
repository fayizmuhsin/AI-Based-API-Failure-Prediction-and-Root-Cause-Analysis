import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.shared.http_client import DownstreamError, request_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
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
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class SimulatedServicesIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        simulated_dir = PROJECT_ROOT / "data" / "simulated"
        simulated_dir.mkdir(parents=True, exist_ok=True)
        cls.temp_dir = tempfile.TemporaryDirectory(prefix=".phase2-test-", dir=simulated_dir)
        cls.test_database = Path(cls.temp_dir.name) / "application-test.db"
        cls.monitoring_database = PROJECT_ROOT / "data" / "monitoring" / "monitoring.db"
        cls.monitoring_before = cls._file_snapshot(cls.monitoring_database)
        cls.ports = {service_id: unused_port() for service_id in SERVICES}
        cls.processes: list[subprocess.Popen] = []

        base_env = os.environ.copy()
        base_env["APP_APPLICATION_DATABASE_PATH"] = str(cls.test_database)
        # Phase 2 regression tests do not depend on the Phase 3 platform process.
        base_env["TELEMETRY_INGEST_URL"] = f"http://127.0.0.1:{unused_port()}/api/telemetry/ingest"
        for service_id, port in cls.ports.items():
            base_env[service_id.replace("-", "_").upper() + "_PORT"] = str(port)
        base_env["DATABASE_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['database-service']}"
        base_env["USER_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['user-service']}"
        base_env["ORDER_SERVICE_URL"] = f"http://127.0.0.1:{cls.ports['order-service']}"

        try:
            # Start dependency-first, while keeping each service in its own process.
            for service_id, module in SERVICES.items():
                process = subprocess.Popen(
                    [sys.executable, "-m", module],
                    cwd=PROJECT_ROOT,
                    env=base_env.copy(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                cls.processes.append(process)
                cls._wait_for_health(process, cls.ports[service_id], service_id)
        except Exception:
            cls._stop_processes()
            cls.temp_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_processes()
        after = cls._file_snapshot(cls.monitoring_database)
        if after != cls.monitoring_before:
            raise AssertionError("Simulated services modified the platform monitoring database")
        cls.temp_dir.cleanup()

    @staticmethod
    def _file_snapshot(path: Path):
        if not path.exists():
            return None
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    @classmethod
    def _wait_for_health(cls, process: subprocess.Popen, port: int, service_id: str) -> None:
        url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{service_id} exited during startup with code {process.returncode}")
            try:
                with urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError) as exc:
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(f"{service_id} did not become healthy: {last_error}")

    @classmethod
    def _stop_processes(cls) -> None:
        for process in reversed(getattr(cls, "processes", [])):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    @classmethod
    def base_url(cls, service_id: str) -> str:
        return f"http://127.0.0.1:{cls.ports[service_id]}"

    def test_each_service_has_a_healthy_endpoint_and_approved_dependencies(self) -> None:
        expected = {
            "api-gateway": {"user-service", "order-service"},
            "user-service": {"database-service"},
            "order-service": {"user-service", "database-service"},
            "database-service": set(),
        }
        for service_id in SERVICES:
            with self.subTest(service=service_id):
                status, payload = json_request(f"{self.base_url(service_id)}/health")
                self.assertEqual(status, 200)
                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["service_id"], service_id)
                self.assertEqual(set(payload["dependencies"]), expected[service_id])

    def test_service_health_allows_local_dashboard_origin(self) -> None:
        for service_id in SERVICES:
            request = Request(
                f"{self.base_url(service_id)}/health",
                method="OPTIONS",
                headers={
                    "Origin": "http://127.0.0.1:5173",
                    "Access-Control-Request-Method": "GET",
                },
            )
            with urlopen(request, timeout=3) as response:
                self.assertIn(response.status, {200, 204})
                self.assertEqual(
                    response.headers.get("Access-Control-Allow-Origin"),
                    "http://127.0.0.1:5173",
                )

    def test_gateway_routes_user_request_through_user_service_to_database(self) -> None:
        status, user = json_request(f"{self.base_url('api-gateway')}/api/users/user-1001")
        self.assertEqual(status, 200)
        self.assertEqual(user["id"], "user-1001")
        self.assertEqual(user["name"], "Avery Chen")

    def test_gateway_order_flow_calls_user_and_persists_order(self) -> None:
        gateway = self.base_url("api-gateway")
        status, created = json_request(
            f"{gateway}/api/orders",
            method="POST",
            body={"user_id": "user-1002", "item": "Campus notebook", "quantity": 2},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["user"]["id"], "user-1002")
        order_id = created["order"]["id"]

        status, retrieved = json_request(f"{gateway}/api/orders/{order_id}")
        self.assertEqual(status, 200)
        self.assertEqual(retrieved["order"]["item"], "Campus notebook")
        self.assertEqual(retrieved["order"]["quantity"], 2)
        self.assertEqual(retrieved["user"]["id"], "user-1002")

    def test_database_operations_and_application_database_isolation(self) -> None:
        database = self.base_url("database-service")
        status, user = json_request(f"{database}/internal/users/user-1001")
        self.assertEqual(status, 200)
        self.assertEqual(user["email"], "avery.chen@example.local")

        status, saved = json_request(
            f"{database}/internal/orders",
            method="POST",
            body={"user_id": "user-1001", "item": "Lab journal", "quantity": 1},
        )
        self.assertEqual(status, 201)
        status, loaded = json_request(f"{database}/internal/orders/{saved['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(loaded["id"], saved["id"])
        self.assertTrue(self.test_database.is_file())
        self.assertNotEqual(self.test_database.resolve(), self.monitoring_database.resolve())
        self.assertEqual(self._file_snapshot(self.monitoring_database), self.monitoring_before)

    def test_order_service_rejects_unknown_user(self) -> None:
        status, error = json_request(
            f"{self.base_url('api-gateway')}/api/orders",
            method="POST",
            body={"user_id": "missing-user", "item": "Test item", "quantity": 1},
        )
        self.assertEqual(status, 404)
        self.assertIn("was not found", error["detail"])

    def test_unavailable_downstream_has_a_clear_service_unavailable_error(self) -> None:
        port = unused_port()
        with self.assertRaises(DownstreamError) as raised:
            request_json("GET", f"http://127.0.0.1:{port}/health")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("Downstream service unavailable", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
