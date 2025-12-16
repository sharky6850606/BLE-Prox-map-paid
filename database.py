import os
import sqlite3

from config import DB_PATH


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def get_db() -> sqlite3.Connection:
    """Open a SQLite connection.

    - check_same_thread=False because Flask/Gunicorn can serve requests from
      different threads.
    - WAL mode improves concurrency for read-heavy workloads.
    """

    _ensure_parent_dir(DB_PATH)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        # PRAGMAs can fail on some platforms; not fatal.
        pass
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}  # r[1] is column name
    except Exception:
        return set()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    cols = _table_columns(conn, table)
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def init_db() -> None:
    """Create core tables (and patch older schemas) for SQLite + Render Disk."""

    conn = get_db()

    # --- Core lookup tables ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS beacon_names (
            id TEXT PRIMARY KEY,
            name TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT,
            color TEXT
        )
        """
    )

    # --- Notifications (history + 15-min status) ---
    # Expected by /notifications/history, analytics, and activity reports.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            beacon_name TEXT,
            event_time TEXT,
            distance REAL,
            created_at TEXT
        )
        """
    )

    # If the DB was created by an older build (missing columns), add them.
    # This prevents errors like: sqlite3.OperationalError: no such column: beacon_name
    try:
        _ensure_column(conn, "notifications", "type", "TEXT")
        _ensure_column(conn, "notifications", "beacon_name", "TEXT")
        _ensure_column(conn, "notifications", "event_time", "TEXT")
        _ensure_column(conn, "notifications", "distance", "REAL")
        _ensure_column(conn, "notifications", "created_at", "TEXT")
    except Exception:
        # If ALTER TABLE fails for any reason, we'll still have the table.
        pass

    # --- Daily reports history ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            pdf_path TEXT,
            report_json TEXT,
            summary TEXT
        )
        """
    )

    # --- Activity reports history (beacon + device both write here) ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            beacon_name TEXT,
            pdf_path TEXT,
            created_at TEXT,
            summary TEXT
        )
        """
    )
    # Patch older schemas if needed
    try:
        _ensure_column(conn, "activity_reports", "beacon_name", "TEXT")
        _ensure_column(conn, "activity_reports", "pdf_path", "TEXT")
        _ensure_column(conn, "activity_reports", "created_at", "TEXT")
        _ensure_column(conn, "activity_reports", "summary", "TEXT")
    except Exception:
        pass

    # --- Uptime logs ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uptime_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            device_count INTEGER,
            beacon_count INTEGER,
            status TEXT
        )
        """
    )

    conn.commit()
    conn.close()
