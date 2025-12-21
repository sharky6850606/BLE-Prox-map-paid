import time
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
    """Ensure the core tables used by /data and renaming exist."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS beacon_names (id TEXT PRIMARY KEY, name TEXT)"
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


@map_bp.route("/data", methods=["GET"])
def map_data():
    """Return live device/beacon data + friendly names/colors for the frontend."""

    # Snapshot so we don't hold the global dict too long
    snapshot = dict(latest_messages)

    # Defaults if DB is temporarily unavailable
    beacon_names_map = {}
    device_meta = {}  # device_id -> {name,color}
    online_devices = None  # None means "don't filter"

    conn = None
    try:
        # DB access can be transiently flaky on Render Disk under concurrent writes.
        # If it fails, we still return live in-memory data so the UI keeps working.
        for _ in range(3):
            try:
                conn = get_db()
                _ensure_tables(conn)

                # Load beacon friendly names
                rows = conn.execute("SELECT id, name FROM beacon_names").fetchall()
                beacon_names_map = {bid: (bname or "") for bid, bname in rows if bid}

                # Load device name + color
                rows = conn.execute("SELECT id, name, color FROM devices").fetchall()
                for did, dname, dcolor in rows:
                    if not did:
                        continue
                    device_meta[did] = {"name": dname or "", "color": dcolor or ""}

                # Which devices are currently online (optional)
                try:
                    rows = conn.execute(
                        "SELECT device_key FROM device_states WHERE online = 1"
                    ).fetchall()
                    online_devices = {r[0] for r in rows if r and r[0]}
                except Exception:
                    online_devices = None

                break
            except Exception:
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
                continue
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    # Build response device list for the frontend
    devices_out = []
    for device_id, payload in snapshot.items():
        if device_id == "DAILY_REPORT":
            continue

        # Optional filter: if DB provided an online list, only show those
        if online_devices is not None and device_id not in online_devices:
            # Still show devices with very recent telemetry even if device_states is stale
            try:
                last_ts = float(payload.get("timestamp") or 0)
            except Exception:
                last_ts = 0
            if last_ts and (time.time() - last_ts) <= 90:
                pass
            else:
                continue

        meta = device_meta.get(device_id, {})
        payload = dict(payload) if isinstance(payload, dict) else {}
        payload["id"] = device_id
        payload["name"] = meta.get("name") or None
        payload["color"] = meta.get("color") or None
        devices_out.append(payload)

    return jsonify({
        "devices": devices_out,
        "beacon_names": beacon_names_map,
    })


@map_bp.route("/rename", methods=["POST"])
def rename_beacon():
    """Rename a beacon (stores in beacon_names table)."""
    data = request.get_json(silent=True) or {}
    beacon_id = (data.get("id") or "").strip()
    new_name = (data.get("name") or "").strip()

    if not beacon_id:
        return jsonify({"status": "error", "message": "Missing beacon id"}), 400

    conn = get_db()
    _ensure_tables(conn)

    conn.execute(
        "INSERT INTO beacon_names (id, name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (beacon_id, new_name),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})


@map_bp.route("/rename_device", methods=["POST"])
def rename_device():
    """Rename a device and optionally set its color."""
    data = request.get_json(silent=True) or {}
    device_id = (data.get("id") or "").strip()
    new_name = (data.get("name") or "").strip()
    color = (data.get("color") or "").strip() or None

    if not device_id:
        return jsonify({"status": "error", "message": "Missing device id"}), 400

    conn = get_db()
    _ensure_tables(conn)

    # Preserve existing color if not provided
    if color is None:
        row = conn.execute("SELECT color FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row:
            color = row[0]

    conn.execute(
        "INSERT INTO devices (id, name, color) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, color = excluded.color",
        (device_id, new_name, color),
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})
