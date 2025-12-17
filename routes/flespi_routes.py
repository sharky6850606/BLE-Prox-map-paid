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


@flespi_bp.route("/flespi", methods=["POST"])
def flespi_receiver():
    for _attempt in range(2):
        try:
            data = request.get_json(silent=True)
            if not data or "data" not in data:
                return "No data", 400

            msgs = data.get("data", [])
            conn = get_db()
            _ensure_device_states(conn)

            count = 0
            now_ts = int(time.time())

            for raw in msgs:
                if not isinstance(raw, dict):
                    continue

                simplified = simplify_message(raw)
                ident = simplified.get("ident")
                if not ident:
                    continue

                latest_messages[ident] = simplified
                count += 1

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
            conn.close()

            log_uptime_snapshot()
            print(f"Received {len(msgs)} msgs, processed {count}, tracking {len(latest_messages)} devices.")
            return "OK", 200

        except sqlite3.OperationalError as e:
            if "disk I/O error" in str(e) and _attempt == 0:
                print("[warn] sqlite disk I/O error; retrying once...")
                time.sleep(0.2)
                continue
            raise

    
