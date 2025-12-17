# services/cron_evaluator.py
import time
import datetime
import zoneinfo
import os
from database import get_db, init_db
from config import TTL_SECONDS

DEVICE_OFFLINE_SECONDS = 1200
STILL_INTERVAL_SECONDS = 600

def iso_now(ts=None):
    tz = zoneinfo.ZoneInfo("Pacific/Apia")
    return datetime.datetime.fromtimestamp(
        ts or time.time(), tz
    ).isoformat(timespec="seconds")

def run_evaluator():
    init_db()
    now = int(time.time())
    conn = get_db()

    # --- Device offline detection ---
    rows = conn.execute(
        "SELECT device_key, online, last_seen_ts FROM device_states"
    ).fetchall()

    for key, online, last_seen in rows:
        if online == 1 and last_seen and now - last_seen > DEVICE_OFFLINE_SECONDS:
            conn.execute(
                "UPDATE device_states SET online=0, state='offline', last_offline_ts=? WHERE device_key=?",
                (now, key)
            )
            conn.execute(
                "INSERT INTO notifications(type, device_ident, created_at) VALUES (?,?,?)",
                ("device_offline", key, iso_now())
            )

    # --- Beacon TTL + still ---
    beacons = conn.execute(
        "SELECT beacon_key, state, last_seen_ts, last_still_ts FROM beacon_states"
    ).fetchall()

    for key, state, last_seen, last_still in beacons:
        if last_seen and now - last_seen > TTL_SECONDS and state != "out":
            conn.execute(
                "UPDATE beacon_states SET state='out', last_change_ts=?, last_still_ts=NULL WHERE beacon_key=?",
                (now, key)
            )
            conn.execute(
                "INSERT INTO notifications(type, beacon_name, created_at) VALUES (?,?,?)",
                ("left", key, iso_now())
            )
        elif state and last_seen and now - last_seen > STILL_INTERVAL_SECONDS:
            if not last_still or now - last_still > STILL_INTERVAL_SECONDS:
                conn.execute(
                    "UPDATE beacon_states SET last_still_ts=? WHERE beacon_key=?",
                    (now, key)
                )
                conn.execute(
                    "INSERT INTO notifications(type, beacon_name, created_at) VALUES (?,?,?)",
                    (f"still_{state}", key, iso_now())
                )

    conn.commit()
    conn.close()
