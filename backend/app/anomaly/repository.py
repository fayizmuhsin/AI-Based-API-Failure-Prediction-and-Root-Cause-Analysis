"""SQLite access for completed windows, model metadata and anomaly history."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.telemetry.storage import TelemetryStorageError, _resolved


class AnomalyRepository:
    def __init__(self, monitoring_path: Path | str, application_path: Path | str):
        self.path = _resolved(monitoring_path)
        self.application_path = _resolved(application_path)
        if self.path == self.application_path:
            raise ValueError("Monitoring and simulated application database paths must be different")

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise TelemetryStorageError("Monitoring database has not been initialized")
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS model_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK (state IN ('ready', 'not_ready')),
            trained_at TEXT NOT NULL,
            feature_names_json TEXT NOT NULL,
            training_sample_count INTEGER NOT NULL,
            model_config_json TEXT NOT NULL,
            training_data_end TEXT,
            model_blob BLOB,
            is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_active
            ON model_metadata(is_active) WHERE is_active = 1;
        CREATE TABLE IF NOT EXISTS detection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id TEXT NOT NULL,
            window_start TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            model_version TEXT,
            model_score REAL,
            model_decision TEXT NOT NULL,
            run_status TEXT NOT NULL CHECK (run_status IN ('completed', 'skipped')),
            is_anomaly INTEGER NOT NULL CHECK (is_anomaly IN (0, 1)),
            threshold_matches_json TEXT NOT NULL,
            feature_values_json TEXT NOT NULL,
            UNIQUE (service_id, window_start)
        );
        CREATE INDEX IF NOT EXISTS idx_detection_service_window
            ON detection_runs(service_id, window_start DESC);
        CREATE INDEX IF NOT EXISTS idx_detection_timestamp
            ON detection_runs(detected_at DESC);
        CREATE TABLE IF NOT EXISTS anomaly_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL UNIQUE REFERENCES detection_runs(id) ON DELETE CASCADE,
            service_id TEXT NOT NULL,
            window_start TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
            model_score REAL,
            model_decision TEXT NOT NULL,
            threshold_matches_json TEXT NOT NULL,
            feature_values_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_anomaly_service_time
            ON anomaly_results(service_id, window_start DESC);
        CREATE INDEX IF NOT EXISTS idx_anomaly_detected_at
            ON anomaly_results(detected_at DESC);
        CREATE TABLE IF NOT EXISTS anomaly_checkpoints (
            worker_name TEXT PRIMARY KEY,
            last_window_start TEXT NOT NULL DEFAULT '',
            windows_processed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO anomaly_checkpoints
            (worker_name, last_window_start, windows_processed, updated_at)
            VALUES ('anomaly-detection', '', 0, '1970-01-01T00:00:00+00:00');
        """
        try:
            with closing(self._connect()) as connection:
                connection.executescript(schema)
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not initialize anomaly tables") from exc

    @staticmethod
    def _window_expression() -> str:
        return "substr(timestamp, 1, 16) || ':00+00:00'"

    def completed_window_keys(self, cutoff: datetime, limit: int = 100) -> list[tuple[str, str]]:
        cutoff_text = cutoff.astimezone(timezone.utc).isoformat()
        expression = self._window_expression()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    f"""SELECT DISTINCT t.service_id, {expression} AS window_start
                    FROM telemetry_records AS t
                    WHERE t.record_type = 'metric' AND t.metric_name = 'http.server.request'
                      AND t.processing_status = 'processed' AND t.timestamp < ?
                      AND NOT EXISTS (
                        SELECT 1 FROM detection_runs AS d
                        WHERE d.service_id = t.service_id AND d.window_start = {expression}
                      )
                    ORDER BY window_start ASC, t.service_id ASC LIMIT ?""",
                    (cutoff_text, limit),
                ).fetchall()
            return [(row["service_id"], row["window_start"]) for row in rows]
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not retrieve completed telemetry windows") from exc

    def historical_window_keys(self, cutoff: datetime, limit: int = 5000) -> list[tuple[str, str]]:
        cutoff_text = cutoff.astimezone(timezone.utc).isoformat()
        expression = self._window_expression()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    f"""SELECT DISTINCT service_id, {expression} AS window_start
                    FROM telemetry_records
                    WHERE record_type = 'metric' AND metric_name = 'http.server.request'
                      AND processing_status = 'processed' AND timestamp < ?
                    ORDER BY window_start DESC, service_id LIMIT ?""",
                    (cutoff_text, limit),
                ).fetchall()
            return sorted(
                [(row["service_id"], row["window_start"]) for row in rows],
                key=lambda item: (item[1], item[0]),
            )
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not retrieve historical telemetry windows") from exc

    def records_for_windows(self, keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
        if not keys:
            return []
        clauses = []
        params: list[Any] = []
        for service_id, start in keys:
            end = (datetime.fromisoformat(start) + timedelta(minutes=1)).isoformat()
            clauses.append("(service_id = ? AND timestamp >= ? AND timestamp < ?)")
            params.extend((service_id, start, end))
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT service_id, record_type AS type, timestamp, metric_name, method, route, "
                    "status_code, duration_ms, timed_out, dependency FROM telemetry_records "
                    "WHERE record_type = 'metric' AND (" + " OR ".join(clauses) + ")",
                    params,
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not load telemetry features") from exc

    def historical_aggregates(self, service_id: str, before: str, limit: int = 60) -> list[dict]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """SELECT window_start, record_count, total_latency_ms
                    FROM telemetry_windows
                    WHERE service_id = ? AND record_type = 'metric'
                      AND metric_name = 'http.server.request' AND window_start < ?
                    ORDER BY window_start DESC LIMIT ?""",
                    (service_id, before, limit),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not load historical telemetry aggregates") from exc

    def known_anomalous_windows(self) -> set[tuple[str, str]]:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT service_id, window_start FROM detection_runs WHERE is_anomaly = 1"
                ).fetchall()
            return {(row["service_id"], row["window_start"]) for row in rows}
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not read known anomalous windows") from exc

    def record_detection(
        self,
        *,
        service_id: str,
        window_start: str,
        model_version: str | None,
        model_score: float | None,
        model_decision: str,
        threshold_matches: list[dict],
        feature_values: dict,
        is_anomaly: bool,
        severity: str,
    ) -> tuple[bool, int | None]:
        now = datetime.now(timezone.utc).isoformat()
        matches_json = json.dumps(threshold_matches, sort_keys=True, separators=(",", ":"))
        features_json = json.dumps(feature_values, sort_keys=True, separators=(",", ":"))
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """INSERT INTO detection_runs
                    (service_id, window_start, detected_at, model_version, model_score,
                     model_decision, run_status, is_anomaly, threshold_matches_json, feature_values_json)
                    VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)
                    ON CONFLICT(service_id, window_start) DO NOTHING""",
                    (service_id, window_start, now, model_version, model_score, model_decision,
                     int(is_anomaly), matches_json, features_json),
                )
                if cursor.rowcount == 0:
                    connection.rollback()
                    return False, None
                run_id = int(cursor.lastrowid)
                anomaly_id = None
                if is_anomaly:
                    connection.execute(
                        """INSERT INTO anomaly_results
                        (run_id, service_id, window_start, detected_at, severity, model_score,
                         model_decision, threshold_matches_json, feature_values_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (run_id, service_id, window_start, now, severity, model_score,
                         model_decision, matches_json, features_json),
                    )
                    anomaly_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                connection.execute(
                    """UPDATE anomaly_checkpoints
                    SET last_window_start = CASE
                        WHEN last_window_start < ? THEN ? ELSE last_window_start END,
                        windows_processed = windows_processed + 1, updated_at = ?
                    WHERE worker_name = 'anomaly-detection'""",
                    (window_start, window_start, now),
                )
                connection.commit()
                return True, anomaly_id
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not persist anomaly detection result") from exc

    def save_model(self, model_bytes: bytes, metadata: dict[str, Any], training_data_end: str | None) -> None:
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE model_metadata SET is_active = 0 WHERE is_active = 1")
                connection.execute(
                    """INSERT INTO model_metadata
                    (model_version, state, trained_at, feature_names_json, training_sample_count,
                     model_config_json, training_data_end, model_blob, is_active)
                    VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        metadata["model_version"], metadata["trained_at"],
                        json.dumps(metadata["feature_names"]), metadata["training_sample_count"],
                        json.dumps(metadata["model_config"], sort_keys=True),
                        training_data_end, sqlite3.Binary(model_bytes),
                    ),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not persist trained anomaly model") from exc

    def active_model(self) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM model_metadata WHERE is_active = 1 AND state = 'ready'"
                ).fetchone()
            if not row:
                return None
            return {
                "metadata": {
                    "model_version": row["model_version"],
                    "trained_at": row["trained_at"],
                    "feature_names": json.loads(row["feature_names_json"]),
                    "training_sample_count": row["training_sample_count"],
                    "model_config": json.loads(row["model_config_json"]),
                },
                "model_blob": bytes(row["model_blob"]),
            }
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise TelemetryStorageError("Could not load persisted anomaly model") from exc

    def model_status(self) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM model_metadata WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        "SELECT * FROM model_metadata ORDER BY id DESC LIMIT 1"
                    ).fetchone()
            if not row:
                return None
            return {
                "model_version": row["model_version"],
                "state": row["state"],
                "trained_at": row["trained_at"],
                "feature_names": json.loads(row["feature_names_json"]),
                "training_sample_count": row["training_sample_count"],
                "model_config": json.loads(row["model_config_json"]),
                "training_data_end": row["training_data_end"],
            }
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise TelemetryStorageError("Could not read anomaly model status") from exc

    def recent_anomalies(self, limit: int, service_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM anomaly_results"
        params: list[Any] = []
        if service_id:
            query += " WHERE service_id = ?"
            params.append(service_id)
        query += " ORDER BY window_start DESC, id DESC LIMIT ?"
        params.append(limit)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(query, params).fetchall()
            return [self._anomaly_dict(row, include_evidence=False) for row in rows]
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise TelemetryStorageError("Could not retrieve recent anomaly results") from exc

    def anomaly_summary(self) -> dict:
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """SELECT service_id, COUNT(*) AS anomaly_count,
                        SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical_count,
                        SUM(CASE WHEN severity = 'high' THEN 1 ELSE 0 END) AS high_count,
                        SUM(CASE WHEN severity = 'medium' THEN 1 ELSE 0 END) AS medium_count,
                        SUM(CASE WHEN severity = 'low' THEN 1 ELSE 0 END) AS low_count,
                        MAX(window_start) AS latest_window
                    FROM anomaly_results GROUP BY service_id ORDER BY service_id"""
                ).fetchall()
                total = connection.execute("SELECT COUNT(*) FROM anomaly_results").fetchone()[0]
            return {
                "total_anomalies": total,
                "by_service": [dict(row) for row in rows],
            }
        except sqlite3.Error as exc:
            raise TelemetryStorageError("Could not summarize anomaly results") from exc

    def anomaly_detail(self, anomaly_id: int) -> dict[str, Any] | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM anomaly_results WHERE id = ?", (anomaly_id,)
                ).fetchone()
            return self._anomaly_dict(row, include_evidence=True) if row else None
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise TelemetryStorageError("Could not retrieve anomaly detail") from exc

    @staticmethod
    def _anomaly_dict(row: sqlite3.Row, include_evidence: bool) -> dict[str, Any]:
        result = {
            "anomaly_id": row["id"],
            "service_id": row["service_id"],
            "window_start": row["window_start"],
            "detected_at": row["detected_at"],
            "severity": row["severity"],
            "model_score": row["model_score"],
            "model_decision": row["model_decision"],
            "threshold_matches": json.loads(row["threshold_matches_json"]),
        }
        if include_evidence:
            result["feature_values"] = json.loads(row["feature_values_json"])
        return result
