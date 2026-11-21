import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen
ROOT = Path(__file__).resolve().parent
PORT = os.environ.get("PORT", "8000")
env = os.environ.copy()
env["PYTHONPATH"] = os.pathsep.join([
    str(ROOT / "backend"),
    str(ROOT),
    env.get("PYTHONPATH", ""),
])
env["TELEMETRY_INGEST_URL"] = (
    f"http://127.0.0.1:{PORT}/api/telemetry/ingest"
)
processes = []
shutdown_requested = False
shutdown_started = False


def start_service(name, module, port):
    if shutdown_requested:
        return False
    print(f"Starting {name} on port {port}...", flush=True)
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            f"{module}:app",
            "--host", "127.0.0.1",
            "--port", str(port),
        ],
        cwd=ROOT,
        env=env,
    )
    processes.append(process)
    if shutdown_requested:
        stop_process(process)
        return False
    health_url = f"http://127.0.0.1:{port}/health"
    for _ in range(60):
        if shutdown_requested:
            return False
        if process.poll() is not None:
            raise RuntimeError(f"{name} stopped during startup")
        try:
            with urlopen(health_url, timeout=2):
                if shutdown_requested:
                    return False
                print(f"{name} is ready.", flush=True)
                return True
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{name} did not become ready")


def wait_for_backend_health(process):
    health_url = f"http://127.0.0.1:{PORT}/api/health"
    deadline = time.monotonic() + 60
    while True:
        if shutdown_requested:
            return False
        if process.poll() is not None:
            raise RuntimeError("Monitoring Backend stopped during startup")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Monitoring Backend did not become healthy within 60 seconds")
        try:
            with urlopen(health_url, timeout=min(2, remaining)):
                if shutdown_requested:
                    return False
                print("Monitoring Backend is ready.", flush=True)
                return True
        except Exception:
            if shutdown_requested:
                return False
            if process.poll() is not None:
                raise RuntimeError("Monitoring Backend stopped during startup")
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def stop_process(process):
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            if process.poll() is None:
                print("Could not terminate a child process cleanly.", flush=True)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
        process.wait()


def stop_children():
    global shutdown_started
    if shutdown_started:
        return
    shutdown_started = True
    print("Stopping all services...", flush=True)
    for process in reversed(processes):
        stop_process(process)


def shutdown(*_):
    global shutdown_requested
    shutdown_requested = True
    stop_children()


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
try:
    services = [
        ("Database Service", "services.database.main", 8103),
        ("User Service", "services.user.main", 8101),
        ("Order Service", "services.order.main", 8102),
        ("API Gateway", "services.gateway.main", 8100),
    ]
    for service in services:
        if not start_service(*service):
            sys.exit(0)
    print(f"Starting public Monitoring Backend on port {PORT}...", flush=True)
    backend = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "app.main:app",
            "--host", "0.0.0.0",
            "--port", PORT,
        ],
        cwd=ROOT,
        env=env,
    )
    processes.append(backend)
    if shutdown_requested:
        stop_process(backend)
        sys.exit(0)
    if not wait_for_backend_health(backend):
        sys.exit(0)
    while not shutdown_requested and backend.poll() is None:
        for process in processes[:-1]:
            if process.poll() is not None:
                raise RuntimeError("An internal microservice stopped unexpectedly")
        time.sleep(2)
    if shutdown_requested:
        sys.exit(0)
    raise RuntimeError("Monitoring backend stopped unexpectedly")
except Exception as error:
    if shutdown_requested:
        stop_children()
        sys.exit(0)
    print(f"Startup error: {error}", flush=True)
    stop_children()
    sys.exit(1)
