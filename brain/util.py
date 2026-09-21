"""Shared helpers: paths, JSON, dates."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MODELS_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "jarvis.db"
LOG_PATH = DATA_DIR / "agent_activity.log"


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    dt = dt or utcnow()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_dt(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def iso_from_ts(ts):
    """Unix epoch seconds -> ISO UTC string."""
    return iso(datetime.fromtimestamp(int(ts), tz=timezone.utc))


def parse_iso(s):
    """ISO string -> aware datetime (tolerates trailing Z / missing Z)."""
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = str(s).strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable datetime: {s!r}")


def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    ensure_dirs()
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def log_event(kind, detail=""):
    """Append a human-readable line to data/agent_activity.log."""
    ensure_dirs()
    line = f"{iso()}  [{kind}]  {detail}".rstrip() + "\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
