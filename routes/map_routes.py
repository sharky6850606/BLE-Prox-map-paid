from flask import Blueprint, request, jsonify, render_template, redirect, url_for

from database import get_db
from services.beacon_logic import latest_messages

map_bp = Blueprint("map", __name__)


@map_bp.route("/", methods=["GET"])
def root():
    """Redirect base URL to the main map page.""" 
    return redirect(url_for("map.map_page"))


@map_bp.route("/map", methods=["GET"])
def map_page():
    """Main map page.""" 
    return render_template("index.html")


def _ensure_tables(conn):
    # Beacon and device tables (names + colors)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS beacon_names (id TEXT PRIMARY KEY, name TEXT)"
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT,
            color TEXT
        )
        """
    )


@map_bp.route("/data", methods=["GET"])
def map_data():
    """Return current devices + beacon names for the frontend.""" 

    # Snapshot so we don't hold the global dict too long
    snapshot = dict(latest_messages)

    conn = None
    try:
        # DB access can be transiently flaky on Render Disk under concurrent writes.
        # If it fails, we still return live in-memory data so the UI keeps working.
        for attempt in range(3):
            try:
                conn = get_db()
                _ensure_tables(conn)

                # Load beacon/device names (if tables exist)
                beacon_name_rows = conn.execute("SELECT beacon_id, name FROM beacons").fetchall()
                device_name_rows = conn.execute("SELECT device_ident, name FROM devices").fetchall()

                beacon_names = {r[0]: r[1] for r in beacon_name_rows}
                device_names = {r[0]: r[1] for r in device_name_rows}

                # Only show ONLINE devices on the UI
                online_rows = conn.execute("SELECT device_key FROM device_states WHERE online = 1").fetchall()
                online_devices = {r[0] for r in online_rows}

                break
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if any(s in msg for s in ("disk i/o error", "database is locked", "database is busy")) and attempt < 2:
                    print(f"[warn] /data sqlite transient error ({e}); retrying...")
                    time.sleep(0.25 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        print(f"[warn] /data DB unavailable: {e}")
        beacon_names = {}
        device_names = {}
        online_devices = set()
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


    devices_payload = []

    for ident, msg in snapshot.items():
        if ident == "DAILY_REPORT":
            devices_payload.append(msg)
            continue

        meta = device_meta.get(ident, {})
        devices_payload.append(
            {
                "ident": ident,
                "name": meta.get("name"),
                "color": meta.get("color"),
                "timestamp_raw": msg.get("timestamp_raw"),
                "timestamp": msg.get("timestamp"),
                "lat": msg.get("lat"),
                "lon": msg.get("lon"),
                "beacons": msg.get("beacons") or [],
            }
        )

    return jsonify(
        {
            "devices": devices_payload,
            "beacon_names": beacon_names,
        }
    )


@map_bp.route("/rename", methods=["POST"])
def rename_beacon():
    """Rename a beacon (stored in beacon_names table).""" 
    data = request.get_json(silent=True) or {}
    beacon_id = data.get("beacon_id")
    new_name = data.get("new_name")

    if not beacon_id or new_name is None:
        return jsonify({"status": "error", "message": "Invalid input"}), 400

    conn = get_db()
    _ensure_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO beacon_names (id, name) VALUES (?, ?)",
        (beacon_id, new_name),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})


@map_bp.route("/rename_device", methods=["POST"])
def rename_device():
    """Rename a device and preserve its color.""" 
    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id")
    new_name = data.get("new_name")

    if not device_id or new_name is None:
        return jsonify({"status": "error", "message": "Invalid input"}), 400

    conn = get_db()
    _ensure_tables(conn)

    row = conn.execute(
        "SELECT color FROM devices WHERE id = ?",
        (device_id,),
    ).fetchone()
    existing_color = row[0] if row and row[0] else None
    color = existing_color or "#3b82f6"

    conn.execute(
        "INSERT OR REPLACE INTO devices (id, name, color) VALUES (?, ?, ?)",
        (device_id, new_name, color),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})
