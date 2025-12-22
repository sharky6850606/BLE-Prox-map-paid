from flask import Blueprint, request
import sqlite3
import time

from database import get_db
from services.beacon_logic import simplify_message, latest_messages

flespi_
# =============================
# SIMPLE NOTIFICATION RULE (no background evaluator)
# - If a beacon distance_m > 3.0: store one OUT_OF_RANGE notification (on transition)
# - If a beacon distance_m <= 3.0: store one IN_RANGE notification (on transition)
# Uses beacon_states table to remember last in/out state.
DISTANCE_THRESHOLD_M = 3.0

def _upsert_beacon_row(conn, device_ident: str, lat, lon, b: dict):
    beacon_id = b.get("id") or "unknown"
    rssi = b.get("rssi")
    distance_m = b.get("distance")
    last_seen = b.get("last_seen")
    last_seen_ts = b.get("last_seen_raw")
    battery_pct = b.get("battery_percent")

    conn.execute(
        """INSERT INTO beacons (beacon_id, device_id, rssi, distance_m, last_seen, last_seen_ts, lat, lon, battery_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(beacon_id) DO UPDATE SET
              device_id=excluded.device_id,
              rssi=excluded.rssi,
              distance_m=excluded.distance_m,
              last_seen=excluded.last_seen,
              last_seen_ts=excluded.last_seen_ts,
              lat=excluded.lat,
              lon=excluded.lon,
              battery_pct=COALESCE(excluded.battery_pct, beacons.battery_pct)
        """,
        (beacon_id, device_ident, rssi, distance_m, last_seen, last_seen_ts, lat, lon, battery_pct)
    )

def _maybe_record_distance_transition(conn, device_ident: str, beacon_id: str, distance_m, event_time: str):
    try:
        if distance_m is None:
            return
        dist = float(distance_m)
    except Exception:
        return

    in_range = 1 if dist <= DISTANCE_THRESHOLD_M else 0

    row = conn.execute(
        "SELECT in_range FROM beacon_states WHERE beacon_id = ?",
        (beacon_id,)
    ).fetchone()
    prev = int(row["in_range"]) if row and row["in_range"] is not None else None

    # Upsert latest state
    conn.execute(
        """INSERT INTO beacon_states (beacon_id, in_range, last_changed)
           VALUES (?, ?, ?)
           ON CONFLICT(beacon_id) DO UPDATE SET
             in_range=excluded.in_range,
             last_changed=excluded.last_changed""",
        (beacon_id, in_range, event_time)
    )

    # Notify only on transitions (skip first sight)
    if prev is None or prev == in_range:
        return

    label_row = conn.execute(
        "SELECT COALESCE(name, beacon_id) AS label FROM beacons WHERE beacon_id = ?",
        (beacon_id,)
    ).fetchone()
    label = label_row["label"] if label_row else beacon_id

    if in_range == 0:
        notif_type = "OUT_OF_RANGE"
        message = f"Beacon {label} is OUT of range (> {DISTANCE_THRESHOLD_M:g}m)"
    else:
        notif_type = "IN_RANGE"
        message = f"Beacon {label} is BACK in range (<= {DISTANCE_THRESHOLD_M:g}m)"

    conn.execute(
        """INSERT INTO notifications (timestamp, level, message, beacon_id, device_id, type)
           VALUES (?, 'info', ?, ?, ?, ?)""",
        (event_time, message, beacon_id, device_ident, notif_type)
    )

bp = Blueprint("flespi", __name__)


def _ensure_device_states(conn):
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


def _extract_messages(payload):
    """
    Flespi can be configured to send either:
      1) {"data": [ ...messages... ]}
      2) [ ...messages... ]
    We accept both to avoid 400/500s.
    """
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get("data")
    if isinstance(payload, list):
        return payload
    return None


@flespi_bp.route("/flespi", methods=["POST"])
def flespi_receiver():
    # Parse payload safely (never crash on unexpected shapes)
    payload = request.get_json(silent=True)
    msgs = _extract_messages(payload)
    if not msgs:
        return "No data", 400

    now_ts = int(time.time())

    # Update in-memory latest_messages first (so the map can still move even if DB has issues)
    processed = 0
    for raw in msgs:
        if not isinstance(raw, dict):
            continue
        simplified = simplify_message(raw)
        ident = simplified.get("ident")
        if not ident:
            continue
        latest_messages[ident] = simplified
        processed += 1

    # Persist device last_seen/online state to SQLite (retry on transient sqlite errors)
    for attempt in range(3):
        conn = None
        try:
            conn = get_db()
            _ensure_device_states(conn)

            # Update only devices seen in this request
            seen_idents = set()
            for raw in msgs:
                if not isinstance(raw, dict):
                    continue
                simplified = simplify_message(raw)
                ident = simplified.get("ident")
                if not ident:
                    continue
                seen_idents.add(ident)

            for ident in seen_idents:
                row = conn.execute(
                    "SELECT online FROM device_states WHERE device_key = ?",
                    (ident,),
                ).fetchone()
                prev_online = int(row[0]) if row and row[0] is not None else None

                if prev_online != 1:
                    conn.execute(
                        """
                        INSERT INTO device_states
                        (device_key, state, last_change_ts, device_ident, online, last_seen_ts, last_online_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(device_key) DO UPDATE SET
                            state=excluded.state,
                            last_change_ts=excluded.last_change_ts,
                            device_ident=excluded.device_ident,
                            online=excluded.online,
                            last_seen_ts=excluded.last_seen_ts,
                            last_online_ts=excluded.last_online_ts
                        """,
                        (ident, "online", now_ts, ident, 1, now_ts, now_ts),
                    )
                else:
                    conn.execute(
                        "UPDATE device_states SET last_seen_ts = ? WHERE device_key = ?",
                        (now_ts, ident),
                    )

            conn.commit()
            break  # success

        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            # Treat disk I/O error + locked/busy as transient on Render Disk
            if any(s in msg for s in ("disk i/o error", "database is locked", "database is busy")) and attempt < 2:
                print(f"[warn] sqlite transient error ({e}); retrying...")
                time.sleep(0.4 * (attempt + 1))
                continue
            raise
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    print(f"[flespi] received={len(msgs)} processed={processed}")
    return "OK", 200
