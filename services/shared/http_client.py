import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from opentelemetry import propagate
from opentelemetry.trace import Status, StatusCode

from services.telemetry.runtime import get_active_runtime

REQUEST_TIMEOUT_SECONDS = 2.5


class DownstreamError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def path_segment(value: str | int) -> str:
    return quote(str(value), safe="")


def request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    *,
    dependency: str | None = None,
    route: str | None = None,
) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    route = route or urlparse(url).path or "/"
    runtime = get_active_runtime()
    span_context = runtime.client_span(method, url, dependency, route) if runtime else _null_span()
    started = time.perf_counter()
    with span_context as span:
        carrier: dict[str, str] = {}
        if runtime:
            propagate.inject(carrier)
        headers = {"Accept": "application/json", "Content-Type": "application/json", **carrier}
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status_code = response.status
                content = response.read()
            if span is not None:
                span.set_attribute("http.response.status_code", status_code)
            duration_ms = (time.perf_counter() - started) * 1_000
            _record_client(runtime, method, route, dependency, status_code, duration_ms)
        except HTTPError as exc:
            with exc:
                try:
                    error_body = json.loads(exc.read().decode("utf-8"))
                    detail = str(error_body.get("detail", f"Downstream returned HTTP {exc.code}"))
                except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                    detail = f"Downstream returned HTTP {exc.code}"
            duration_ms = (time.perf_counter() - started) * 1_000
            if span is not None:
                span.set_attribute("http.response.status_code", exc.code)
                span.set_status(Status(StatusCode.ERROR, detail))
            _record_client(runtime, method, route, dependency, exc.code, duration_ms, error=detail)
            raise DownstreamError(exc.code, detail) from exc
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(reason, (TimeoutError, socket.timeout))
            detail = f"Downstream service unavailable: {reason}"
            duration_ms = (time.perf_counter() - started) * 1_000
            if span is not None:
                span.set_attribute("error.type", type(reason).__name__)
                span.set_status(Status(StatusCode.ERROR, detail))
            _record_client(runtime, method, route, dependency, 503, duration_ms, error=detail, timed_out=timed_out)
            raise DownstreamError(503, detail) from exc

        try:
            result = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if span is not None:
                span.set_status(Status(StatusCode.ERROR, "Downstream service returned invalid JSON"))
            raise DownstreamError(502, "Downstream service returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise DownstreamError(502, "Downstream service returned an unexpected response")
        return result


class _null_span:
    def __enter__(self):
        return None

    def __exit__(self, *_):
        return False


def _record_client(runtime, method, route, dependency, status_code, duration_ms, error=None, timed_out=False):
    if runtime is None:
        return
    try:
        runtime.record_client_result(
            method=method,
            route=route,
            dependency=dependency,
            status_code=status_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            error_details=error,
        )
    except Exception:
        # Telemetry collection must never turn an otherwise successful call into an application failure.
        return


