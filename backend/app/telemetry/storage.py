"""SQLite persistence for monitoring telemetry and worker checkpoints."""

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TelemetryStorageError(RuntimeError):
    """Raised when monitoring telemetry cannot be persisted or read."""


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _timestamp(value: datetime | str) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Telemetry timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _window(timestamp: str) -> str:
    value = datetime.fromisoformat(timestamp).astimezone(timezone.utc)
    return value.replace(second=0, microsecond=0).isoformat()


class TelemetryRepository:
    """Owns the platform monitoring DB; never opens the simulated app DB."""

    def __init__(self, monitoring_path: Path | str, application_path: Path | str):
        self.path = _resolved(monitoring_path)
        self.application_path = _resolved(application_path)
        if self.path == self.application_path:
            raise ValueError("Monitoring and simulated application database paths must be different")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS telemetry_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            service_id TEXT NOT NULL,
            record_type TEXT NOT NULL CHECK (record_type IN ('metric', 'trace', 'log')),
            timestamp TEXT NOT NULL,
            received_at TEXT NOT NULL,
            trace_id TEXT,
            span_id TEXT,
            metric_name TEXT,
            method TEXT,
            route TEXT,
            status_code INTEGER,
            duration_ms REAL,
            timed_out INTEGER NOT NULL DEFAULT 0,
            dependency TEXT,
            processing_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (processing_status IN ('pending', 'processed')),
            processed_at TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_telemetry_service_time
            ON telemetry_records(service_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp
            ON telemetry_records(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_telemetry_trace_id
            ON telemetry_records(trace_id) WHERE trace_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_telemetry_processing
            ON telemetry_records(processing_status, id);
        CREATE TABLE IF NOT EXISTS telemetry_windows (
            service_id TEXT NOT NULL,
            window_start TEXT NOT NULL,
            record_type TEXT NOT NULL,
            metric_name TEXT NOT NULL DEFAULT '',
            record_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            total_latency_ms REAL NOT NULL DEFAULT 0,
            max_latency_ms REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (service_id, window_start, record_type, metric_name)
        );
        CREATE TABLE IF NOT EXISTS processing_checkpoints (
            worker_name TEXT PRIMARY KEY,
            last_record_id INTEGER NOT NULL DEFAULT 0,
            processed_records INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO processing_checkpoints
            (worker_name, last_record_id, processed_records, updated_at)
            VALUES ('telemetry-aggregation', 0, 0, '1970-01-01T00:00:00+00:00');
        """
        try:
            with closing(self._connect()) as connection:
                connection.executescript(schema)
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not initialize monitoring database") from exc

    @staticmethod
    def _event_id(service_id: str, item: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"service_id": service_id, "item": item},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def insert_batch(self, service_id: str, items: list[dict[str, Any]]) -> tuple[int, int]:
        received_at = datetime.now(timezone.utc).isoformat()
        rows = []
        for item in items:
            record = dict(item)
            record["timestamp"] = _timestamp(record["timestamp"])
            if "end_time" in record:
                record["end_time"] = _timestamp(record["end_time"])
            for event in record.get("events", []):
                event["timestamp"] = _timestamp(event["timestamp"])
            event_id = self._event_id(service_id, record)
            rows.append((
                event_id, service_id, record["type"], record["timestamp"], received_at,
                record.get("trace_id"), record.get("span_id"), record.get("metric_name"),
                record.get("method"), record.get("route"), record.get("status_code"),
                record.get("duration_ms"), int(bool(record.get("timed_out", False))),
                record.get("dependency"), json.dumps(record, sort_keys=True, separators=(",", ":")),
            ))
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                before = connection.total_changes
                connection.executemany(
                    """INSERT INTO telemetry_records
                    (event_id, service_id, record_type, timestamp, received_at, trace_id,
                     span_id, metric_name, method, route, status_code, duration_ms,
                     timed_out, dependency, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO NOTHING""",
                    rows,
                )
                inserted = connection.total_changes - before
                connection.commit()
                return inserted, len(rows) - inserted
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            logger.exception("Telemetry batch persistence failed for service %s", service_id)
            raise TelemetryStorageError("Could not persist telemetry batch") from exc

    def recent(self, limit: int, service_id: str | None = None) -> tuple[list[dict], int]:
        try:
            with closing(self._connect()) as connection:
                total = connection.execute("SELECT COUNT(*) FROM telemetry_records").fetchone()[0]
                if service_id:
                    rows = connection.execute(
                        "SELECT * FROM telemetry_records WHERE service_id = ? ORDER BY id DESC LIMIT ?",
                        (service_id, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM telemetry_records ORDER BY id DESC LIMIT ?", (limit,)
                    ).fetchall()
            results = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                payload.update(
                    id=row["id"], event_id=row["event_id"], service_id=row["service_id"],
                    service_name=_service_name(row["service_id"]), received_at=row["received_at"],
                    processing_status=row["processing_status"],
                )
                results.append(payload)
            return results, total
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise TelemetryStorageError("Could not read recent telemetry") from exc

    def summary(self, service_id: str | None = None) -> dict:
        try:
            with closing(self._connect()) as connection:
                where = "WHERE record_type = 'metric'"
                params: tuple = ()
                if service_id:
                    where += " AND service_id = ?"
                    params = (service_id,)
                rows = connection.execute(
                    f"""SELECT service_id, metric_name, route, method, dependency,
                        COUNT(*) AS request_count,
                        SUM(CASE WHEN timed_out = 1 OR status_code >= 400 THEN 1 ELSE 0 END) AS failed_count,
                        SUM(timed_out) AS timeout_count, AVG(duration_ms) AS average_latency_ms,
                        MIN(duration_ms) AS min_latency_ms, MAX(duration_ms) AS max_latency_ms
                    FROM telemetry_records {where}
                    GROUP BY service_id, metric_name, route, method, dependency
                    ORDER BY service_id, metric_name, route, method""", params
                ).fetchall()
                status_rows = connection.execute(
                    f"""SELECT service_id, metric_name, route, method, dependency,
                        status_code, COUNT(*) AS amount FROM telemetry_records {where}
                        GROUP BY service_id, metric_name, route, method, dependency, status_code""",
                    params,
                ).fetchall()
                latency_rows = connection.execute(
                    f"SELECT service_id, metric_name, route, method, dependency, duration_ms "
                    f"FROM telemetry_records {where} ORDER BY duration_ms", params
                ).fetchall()
                total = connection.execute("SELECT COUNT(*) FROM telemetry_records").fetchone()[0]
                processed = connection.execute(
                    "SELECT processed_records FROM processing_checkpoints WHERE worker_name = ?",
                    ("telemetry-aggregation",),
                ).fetchone()
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not summarize telemetry") from exc

        key_fields = ("service_id", "metric_name", "route", "method", "dependency")
        status_counts: dict[tuple, dict[str, int]] = defaultdict(dict)
        for item in status_rows:
            if item["status_code"] is not None:
                key = tuple(item[field] for field in key_fields)
                status_counts[key][str(item["status_code"])] = item["amount"]
        latencies: dict[tuple, list[float]] = defaultdict(list)
        for item in latency_rows:
            key = tuple(item[field] for field in key_fields)
            latencies[key].append(item["duration_ms"])
        metrics = []
        for row in rows:
            key = tuple(row[field] for field in key_fields)
            values = latencies[key]
            metrics.append({
                **{field: row[field] for field in key_fields},
                "service_name": _service_name(row["service_id"]),
                "request_count": row["request_count"],
                "success_count": row["request_count"] - row["failed_count"],
                "failed_count": row["failed_count"],
                "error_rate": row["failed_count"] / row["request_count"],
                "status_code_counts": status_counts[key],
                "timeout_count": row["timeout_count"] or 0,
                "average_latency_ms": row["average_latency_ms"] or 0.0,
                "min_latency_ms": row["min_latency_ms"] or 0.0,
                "max_latency_ms": row["max_latency_ms"] or 0.0,
                "p50_latency_ms": _percentile(values, 0.50),
                "p95_latency_ms": _percentile(values, 0.95),
            })
        checkpoint_records = 0
        if processed:
            checkpoint_records = processed[0]
        return {
            "stored_records": total,
            "capacity": None,
            "dropped_oldest": 0,
            "processed_records": checkpoint_records,
            "metrics": metrics,
        }

    def process_batch(self, batch_size: int = 100) -> int:
        """Aggregate and mark one batch atomically; SQLite serializes competing workers."""
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT * FROM telemetry_records WHERE processing_status = 'pending' ORDER BY id LIMIT ?",
                    (batch_size,),
                ).fetchall()
                if not rows:
                    connection.commit()
                    return 0
                now = datetime.now(timezone.utc).isoformat()
                aggregates: dict[tuple, dict[str, float | int]] = {}
                for row in rows:
                    metric_name = row["metric_name"] or ""
                    key = (row["service_id"], _window(row["timestamp"]), row["record_type"], metric_name)
                    aggregate = aggregates.setdefault(key, {"count": 0, "failed": 0, "latency": 0.0, "max": 0.0})
                    aggregate["count"] += 1
                    if row["record_type"] == "metric":
                        aggregate["failed"] += int(bool(row["timed_out"]) or (row["status_code"] or 0) >= 400)
                        latency = row["duration_ms"] or 0.0
                        aggregate["latency"] += latency
                        aggregate["max"] = max(aggregate["max"], latency)
                for (sid, window, record_type, metric_name), aggregate in aggregates.items():
                    connection.execute(
                        """INSERT INTO telemetry_windows
                        (service_id, window_start, record_type, metric_name, record_count,
                         failed_count, total_latency_ms, max_latency_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(service_id, window_start, record_type, metric_name) DO UPDATE SET
                            record_count = record_count + excluded.record_count,
                            failed_count = failed_count + excluded.failed_count,
                            total_latency_ms = total_latency_ms + excluded.total_latency_ms,
                            max_latency_ms = MAX(max_latency_ms, excluded.max_latency_ms)""",
                        (sid, window, record_type, metric_name, aggregate["count"], aggregate["failed"],
                         aggregate["latency"], aggregate["max"]),
                    )
                highest_id = rows[-1]["id"]
                connection.execute(
                    "UPDATE telemetry_records SET processing_status = 'processed', processed_at = ? "
                    "WHERE id IN (" + ",".join("?" for _ in rows) + ") AND processing_status = 'pending'",
                    (now, *(row["id"] for row in rows)),
                )
                connection.execute(
                    """UPDATE processing_checkpoints SET last_record_id = ?,
                        processed_records = processed_records + ?, updated_at = ?
                        WHERE worker_name = 'telemetry-aggregation'""",
                    (highest_id, len(rows), now),
                )
                connection.commit()
                return len(rows)
        except (sqlite3.Error, OSError, ValueError) as exc:
            logger.exception("Telemetry processing batch failed; records remain pending")
            raise TelemetryStorageError("Could not process telemetry batch") from exc

    def checkpoint(self) -> dict:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM processing_checkpoints WHERE worker_name = 'telemetry-aggregation'"
                ).fetchone()
            return dict(row) if row else {"last_record_id": 0, "processed_records": 0}
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not read telemetry checkpoint") from exc


def _service_name(service_id: str) -> str:
    return {
        "api-gateway": "API Gateway",
        "user-service": "User Service",
        "order-service": "Order Service",
        "database-service": "Database Service",
    }.get(service_id, service_id)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, int((percentile * len(ordered) + 0.999999)) - 1)
    return ordered[index]
