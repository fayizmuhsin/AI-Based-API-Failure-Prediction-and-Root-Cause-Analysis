import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from services.shared.topology import service_details


logger = logging.getLogger("services.telemetry.delivery")
_active_runtime: "TelemetryRuntime | None" = None
MAX_QUEUE_ITEMS = 2_000
MAX_EXPORT_BATCH = 100
DELIVERY_TIMEOUT_SECONDS = 0.75
INGEST_URL = os.getenv("TELEMETRY_INGEST_URL", "http://127.0.0.1:8000/api/telemetry/ingest")


def _iso_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat()


def _safe_attributes(attributes: dict | None, limit: int = 64) -> dict:
    result = {}
    for key, value in list((attributes or {}).items())[:limit]:
        if isinstance(value, (str, int, float, bool)):
            result[str(key)[:128]] = value[:1024] if isinstance(value, str) else value
    return result


class TraceIngestionExporter(SpanExporter):
    """Serialize SDK spans into the platform's validated development JSON schema."""

    def __init__(self, runtime: "TelemetryRuntime"):
        self.runtime = runtime

    def export(self, spans: Iterable) -> SpanExportResult:
        for span in spans:
            context = span.context
            if context is None or not context.is_valid:
                continue
            parent = span.parent
            events = []
            for event in list(span.events or [])[:32]:
                events.append({
                    "name": str(event.name)[:256],
                    "timestamp": _iso_from_ns(event.timestamp),
                    "attributes": _safe_attributes(event.attributes),
                })
            self.runtime.emit({
                "type": "trace",
                "timestamp": _iso_from_ns(span.start_time),
                "end_time": _iso_from_ns(span.end_time),
                "trace_id": f"{context.trace_id:032x}",
                "span_id": f"{context.span_id:016x}",
                "parent_span_id": f"{parent.span_id:016x}" if parent and parent.is_valid else None,
                "name": str(span.name)[:256],
                "kind": span.kind.name,
                "status": span.status.status_code.name,
                "status_description": (span.status.description or "")[:2048] or None,
                "attributes": _safe_attributes(span.attributes),
                "events": events,
            })
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


class TelemetryRuntime:
    """Per-service OTel providers plus a bounded, non-blocking delivery queue."""

    def __init__(self, service_id: str):
        details = service_details(service_id)
        self.service_id = service_id
        self.service_name = details["name"]
        self.endpoint = os.getenv("TELEMETRY_INGEST_URL", INGEST_URL)
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=MAX_QUEUE_ITEMS)
        self._thread: threading.Thread | None = None
        self._started = False
        self._stop_lock = threading.Lock()
        self.dropped_records = 0
        self._last_warning = 0.0
        install_trace_context_propagator()

        resource = Resource.create({
            "service.name": service_id,
            "service.display_name": self.service_name,
            "service.namespace": "api-failure-prediction",
            "service.version": "0.1.0",
        })
        self.tracer_provider = TracerProvider(resource=resource)
        self.tracer_provider.add_span_processor(
            BatchSpanProcessor(
                TraceIngestionExporter(self),
                max_queue_size=512,
                max_export_batch_size=64,
                schedule_delay_millis=200,
                export_timeout_millis=1_000,
            )
        )
        self.tracer = self.tracer_provider.get_tracer("simulated-service")

        self.metric_reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[self.metric_reader])
        meter = self.meter_provider.get_meter("simulated-service")
        self.request_counter = meter.create_counter("http.request.count", unit="{request}")
        self.duration_histogram = meter.create_histogram("http.request.duration", unit="ms")

    def start(self) -> None:
        with self._stop_lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._deliver_loop,
                name=f"telemetry-delivery-{self.service_id}",
                daemon=True,
            )
            self._thread.start()

    def server_request_hook(self, span, scope: dict) -> None:
        context = span.get_span_context()
        if context.is_valid:
            scope["phase3.trace_id"] = f"{context.trace_id:032x}"
            scope["phase3.span_id"] = f"{context.span_id:016x}"

    async def http_middleware(self, request, call_next):
        started = time.perf_counter()
        route = request.url.path
        status_code = 500
        error_details = None
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            error_details = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1_000
            route_obj = request.scope.get("route")
            route = getattr(route_obj, "path", route)
            trace_id = request.scope.get("phase3.trace_id")
            self.record_request(
                metric_name="http.server.request",
                method=request.method,
                route=route,
                status_code=status_code,
                duration_ms=duration_ms,
                trace_id=trace_id,
            )
            level = "ERROR" if status_code >= 500 else "WARNING" if status_code >= 400 else "INFO"
            self.record_log(
                level=level,
                event_type="http.server.request",
                trace_id=trace_id,
                method=request.method,
                route=route,
                status_code=status_code,
                duration_ms=duration_ms,
                error_details=error_details or (f"HTTP {status_code}" if status_code >= 400 else None),
            )
        return response

    def record_request(
        self,
        *,
        metric_name: str,
        method: str,
        route: str,
        status_code: int | None,
        duration_ms: float,
        trace_id: str | None,
        dependency: str | None = None,
        timed_out: bool = False,
    ) -> None:
        attributes = {
            "http.request.method": method,
            "http.route": route,
            "http.request.direction": "server" if metric_name == "http.server.request" else "client",
        }
        if status_code is not None:
            attributes["http.response.status_code"] = status_code
        if dependency:
            attributes["peer.service"] = dependency
        if timed_out:
            attributes["http.request.timeout"] = True
        self.request_counter.add(1, attributes)
        self.duration_histogram.record(duration_ms, attributes)
        self.emit({
            "type": "metric",
            "metric_name": metric_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "route": route,
            "status_code": status_code,
            "duration_ms": round(max(0.0, duration_ms), 3),
            "timed_out": timed_out,
            "dependency": dependency,
            "trace_id": trace_id,
        })

    def record_log(
        self,
        *,
        level: str,
        event_type: str,
        trace_id: str | None,
        method: str | None = None,
        route: str | None = None,
        status_code: int | None = None,
        duration_ms: float | None = None,
        error_details: str | None = None,
        dependency: str | None = None,
    ) -> None:
        self.emit({
            "type": "log",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event_type": event_type,
            "trace_id": trace_id,
            "method": method,
            "route": route,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
            "error_details": (error_details or "")[:2048] or None,
            "dependency": dependency,
        })

    def emit(self, item: dict) -> bool:
        if not self._started:
            return False
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped_records += 1
            now = time.monotonic()
            if now - self._last_warning > 30:
                self._last_warning = now
                logger.warning("Telemetry queue full for %s; newest records are being dropped", self.service_id)
            return False

    def client_span(self, method: str, url: str, dependency: str | None, route: str):
        parsed = urlparse(url)
        attributes = {
            "http.request.method": method,
            "url.scheme": parsed.scheme,
            "server.address": parsed.hostname or "unknown",
            "http.route": route,
        }
        if parsed.port is not None:
            attributes["server.port"] = parsed.port
        if dependency:
            attributes["peer.service"] = dependency
        return self.tracer.start_as_current_span(
            f"{method} {dependency or parsed.path}",
            kind=SpanKind.CLIENT,
            attributes=attributes,
            record_exception=True,
            set_status_on_exception=True,
        )

    def current_trace_id(self) -> str | None:
        context = trace.get_current_span().get_span_context()
        return f"{context.trace_id:032x}" if context.is_valid else None

    def record_client_result(
        self, *, method: str, route: str, dependency: str | None, status_code: int | None,
        duration_ms: float, timed_out: bool = False, error_details: str | None = None,
    ) -> None:
        trace_id = self.current_trace_id()
        self.record_request(
            metric_name="http.client.request",
            method=method,
            route=route,
            status_code=status_code,
            duration_ms=duration_ms,
            trace_id=trace_id,
            dependency=dependency,
            timed_out=timed_out,
        )
        self.record_log(
            level="ERROR" if error_details else "INFO",
            event_type="http.client.request.failed" if error_details else "http.client.request",
            trace_id=trace_id,
            method=method,
            route=route,
            status_code=status_code,
            duration_ms=duration_ms,
            error_details=error_details,
            dependency=dependency,
        )

    def _deliver_loop(self) -> None:
        stopping = False
        while not stopping:
            try:
                first = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if first is None:
                self._queue.task_done()
                break
            batch = [first]
            while len(batch) < MAX_EXPORT_BATCH:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    self._queue.task_done()
                    stopping = True
                    break
                batch.append(item)
            try:
                self._send_batch(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _send_batch(self, items: list[dict]) -> None:
        envelope = json.dumps({"service_id": self.service_id, "items": items}).encode("utf-8")
        request = Request(
            self.endpoint,
            data=envelope,
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:
                if response.status >= 400:
                    raise URLError(f"ingestion returned HTTP {response.status}")
                response.read(256)
        except Exception as exc:
            self.dropped_records += len(items)
            now = time.monotonic()
            if now - self._last_warning > 30:
                self._last_warning = now
                logger.warning("Telemetry delivery failed for %s; dropped %d records: %s", self.service_id, len(items), exc)

    def shutdown(self) -> None:
        if not self._started:
            self.tracer_provider.shutdown()
            self.meter_provider.shutdown()
            return
        self.tracer_provider.force_flush(timeout_millis=1_000)
        self.tracer_provider.shutdown()
        deadline = time.monotonic() + 1.5
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            self._queue.put(None, timeout=0.1)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        self.meter_provider.shutdown()


def install_trace_context_propagator() -> None:
    propagate.set_global_textmap(TraceContextTextMapPropagator())


def set_active_runtime(runtime: TelemetryRuntime) -> None:
    global _active_runtime
    _active_runtime = runtime


def get_active_runtime() -> TelemetryRuntime | None:
    return _active_runtime
