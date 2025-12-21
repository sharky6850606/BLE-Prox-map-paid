"""
Cron evaluator for ProxMap (SQLite + Render Disk)

Run this every 1 minute via a Render Cron Job OR call `run_evaluator()` from a background thread.
It evaluates:
- Device offline based on last_seen_ts
- Beacon TTL-based LEFT transitions
- Beacon STILL IN/OUT status pings every STILL_INTERVAL_SECONDS

Safe to run repeatedly (idempotent / deduped via state tables).
"""

import os
import time
import sqlite3

from database import get_db, init_db
from config import TTL_SECONDS

DEVICE_OFFLINE_SECONDS = int(os.getenv("DEVICE_OFFLINE_SECONDS", "1200"))  # 20 min
STILL_INTERVAL_SECONDS = int(os.getenv("STILL_INTERVAL_SECONDS", "600"))   # 10 min


def ensure_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            beacon_name TEXT,
            event_time TEXT,
            distance REAL,
            created_at TEXT,
            beacon_id TEXT,
            device_ident TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS beacon_states (
            beacon_key TEXT PRIMARY KEY,
            state TEXT,
            last_change_ts INTEGER,
            last_still_ts INTEGER,
            device_ident TEXT,
            last_seen_ts INTEGER,
            active INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_states (
            device_key TEXT PRIMARY KEY,
            state TEXT,
            last_change_ts INTEGER,
            device_ident TEXT,
            online INTEGER,
            last_seen_ts INTEGER,
            last_online_ts INTEGER,
            last_offline_ts INTEGER
        )
        """
    )


def iso_now(ts: int | None = None) -> str:
    import datetime, zoneinfo
    tz = zoneinfo.ZoneInfo("Pacific/Apia")
    dt = datetime.datetime.fromtimestamp(ts or time.time(), tz=tz)
    return dt.isoformat(timespec="seconds")


def insert_notification(
    conn,
    ntype: str,
    beacon_name: str | None,
    beacon_id: str | None,
    device_ident: str | None,
    event_time_iso: str | None,
    distance=None,
):
    created_at = iso_now()
    conn.execute(
        """
        INSERT INTO notifications (type, beacon_name, event_time, distance, created_at, beacon_id, device_ident)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ntype, beacon_name, event_time_iso, distance, created_at, beacon_id, device_ident),
    )


def main():
    # DB I/O resilience: retry on transient sqlite errors (Render Disk can occasionally spike)
    for attempt in range(3):
        conn = None
        try:
            init_db()
            now_ts = int(time.time())
            conn = get_db()
            ensure_tables(conn)

            # --- Device offline evaluation ---
            dev_rows = conn.execute(
                "SELECT device_key, online, last_seen_ts FROM device_states"
            ).fetchall()

            for device_key, online, last_seen_ts in dev_rows:
                if last_seen_ts is None:
                    continue
                online = int(online) if online is not None else 0
                if online == 1 and (now_ts - int(last_seen_ts)) > DEVICE_OFFLINE_SECONDS:
                    # transition to offline
                    conn.execute(
                        "UPDATE device_states SET online = 0, state = 'offline', last_change_ts = ?, last_offline_ts = ? WHERE device_key = ?",
                        (now_ts, now_ts, device_key),
                    )
                    insert_notification(conn, "device_offline", None, None, device_key, iso_now(now_ts))

                    # deactivate beacons for this device
                    conn.execute(
                        "UPDATE beacon_states SET active = 0 WHERE device_ident = ?",
                        (device_key,),
                    )

            # --- Beacon TTL + STILL evaluation ---
            online_set = {
                r[0]
                for r in conn.execute("SELECT device_key FROM device_states WHERE online = 1").fetchall()
            }

            b_rows = conn.execute(
                "SELECT beacon_key, state, last_change_ts, last_still_ts, device_ident, last_seen_ts, active FROM beacon_states"
            ).fetchall()

            for beacon_key, state, last_change_ts, last_still_ts, device_ident, last_seen_ts, active in b_rows:
                active = int(active) if active is not None else 1

                # If device is offline, do not emit still pings (avoids noise)
                if device_ident and device_ident not in online_set:
                    continue

                # TTL evaluation
                if last_seen_ts is not None:
                    age = now_ts - int(last_seen_ts)
                    if age > TTL_SECONDS:
                        # If currently IN, force LEFT transition
                        if (state or "") != "out":
                            conn.execute(
                                "UPDATE beacon_states SET state = 'out', last_change_ts = ?, last_still_ts = NULL WHERE beacon_key = ?",
                                (now_ts, beacon_key),
                            )
                            insert_notification(
                                conn,
                                "left",
                                beacon_key,
                                beacon_key,
                                device_ident,
                                iso_now(int(last_seen_ts)),
                            )
                        continue

                # STILL evaluation
                if not state or last_change_ts is None:
                    continue
                if (now_ts - int(last_change_ts)) < STILL_INTERVAL_SECONDS:
                    continue
                if last_still_ts is not None and (now_ts - int(last_still_ts)) < STILL_INTERVAL_SECONDS:
                    continue

                ntype = "still_in" if state == "in" else "still_out"
                conn.execute(
                    "UPDATE beacon_states SET last_still_ts = ? WHERE beacon_key = ?",
                    (now_ts, beacon_key),
                )
                insert_notification(conn, ntype, beacon_key, beacon_key, device_ident, iso_now(now_ts))

            conn.commit()
            return  # success

        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if any(s in msg for s in ("disk i/o error", "database is locked", "database is busy")) and attempt < 2:
                print(f"[warn] evaluator sqlite transient error ({e}); retrying...")
                time.sleep(0.4 * (attempt + 1))
                continue
            raise
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()


def run_evaluator():
    """Compatibility wrapper: used by single-service background thread."""
    return main()
