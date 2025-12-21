from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    send_file,
    abort,
)
import os
import time
import json
import threading

from database import init_db, get_db
from routes import map_bp, flespi_bp

from services.beacon_logic import format_samoa_time
from services.reporting_service import (
    start_daily_beacon_check_thread,
    generate_activity_report,
    generate_device_activity_report,
)
# NOTE: cron_evaluator.py lives at the repo root (not inside services/)
from cron_evaluator import run_evaluator

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

# Ensure directories exist
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(ACTIVITY_REPORTS_DIR, exist_ok=True)

# Ensure DB schema exists
init_db()


# ======================================================
# Background Evaluator (OPTION A: single web service)
#
# IMPORTANT:
# - This is optional and controlled by BACKGROUND_EVALUATOR=1
# - Uses a daemon thread so it won't block requests.
# ======================================================

def evaluator_loop():
    print("🔄 Background evaluator started")
    while True:
        try:
            run_evaluator()
        except Exception as e:
            print("❌ Evaluator error:", e)
        time.sleep(60)


def maybe_start_background_threads():
    # Prevent double-start (gunicorn can fork workers)
    if os.environ.get("THREADS_STARTED") == "1":
        return
    os.environ["THREADS_STARTED"] = "1"

    # Daily report thread (safe to run in the background)
    try:
        start_daily_beacon_check_thread()
    except Exception as e:
        print("[warn] daily report thread not started:", e)

    # Optional 60s evaluator thread
    if os.getenv("BACKGROUND_EVALUATOR", "0") == "1":
        threading.Thread(target=evaluator_loop, daemon=True).start()


maybe_start_background_threads()


# ======================================================
# Helpers
# ======================================================

def samoa_iso_now() -> str:
    # Returns a Samoa-local timestamp string (YYYY-MM-DDTHH:MM:SS)
    return format_samoa_time(time.time()).replace(" ", "T")


def build_beacon_alias_map(conn):
    rows = conn.execute("SELECT id, name FROM beacon_names").fetchall()
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
# Analytics payload builder (for templates/analytics.html)
# ======================================================

def build_analytics_payload(conn, beacon_alias_map, window_hours: int = 24, selected_beacon: str | None = None):
    """Builds all variables expected by templates/analytics.html.

    This keeps the analytics page from breaking if some tables are empty.
    We intentionally keep this lightweight to avoid gunicorn timeouts.
    """

    now_ts = int(time.time())
    window_start_ts = now_ts - (window_hours * 3600)

    # --- Uptime series (last 500)
    try:
        uptime_rows = conn.execute(
            "SELECT timestamp, device_count, beacon_count, status FROM uptime_logs ORDER BY id DESC LIMIT 500"
        ).fetchall()
        uptime_rows = list(reversed(uptime_rows))
    except sqlite3.OperationalError as e:
        # Render Disk can intermittently throw "disk I/O error" (or the DB can be mid-write).
        # Never crash the whole app/page for this.
        print(f"[analytics] DB error reading uptime_logs: {e}")
        uptime_rows = []

    uptime_labels = [r[0] for r in uptime_rows]
    uptime_device_counts = [int(r[1] or 0) for r in uptime_rows]
    uptime_beacon_counts = [int(r[2] or 0) for r in uptime_rows]

    # status counts (OK/WARN/ERROR)
    status_counter = {"OK": 0, "WARN": 0, "ERROR": 0}
    for r in uptime_rows:
        s = (r[3] or "OK").upper()
        if s not in status_counter:
            status_counter[s] = 0
        status_counter[s] += 1
    uptime_status_counts = status_counter

    latest_uptime = uptime_rows[-1] if uptime_rows else None
    latest_timestamp = latest_uptime[0] if latest_uptime else ""
    latest_devices = int(latest_uptime[1] or 0) if latest_uptime else 0
    latest_beacons = int(latest_uptime[2] or 0) if latest_uptime else 0
    latest_status = (latest_uptime[3] or "OK") if latest_uptime else "OK"

    # --- Notifications in window
    try:
        notif_rows = conn.execute(
            """
            SELECT beacon_name, type, created_at
            FROM notifications
            WHERE created_at IS NOT NULL
              AND type IN ('in','left','still_in','still_out')
              AND strftime('%s', replace(created_at,'T',' ')) >= ?
            """,
            (str(window_start_ts),),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[analytics] DB error reading notifications: {e}")
        notif_rows = []

    # beacon list for dropdown
    beacon_names = sorted({(r[0] or "").strip() for r in notif_rows if (r[0] or "").strip()})
    if not selected_beacon and beacon_names:
        selected_beacon = beacon_names[0]

    # --- Event counts by type (bar)
    notif_type_counts = {"in": 0, "left": 0, "still_in": 0, "still_out": 0}
    for b, t, _ in notif_rows:
        t = (t or "").strip()
        if t in notif_type_counts:
            notif_type_counts[t] += 1
    notif_labels = list(notif_type_counts.keys())
    notif_values = [notif_type_counts[k] for k in notif_labels]

    # --- By-day counts (last 14 days)
    by_day = conn.execute(
        """
        SELECT substr(created_at,1,10) as day, COUNT(*)
        FROM notifications
        WHERE type IN ('in','left','still_in','still_out')
          AND created_at IS NOT NULL
          AND strftime('%s', replace(created_at,'T',' ')) >= ?
        GROUP BY day
        ORDER BY day DESC
        LIMIT 14
        """,
        (str(window_start_ts),),
    ).fetchall()
    by_day = list(reversed(by_day))
    by_day_labels = [r[0] for r in by_day]
    by_day_values = [int(r[1] or 0) for r in by_day]

    # --- Hourly activity (selected beacon, last 24h)
    hourly = []
    if selected_beacon:
        hourly = conn.execute(
            """
            SELECT substr(created_at,1,13) as hour, COUNT(*)
            FROM notifications
            WHERE beacon_name = ?
              AND type IN ('in','left','still_in','still_out')
              AND created_at IS NOT NULL
              AND strftime('%s', replace(created_at,'T',' ')) >= ?
            GROUP BY hour
            ORDER BY hour
            """,
            (selected_beacon, str(window_start_ts)),
        ).fetchall()
    hourly_labels = [r[0] + ":00" for r in hourly]
    hourly_counts = [int(r[1] or 0) for r in hourly]

    # --- Top beacons (bar)
    top = conn.execute(
        """
        SELECT beacon_name,
               SUM(CASE WHEN type='in' THEN 1 ELSE 0 END) as ins,
               SUM(CASE WHEN type='left' THEN 1 ELSE 0 END) as lefts,
               COUNT(*) as total
        FROM notifications
        WHERE type IN ('in','left')
          AND created_at IS NOT NULL
          AND strftime('%s', replace(created_at,'T',' ')) >= ?
        GROUP BY beacon_name
        ORDER BY total DESC
        LIMIT 15
        """,
        (str(window_start_ts),),
    ).fetchall()

    beacon_labels = [beacon_alias_map.get(r[0], r[0]) for r in top]
    beacon_totals = [int(r[3] or 0) for r in top]
    beacon_ins = [int(r[1] or 0) for r in top]
    beacon_lefts = [int(r[2] or 0) for r in top]

    total_events = sum(beacon_totals)

    # --- Status pie (approx from beacon_states)
    states = conn.execute("SELECT state, COUNT(*) FROM beacon_states WHERE active=1 GROUP BY state").fetchall()
    in_count = 0
    out_count = 0
    for st, c in states:
        if (st or "").lower() == "in":
            in_count += int(c or 0)
        else:
            out_count += int(c or 0)
    status_pie_labels = ["in", "out"]
    status_pie_values = [in_count, out_count]

    # Presence per beacon (simple % based on in vs left event counts)
    presence_by_beacon = []
    for name, ins, lefts, total in top:
        total = int(total or 0)
        ins = int(ins or 0)
        lefts = int(lefts or 0)
        pct_in = (ins / total) * 100 if total else 0
        pct_out = 100 - pct_in if total else 0
        presence_by_beacon.append({
            "beacon": beacon_alias_map.get(name, name),
            "raw_beacon": name,
            "in_percent": round(pct_in, 1),
            "out_percent": round(pct_out, 1),
            "events": total,
        })

    most_active_beacon = presence_by_beacon[0]["beacon"] if presence_by_beacon else ""

    # Uptime OK percent
    total_uptime = len(uptime_rows)
    ok_uptime = status_counter.get("OK", 0)
    uptime_ok_percent = round((ok_uptime / total_uptime) * 100, 1) if total_uptime else 0

    return {
        "window_hours": window_hours,
        "selected_beacon": selected_beacon or "",
        "beacon_names": beacon_names,

        "latest_timestamp": latest_timestamp,
        "latest_devices": latest_devices,
        "latest_beacons": latest_beacons,
        "latest_status": latest_status,
        "uptime_ok_percent": uptime_ok_percent,
        "most_active_beacon": most_active_beacon,

        "total_events": total_events,

        "uptime_labels": uptime_labels,
        "uptime_device_counts": uptime_device_counts,
        "uptime_beacon_counts": uptime_beacon_counts,
        "uptime_status_counts": uptime_status_counts,

        "status_pie_labels": status_pie_labels,
        "status_pie_values": status_pie_values,

        "notif_labels": notif_labels,
        "notif_values": notif_values,

        "by_day_labels": by_day_labels,
        "by_day_values": by_day_values,

        "hourly_labels": hourly_labels,
        "hourly_counts": hourly_counts,

        "beacon_labels": beacon_labels,
        "beacon_totals": beacon_totals,
        "beacon_ins": beacon_ins,
        "beacon_lefts": beacon_lefts,

        "presence_by_beacon_json": json.dumps(presence_by_beacon),
    }


# ======================================================
# ======================================================
# Analytics Helpers (no extra module needed)
# ======================================================

def _table_exists(conn, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def build_analytics_payload(window_hours: int = 24, selected_beacon: str | None = None) -> dict:
    """Builds the context dict required by templates/analytics.html.
    Always returns JSON-serializable values and never raises (best-effort).
    """
    payload = {
        "window_hours": int(window_hours or 24),
        "total_events": 0,
        "uptime_labels": [],
        "device_counts": [],
        "beacon_counts": [],
        "hourly_labels": [],
        "hourly_counts": [],
        "beacon_labels": [],
        "beacon_totals": [],
        "beacon_ins": [],
        "beacon_lefts": [],
        "selected_beacon": selected_beacon or "",
        "latest_timestamp": "",
        "latest_status": "",
        "latest_device_count": 0,
        "latest_beacon_count": 0,
        "uptime_ok_percent": 0,
        "presence_by_beacon_json": "{}",
        "most_active_beacon": "",
        "latest_devices": [],
        "latest_beacons": [],
    }

    try:
        conn = get_db()

        # --------------------------
        # UPTIME CHART (last 500)
        # --------------------------
        if _table_exists(conn, "uptime_logs"):
            rows = conn.execute(
                "SELECT timestamp, device_count, beacon_count, status "
                "FROM uptime_logs ORDER BY id DESC LIMIT 500"
            ).fetchall()
            rows = list(reversed(rows))
            labels = []
            dcounts = []
            bcounts = []
            ok = 0
            for ts, dc, bc, st in rows:
                labels.append(str(ts))
                dcounts.append(int(dc or 0))
                bcounts.append(int(bc or 0))
                if str(st or "").upper() == "OK":
                    ok += 1

            payload["uptime_labels"] = labels
            payload["device_counts"] = dcounts
            payload["beacon_counts"] = bcounts
            payload["uptime_ok_percent"] = int((ok / len(rows)) * 100) if rows else 0

            if rows:
                last_ts, last_dc, last_bc, last_st = rows[-1]
                payload["latest_timestamp"] = str(last_ts or "")
                payload["latest_status"] = str(last_st or "")
                payload["latest_device_count"] = int(last_dc or 0)
                payload["latest_beacon_count"] = int(last_bc or 0)

        # --------------------------
        # EVENTS (in/left) totals + by beacon
        # --------------------------
        if _table_exists(conn, "notifications"):
            # total events in DB (kept simple)
            ev = conn.execute(
                "SELECT type, beacon_name FROM notifications WHERE type IN ('in','left')"
            ).fetchall()
            payload["total_events"] = len(ev)

            by_beacon = {}
            for t, b in ev:
                b = (b or "").strip() or "Unknown"
                by_beacon.setdefault(b, {"in": 0, "left": 0})
                if t == "in":
                    by_beacon[b]["in"] += 1
                elif t == "left":
                    by_beacon[b]["left"] += 1

            # top 25 beacons
            items = sorted(
                by_beacon.items(),
                key=lambda kv: (kv[1]["in"] + kv[1]["left"]),
                reverse=True,
            )[:25]

            payload["beacon_labels"] = [k for k, _ in items]
            payload["beacon_ins"] = [v["in"] for _, v in items]
            payload["beacon_lefts"] = [v["left"] for _, v in items]
            payload["beacon_totals"] = [v["in"] + v["left"] for _, v in items]
            payload["most_active_beacon"] = payload["beacon_labels"][0] if items else ""

            payload["presence_by_beacon_json"] = json.dumps(by_beacon)

            # hourly counts (use created_at ISO string prefix YYYY-MM-DDTHH)
            hourly = conn.execute(
                "SELECT substr(created_at, 1, 13) AS hr, COUNT(*) "
                "FROM notifications "
                "WHERE type IN ('in','left') AND created_at IS NOT NULL "
                "GROUP BY hr ORDER BY hr DESC LIMIT 48"
            ).fetchall()
            hourly = list(reversed(hourly))
            payload["hourly_labels"] = [str(h or "") for h, _ in hourly]
            payload["hourly_counts"] = [int(c or 0) for _, c in hourly]

        # --------------------------
        # Optional “latest lists” for template (safe defaults)
        # --------------------------
        if _table_exists(conn, "devices"):
            devs = conn.execute("SELECT name FROM devices ORDER BY id DESC LIMIT 20").fetchall()
            payload["latest_devices"] = [d[0] for d in devs if d and d[0]]

        if _table_exists(conn, "beacon_names"):
            bns = conn.execute("SELECT name FROM beacon_names ORDER BY id DESC LIMIT 20").fetchall()
            payload["latest_beacons"] = [b[0] for b in bns if b and b[0]]

        conn.close()

    except Exception as e:
        print("[analytics] payload build error:", e)

    return payload

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

    # Ignore still_* (handled by cron_evaluator)
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

    # Maintain state for analytics / reports
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
        "SELECT id, created_at, summary, pdf_path FROM daily_reports ORDER BY id DESC LIMIT 200"
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


@app.route("/download/report/<int:report_id>")
def download_daily_report(report_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT pdf_path FROM daily_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    conn.close()

    if not row or not row[0] or not os.path.exists(row[0]):
        abort(404)
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

        conn.close()
        return redirect(url_for("activity_reports"))

    beacons = conn.execute(
        "SELECT DISTINCT beacon_name FROM notifications WHERE beacon_name IS NOT NULL AND beacon_name != '' ORDER BY beacon_name"
    ).fetchall()

    devices = conn.execute(
        "SELECT ident, name FROM devices ORDER BY COALESCE(name, ident)"
    ).fetchall()

    reports = conn.execute(
        "SELECT id, beacon_name, created_at, summary, pdf_path FROM activity_reports ORDER BY id DESC LIMIT 300"
    ).fetchall()
    conn.close()

    return render_template(
        "activity_reports.html",
        beacons=[b[0] for b in beacons],
        devices=devices,
        reports=reports,
    )


@app.route("/activity-reports/download/<int:report_id>")
def download_activity_report(report_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT pdf_path FROM activity_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    conn.close()

    if not row or not row[0] or not os.path.exists(row[0]):
        return "Report not found", 404

    return send_file(row[0], as_attachment=True)


# ======================================================
# Notifications History
# ======================================================

@app.route("/notifications/history")
def notifications_history():
    q = (request.args.get("q") or "").strip()
    page = int(request.args.get("page") or 1)
    page = max(page, 1)
    per_page = 250
    offset = (page - 1) * per_page

    conn = get_db()
    alias = build_beacon_alias_map(conn)

    if q:
        like = f"%{q}%"
        rows = conn.execute(
            """
            SELECT id, type, beacon_name, event_time, created_at
            FROM notifications
            WHERE beacon_name LIKE ? OR type LIKE ? OR created_at LIKE ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (like, like, like, per_page, offset),
        ).fetchall()
        total_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM notifications
            WHERE beacon_name LIKE ? OR type LIKE ? OR created_at LIKE ?
            """,
            (like, like, like),
        ).fetchone()
    else:
        rows = conn.execute(
            """
            SELECT id, type, beacon_name, event_time, created_at
            FROM notifications
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()
        total_row = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()

    conn.close()

    # Friendly names for display
    display_rows = []
    for r in rows:
        rid, rtype, bname, etime, ctime = r
        label = alias.get(bname, bname)
        display_rows.append((rid, rtype, label, etime, ctime))

    total = int(total_row[0] if total_row else 0)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "notifications_history.html",
        rows=display_rows,
        q=q,
        page=page,
        total_pages=total_pages,
        total=total,
    )


# ======================================================
# Uptime
# ======================================================

@app.route("/uptime")
def uptime_page():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, device_count, beacon_count, status FROM uptime_logs ORDER BY id DESC LIMIT 500"
        ).fetchall()
    except sqlite3.OperationalError as e:
        # If the mounted disk is having an I/O hiccup, avoid crashing the whole page.
        print(f"[uptime] sqlite error: {e}")
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return render_template("uptime.html", logs=rows)


# ======================================================
# Analytics Dashboard
# ======================================================

@app.route("/analytics")
def analytics_dashboard():
    # Build all values the template expects (avoids Jinja Undefined -> 500)
    payload = build_analytics_payload()
    return render_template("analytics.html", **payload)


# ======================================================
# Internal: force evaluator run (useful for debugging)
# ======================================================

@app.route("/internal/evaluate", methods=["POST", "GET"])
def internal_evaluate():
    # Optional key protection
    expected = os.getenv("INTERNAL_KEY")
    if expected:
        provided = request.args.get("k") or request.headers.get("X-Internal-Key")
        if provided != expected:
            abort(403)

    run_evaluator()
    return jsonify({"status": "ok"})


# ======================================================
# Main
# ======================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
