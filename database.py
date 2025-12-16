import os
import sqlite3
from config import DB_PATH

def get_db():
    # Ensure parent dir exists (important when using a Render Disk at /var/data)
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # Safer concurrency settings for a web app
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn

def init_db():
    conn = get_db()
    # Beacon names table
    conn.execute("CREATE TABLE IF NOT EXISTS beacon_names (id TEXT PRIMARY KEY, name TEXT)")
    # Notifications history table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            name TEXT,
            message TEXT,
            timestamp TEXT
        )
        """
    )
    # Daily reports history table
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
    # Beacon activity report history table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            beacon_id TEXT,
            beacon_name TEXT,
            window_start TEXT,
            window_end TEXT,
            pdf_path TEXT,
            summary_json TEXT
        )
        """
    )
    # Device activity report history table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            device_id TEXT,
            device_name TEXT,
            window_start TEXT,
            window_end TEXT,
            pdf_path TEXT,
            summary_json TEXT
        )
        """
    )
    # Uptime logs table
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
