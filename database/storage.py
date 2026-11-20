import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from services.shared.config import settings
from services.shared.faults import get_active_fault_manager


SAMPLE_USERS = (
    ("user-1001", "Avery Chen", "avery.chen@example.local"),
    ("user-1002", "Jordan Patel", "jordan.patel@example.local"),
)


class SimulatedDatabaseUnavailable(RuntimeError):
    """Raised by the local fault hook before opening the simulated SQLite DB."""


def database_path() -> Path:
    """Return the validated simulated DB path; monitoring storage is never opened here."""
    path = settings.application_database_path
    if path == settings.monitoring_database_path:
        raise RuntimeError("Refusing to open platform monitoring database as simulated data")
    return path


@contextmanager
def connect():
    path = database_path()
    manager = get_active_fault_manager()
    if manager is not None and manager.is_active("database-service", "database_connection_failure"):
        raise SimulatedDatabaseUnavailable("Simulated application database connection is unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                item TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT OR IGNORE INTO users (id, name, email) VALUES (?, ?, ?)",
            SAMPLE_USERS,
        )


def get_user(user_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT id, name, email FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def create_order(user_id: str, item: str, quantity: int) -> dict:
    created_at = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO orders (user_id, item, quantity, created_at) VALUES (?, ?, ?, ?)",
            (user_id, item, quantity, created_at),
        )
        order_id = cursor.lastrowid
        row = connection.execute(
            "SELECT id, user_id, item, quantity, status, created_at FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    return dict(row)


def get_order(order_id: int) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT id, user_id, item, quantity, status, created_at FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    return dict(row) if row else None


def check_database() -> dict[str, str]:
    with connect() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"database": "available"}
