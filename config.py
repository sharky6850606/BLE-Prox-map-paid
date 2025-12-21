import os
from pathlib import Path


def _ensure_writable_dir(path: Path) -> bool:
    """Create dir (if needed) and verify we can write into it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def pick_data_root() -> Path:
    """
    Choose a writable data directory.

    Priority:
      1) DATA_ROOT env var (explicit override)
      2) /var/data (Render Disk mount path)
      3) ./data (inside repo, works locally)
      4) /tmp/proxmap_data (always writable on Linux, not persistent)
    """
    candidates = []

    env_root = os.environ.get("DATA_ROOT")
    if env_root:
        candidates.append(Path(env_root))

    # Render Disk default mount
    candidates.append(Path("/var/data"))

    # Local dev
    candidates.append(Path(__file__).resolve().parent / "data")

    # Fallback (non-persistent)
    candidates.append(Path("/tmp/proxmap_data"))

    for p in candidates:
        if _ensure_writable_dir(p):
            return p

    # Last resort: current directory
    return Path(__file__).resolve().parent


DATA_ROOT = pick_data_root()

# SQLite DB path
DB_PATH = str(DATA_ROOT / "beacons.db")

# Report storage
REPORTS_DIR = str(DATA_ROOT / "reports")
ACTIVITY_REPORTS_DIR = str(DATA_ROOT / "activity_reports")
