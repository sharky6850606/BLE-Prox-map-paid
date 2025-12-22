import sqlite3

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
# Main
# ======================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
