import os

# ---- Persistent data directory (Render Disk friendly) ----
# If you attach a Render Disk at /var/data, everything under that path survives deploys/restarts.
_DEFAULT_DATA_DIR = "/var/data" if os.path.isdir("/var/data") else os.path.dirname(__file__)

# Database path (SQLite)
# You can override with SQLITE_DB_PATH in Render Environment Variables.
DB_PATH = os.getenv("SQLITE_DB_PATH") or os.path.join(_DEFAULT_DATA_DIR, "beacons.db")

# Report output directories (PDFs)
REPORTS_DIR = os.getenv("REPORTS_DIR") or os.path.join(_DEFAULT_DATA_DIR, "reports")
ACTIVITY_REPORTS_DIR = os.getenv("ACTIVITY_REPORTS_DIR") or os.path.join(_DEFAULT_DATA_DIR, "activity_reports")

# Beacon TTL (seconds)
TTL_SECONDS = int(os.getenv("TTL_SECONDS", "900"))  # tuned for ~5-min sends

# Samoa timezone offset
SAMOA_OFFSET_HOURS = int(os.getenv("SAMOA_OFFSET_HOURS", "13"))  # UTC+13 for Samoa

# RSSI -> distance model
TX_POWER = int(os.getenv("TX_POWER", "-59"))
PATH_LOSS_N = float(os.getenv("PATH_LOSS_N", "2.0"))
