from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings
from app.telemetry.storage import TelemetryRepository, TelemetryStorageError

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

SERVICE_NAMES = {
    "api-gateway": "API Gateway",
    "user-service": "User Service",
    "order-service": "Order Service",
    "database-service": "Database Service",
}
SERVICE_ALIASES = {name.casefold(): service_id for service_id, name in SERVICE_NAMES.items()}
SERVICE_ALIASES.update({service_id: service_id for service_id in SERVICE_NAMES})
ServiceId = Literal["api-gateway", "user-service", "order-service", "database-service"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
AttributeValue = Union[str, int, float, bool]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricItem(StrictModel):
    type: Literal["metric"]
    metric_name: Literal["http.server.request", "http.client.request"]
    timestamp: datetime
    method: str = Field(min_length=1, max_length=20)
    route: str = Field(min_length=1, max_length=512)
    status_code: int | None = Field(default=None, ge=100, le=599)
    duration_ms: float = Field(ge=0, le=3_600_000)
    timed_out: bool = False
    dependency: ServiceId | None = None
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{32}$")

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value


class TraceEvent(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    timestamp: datetime
    attributes: dict[str, AttributeValue] = Field(default_factory=dict, max_length=64)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must include a timezone")
        return value


class TraceItem(StrictModel):
    type: Literal["trace"]
    timestamp: datetime
    end_time: datetime
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-fA-F]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{16}$")
    name: str = Field(min_length=1, max_length=256)
    kind: Literal["INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER"]
    status: Literal["UNSET", "OK", "ERROR"]
    status_description: str | None = Field(default=None, max_length=2048)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict, max_length=64)
    events: list[TraceEvent] = Field(default_factory=list, max_length=32)

    @field_validator("timestamp", "end_time")
    @classmethod
    def span_times_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("span timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def end_cannot_precede_start(self) -> "TraceItem":
        if self.end_time < self.timestamp:
            raise ValueError("end_time cannot precede timestamp")
        return self


class LogItem(StrictModel):
    type: Literal["log"]
    timestamp: datetime
    level: LogLevel
    event_type: str = Field(min_length=1, max_length=128)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{32}$")
    method: str | None = Field(default=None, max_length=20)
    route: str | None = Field(default=None, max_length=512)
    status_code: int | None = Field(default=None, ge=100, le=599)
    duration_ms: float | None = Field(default=None, ge=0, le=3_600_000)
    error_details: str | None = Field(default=None, max_length=2048)
    dependency: ServiceId | None = None

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value


TelemetryItem = Annotated[Union[MetricItem, TraceItem, LogItem], Field(discriminator="type")]


class TelemetryBatch(StrictModel):
    service_id: ServiceId
    items: list[TelemetryItem] = Field(min_length=1, max_length=200)

    @field_validator("service_id", mode="before")
    @classmethod
    def normalize_service_name(cls, value: object) -> object:
        if isinstance(value, str):
            return SERVICE_ALIASES.get(value.strip().casefold(), value.strip())
        return value


telemetry_repository = TelemetryRepository(
    settings.monitoring_database_path,
    settings.application_database_path,
)


@router.post("/ingest", status_code=202, summary="Ingest a validated telemetry batch")
def ingest(batch: TelemetryBatch) -> dict:
    try:
        _, duplicates = telemetry_repository.insert_batch(
            batch.service_id,
            [item.model_dump(mode="json") for item in batch.items],
        )
    except TelemetryStorageError as exc:
        raise HTTPException(status_code=503, detail="Telemetry storage is unavailable") from exc
    return {
        "status": "accepted",
        "service_id": batch.service_id,
        "service_name": SERVICE_NAMES[batch.service_id],
        "accepted": len(batch.items),
        "dropped_oldest": 0,
        "duplicate_count": duplicates,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/recent", summary="Inspect recently received telemetry")
def recent(
    limit: int = Query(default=100, ge=1, le=500),
    service_id: ServiceId | None = None,
) -> dict:
    try:
        records, count = telemetry_repository.recent(limit, service_id)
    except TelemetryStorageError as exc:
        raise HTTPException(status_code=503, detail="Telemetry storage is unavailable") from exc
    return {
        "count": len(records),
        "stored_records": count,
        "capacity": None,
        "dropped_oldest": 0,
        "items": records,
    }


@router.get("/summary", summary="Summarize persisted request metrics")
def summary(service_id: ServiceId | None = None) -> dict:
    try:
        return telemetry_repository.summary(service_id)
    except TelemetryStorageError as exc:
        raise HTTPException(status_code=503, detail="Telemetry storage is unavailable") from exc
