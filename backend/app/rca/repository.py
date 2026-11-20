"""Durable RCA work queue and investigation persistence in monitoring SQLite."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.telemetry.storage import _resolved


class RcaStorageError(RuntimeError):
    """Raised when RCA monitoring data cannot be read or committed."""


class RcaRepository:
    def __init__(self, monitoring_path: Path | str, application_path: Path | str):
        self.path = _resolved(monitoring_path)
        self.application_path = _resolved(application_path)
        if self.path == self.application_path:
            raise ValueError("Monitoring and simulated application database paths must be different")

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise RcaStorageError("Monitoring database has not been initialized")
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS rca_queue (
            anomaly_id INTEGER PRIMARY KEY REFERENCES anomaly_results(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            queued_at TEXT NOT NULL,
            processed_at TEXT,
            investigation_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rca_queue_pending ON rca_queue(status, anomaly_id);
        CREATE TABLE IF NOT EXISTS investigations (
            investigation_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            episode_start TEXT NOT NULL,
            episode_end TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('completed', 'inconclusive')),
            trigger_count INTEGER NOT NULL,
            candidate_count INTEGER NOT NULL,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_investigation_created ON investigations(created_at DESC);
        CREATE TABLE IF NOT EXISTS investigation_triggers (
            investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id) ON DELETE CASCADE,
            anomaly_id INTEGER NOT NULL UNIQUE REFERENCES anomaly_results(id),
            PRIMARY KEY (investigation_id, anomaly_id)
        );
        CREATE INDEX IF NOT EXISTS idx_investigation_triggers_anomaly ON investigation_triggers(anomaly_id);
        """
        try:
            with closing(self._connect()) as connection:
                connection.executescript(schema)
        except sqlite3.Error as exc:
            raise RcaStorageError("Could not initialize RCA tables") from exc

    def enqueue_new_anomalies(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO rca_queue(anomaly_id, queued_at)
                    SELECT a.id, ? FROM anomaly_results AS a
                    LEFT JOIN rca_queue AS q ON q.anomaly_id = a.id
                    WHERE q.anomaly_id IS NULL ORDER BY a.id""", (now,),
                )
                connection.commit()
                return cursor.rowcount
        except sqlite3.Error as exc:
            raise RcaStorageError("Could not enqueue new anomalies for RCA") from exc

    def pending_anomalies(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """SELECT a.id AS anomaly_id, a.service_id, a.window_start, a.detected_at,
                        a.severity, a.model_score, a.model_decision,
                        a.threshold_matches_json, a.feature_values_json, q.attempts
                    FROM rca_queue AS q JOIN anomaly_results AS a ON a.id = q.anomaly_id
                    WHERE q.status = 'pending' ORDER BY a.window_start, a.id LIMIT ?""", (limit,),
                ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["threshold_matches"] = json.loads(item.pop("threshold_matches_json"))
                item["feature_values"] = json.loads(item.pop("feature_values_json"))
                results.append(item)
            return results
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise RcaStorageError("Could not load pending RCA anomalies") from exc

    def telemetry_between(self, start: str, end: str, limit: int = 10000) -> list[dict[str, Any]]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """SELECT id, event_id, service_id, record_type, timestamp, trace_id,
                        span_id, payload_json FROM telemetry_records
                    WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp, id LIMIT ?""",
                    (start, end, limit),
                ).fetchall()
            result = []
            for row in rows:
                record = dict(row)
                record["payload"] = json.loads(record.pop("payload_json"))
                result.append(record)
            return result
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise RcaStorageError("Could not load telemetry evidence for RCA") from exc

    def persist_investigation(self, report: dict[str, Any], anomaly_ids: list[int]) -> bool:
        ids = sorted(set(anomaly_ids))
        if not ids:
            return False
        key = hashlib.sha256(",".join(map(str, ids)).encode("ascii")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT investigation_id FROM investigations WHERE idempotency_key = ?", (key,),
                ).fetchone()
                if existing:
                    investigation_id = existing["investigation_id"]
                else:
                    placeholders = ",".join("?" for _ in ids)
                    eligible = connection.execute(
                        f"""SELECT q.anomaly_id FROM rca_queue q
                        LEFT JOIN investigation_triggers t ON t.anomaly_id = q.anomaly_id
                        WHERE q.status = 'pending' AND q.anomaly_id IN ({placeholders})
                          AND t.anomaly_id IS NULL""", ids,
                    ).fetchall()
                    ids = sorted(row["anomaly_id"] for row in eligible)
                    if not ids:
                        connection.rollback()
                        return False
                    key = hashlib.sha256(",".join(map(str, ids)).encode("ascii")).hexdigest()
                    report["triggering_anomaly_ids"] = ids
                    report["trigger_count"] = len(ids)
                    report["created_at"] = created_at
                    investigation_id = report["investigation_id"]
                    connection.execute(
                        """INSERT INTO investigations
                        (investigation_id, idempotency_key, created_at, episode_start, episode_end,
                         status, trigger_count, candidate_count, report_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (investigation_id, key, created_at, report["episode_start"], report["episode_end"],
                         report["status"], len(ids), len(report["candidates"]),
                         json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)),
                    )
                    connection.executemany(
                        "INSERT INTO investigation_triggers(investigation_id, anomaly_id) VALUES (?, ?)",
                        [(investigation_id, anomaly_id) for anomaly_id in ids],
                    )
                now = datetime.now(timezone.utc).isoformat()
                connection.executemany(
                    """UPDATE rca_queue SET status = 'completed', attempts = attempts + 1,
                        last_error = NULL, processed_at = ?, investigation_id = ?
                    WHERE anomaly_id = ? AND status = 'pending'""",
                    [(now, investigation_id, anomaly_id) for anomaly_id in ids],
                )
                connection.commit()
                return existing is None
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise RcaStorageError("Could not persist RCA investigation") from exc

    def record_attempt_failure(self, anomaly_ids: list[int], error: str) -> None:
        if not anomaly_ids:
            return
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    """UPDATE rca_queue SET attempts = attempts + 1, last_error = ?
                    WHERE anomaly_id = ? AND status = 'pending'""",
                    [(error[:1000], anomaly_id) for anomaly_id in anomaly_ids],
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise RcaStorageError("Could not record RCA retry state") from exc

    def recent_investigations(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """SELECT investigation_id, created_at, episode_start, episode_end,
                        status, trigger_count, candidate_count, report_json FROM investigations
                    ORDER BY created_at DESC LIMIT ?""", (limit,),
                ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                try:
                    report = json.loads(item.pop("report_json"))
                except (ValueError, TypeError):
                    report = {}
                item["affected_services"] = report.get("affected_services", [])
                item["triggering_anomaly_ids"] = report.get("triggering_anomaly_ids", [])
                item["root_causes"] = [
                    {key: candidate.get(key) for key in (
                        "service_id", "service_name", "category", "rank", "rank_score",
                        "selected_as_probable_root_cause",
                    )}
                    for candidate in report.get("root_causes", [])
                ]
                item["conclusion"] = report.get("conclusion")
                items.append(item)
            return items
        except sqlite3.Error as exc:
            raise RcaStorageError("Could not retrieve recent investigations") from exc

    def investigation_detail(self, investigation_id: str) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT report_json FROM investigations WHERE investigation_id = ?", (investigation_id,),
                ).fetchone()
            return json.loads(row["report_json"]) if row else None
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise RcaStorageError("Could not retrieve investigation detail") from exc

    def investigation_summary(self) -> dict[str, Any]:
        try:
            with closing(self._connect()) as connection:
                totals = connection.execute(
                    "SELECT COUNT(*) AS total, SUM(status = 'completed') AS completed, "
                    "SUM(status = 'inconclusive') AS inconclusive FROM investigations"
                ).fetchone()
                reports = connection.execute("SELECT report_json FROM investigations").fetchall()
                pending = connection.execute("SELECT COUNT(*) FROM rca_queue WHERE status = 'pending'").fetchone()[0]
                retrying = connection.execute(
                    "SELECT COUNT(*) FROM rca_queue WHERE status = 'pending' AND attempts > 0"
                ).fetchone()[0]
            return {
                "total_investigations": totals["total"] or 0,
                "completed": totals["completed"] or 0,
                "inconclusive": totals["inconclusive"] or 0,
                "pending_anomalies": pending,
                "retrying_anomalies": retrying,
                "root_causes_by_service": self._root_cause_counts(reports),
            }
        except sqlite3.Error as exc:
            raise RcaStorageError("Could not summarize RCA investigations") from exc

    def candidates(self, investigation_id: str) -> list[dict[str, Any]] | None:
        report = self.investigation_detail(investigation_id)
        return report["candidates"] if report else None

    @staticmethod
    def _root_cause_counts(reports: list[sqlite3.Row]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in reports:
            try:
                report = json.loads(row["report_json"])
            except (ValueError, TypeError):
                continue
            for candidate in report.get("root_causes", []):
                service_id = candidate.get("service_id")
                if isinstance(service_id, str):
                    counts[service_id] = counts.get(service_id, 0) + 1
        return [
            {"service_id": service_id, "cause_count": count}
            for service_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
