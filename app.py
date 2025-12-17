from flask import (
    Flask, request, jsonify, render_template,
    redirect, url_for, send_file
)
import os
import time
import json
import threading

from database import init_db, get_db
from routes import map_bp, flespi_bp
from services.beacon_logic import format_samoa_time, latest_messages
from services.reporting_service import (
    generate_activity_report,
    generate_device_activity_report,
)
from services.cron_evaluator import run_evaluator
from config import DB_PATH, REPORTS_DIR, ACTIVITY_REPORTS_DIR


# ======================================================
# Flask App Setup
# ======================================================

app = Flask(__name__)
app.register_blueprint(map_bp)
app.register_blueprint(flespi_bp)


# ======================================================
# Startup Initialization (Gunicorn-safe)
# ======================================================

print(f"[startup] SQLite DB: {DB_PATH}")
print(f"[startup] Reports dir: {REPORTS_DIR}")
print(f"[startup] Activity reports dir: {ACTIVITY_REPORTS_DIR}")

init_db()


# ======================================================
# Background Evaluator (SINGLE SERVICE MODE)
# ======================================================

def evaluator_loop():
    print("🔄 Background evaluator started")
    while True:
        try:
            run_evaluator()
        except Exception as e:
            print("❌ Evaluator error:", e)
        time.sleep(60)


if os.getenv("BACKGROUND_EVALUATOR", "0") == "1":
    # Prevent double-start under gunicorn reloads
    if os.environ.get("EVALUATOR_STARTED") != "1":
        os.environ["EVALUATOR_STARTED"] = "1"
        threading.Thread(
            target=evaluator_loop,
            daemon=True
        ).start()


# ======================================================
# Helpers
# ======================================================

def samoa_iso_now() -> str:
    return format_samoa_time(time.time()).replace(" ", "T")


def build_beacon_alias_map(conn):
    rows = conn.execute(
        "SELECT id, name FROM beacon_names"
    ).fetchall()

    alias = {}
    for bid, bname in rows:
        if not bid:
            continue
        if bname and bname != bid:
            label = f"{bname} ({bid})"
        else:
            label = bid
        alias[bid] = label
        if bname:
            alias[bname] = label
    return alias


# ======================================================
# Notifications API (TRANSITIONS ONLY)
# ======================================================

@app.route("/api/notifications", methods=["POST"])
def save_notification():
    data = request.get_json(silent=True) or {}
    ntype = (data.get("type") or "").strip()
    name = (data.get("name") or "").strip()
    event_time = (data.get("time") or "").strip() or None

    if not ntype or not name:
        return jsonify({"error": "invalid"}), 400

    # Ignore still_* (handled by evaluator)
    if ntype.startswith("still_"):
        return jsonify({"status": "ignored"}), 202

    conn = get_db()
    now_ts = int(time.time())
    created_at = samoa_iso_now()

    conn.execute(
        """
        INSERT INTO notifications
        (type, beacon_name, event_time, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (ntype, name, event_time, created_at),
    )

    if ntype in ("in", "left"):
        desired = "in" if ntype == "in" else "out"
        row = conn.execute(
            "SELECT state FROM beacon_states WHERE beacon_key = ?",
            (name,),
        ).fetchone()

        if not row or row[0] != desired:
            conn.execute(
                """
                INSERT INTO beacon_states
                (beacon_key, state, last_change_ts, active)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(beacon_key) DO UPDATE SET
                  state=excluded.state,
                  last_change_ts=excluded.last_change_ts,
                  active=1
                """,
                (name, desired, now_ts),
            )

    conn.commit()
    conn.close()
    return jsonify({"status": "ok"}), 201


# ======================================================
# Reports
# ======================================================

@app.route("/reports/history")
def reports_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, created_at, summary FROM daily_reports ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return render_template("reports_history.html", reports=rows)


@app.route("/download/latest-report")
def download_latest_report():
    conn = get_db()
    row = conn.execute(
        "SELECT pdf_path FROM daily_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if not row or not row[0] or not os.path.exists(row[0]):
        return "No report", 404

    return send_file(row[0], as_attachment=True)


# ======================================================
# Activity Reports
# ======================================================

@app.route("/activity-reports", methods=["GET", "POST"])
def activity_reports():
    conn = get_db()

    if request.method == "POST":
        kind = request.form.get("report_kind", "beacon")
        start = request.form.get("start_date") or None
        end = request.form.get("end_date") or None

        if kind == "device":
            ident = request.form.get("device_ident")
            if ident:
                generate_device_activity_report(ident, start, end)
        else:
            beacon = request.form.get("beacon_id")
            if beacon:
                generate_activity_report(beacon, start, end)

        return redirect(url_for("activity_reports"))

    beacons = conn.execute(
        "SELECT DISTINCT beacon_name FROM notifications"
    ).fetchall()

    devices = conn.execute(
        "SELECT id, name FROM devices"
    ).fetchall()

    reports = conn.execute(
        "SELECT id, beacon_name, created_at, summary FROM activity_reports ORDER BY id DESC"
    ).fetchall()

    conn.close()

    return render_template(
        "activity_reports.html",
        beacons=[b[0] for b in beacons if b[0]],
        devices=devices,
        reports=reports,
    )


# ======================================================
# Uptime
# ======================================================

@app.route("/uptime")
def uptime_page():
    conn = get_db()
    rows = conn.execute(
        "SELECT timestamp, device_count, beacon_count, status FROM uptime_logs ORDER BY id DESC LIMIT 500"
    ).fetchall()
    conn.close()
    return render_template("uptime.html", logs=rows)


# ======================================================
# Analytics
# ======================================================

@app.route("/analytics")
def analytics():
    conn = get_db()
    rows = conn.execute(
        "SELECT type FROM notifications WHERE type IN ('in','left')"
    ).fetchall()
    conn.close()

    return render_template(
        "analytics.html",
        total_events=len(rows),
    )


# ======================================================
# Main
# ======================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
