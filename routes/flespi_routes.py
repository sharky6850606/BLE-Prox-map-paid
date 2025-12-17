from flask import Blueprint, request
import sqlite3
import time

from database import get_db
from services.beacon_logic import simplify_message, latest_messages
from services.uptime_service import log_uptime_snapshot

flespi_bp = Blueprint("flespi", __name__)


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

    # Uptime log should never kill the webhook
    try:
        log_uptime_snapshot()
    except Exception as e:
        print(f"[warn] uptime snapshot failed: {e}")

    print(f"[flespi] received={len(msgs)} processed={processed}")
    return "OK", 200
