"""
Shared utilities for OpenCode cleanup tools.

Provides: configuration loading, logging, process detection,
database connection, file size formatting.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Project Root ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_ROOT / "settings.jsonc"


# ─── Settings ────────────────────────────────────────────────────────────────

_DEFAULT_SETTINGS = {
    "session_retention_days": 7,
    "session_auto_backup": True,
    "session_auto_vacuum": True,
    "session_save_list": {},
    "temp_script_retention_days": 1,
    "temp_project_retention_days": 1,
    "db_path_override": None,
    "analysis_language": "en",
    "analysis_model": None,
    "analysis_variant": None,
}


def _load_settings() -> dict:
    """Load settings from settings.jsonc, falling back to built‑in defaults."""
    settings = dict(_DEFAULT_SETTINGS)

    if not SETTINGS_PATH.exists():
        return settings

    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        # Strip // line comments
        text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
        # Strip /* block */ comments (unlikely but safe)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        loaded = json.loads(text)
        # Only accept keys we recognise
        for key in settings:
            if key in loaded:
                settings[key] = loaded[key]
    except (json.JSONDecodeError, OSError) as e:
        log = logging.getLogger("common")
        log.warning("Failed to parse %s: %s  — using defaults.", SETTINGS_PATH, e)

    return settings


_settings = _load_settings()


def get_settings() -> dict:
    """Return a copy of the current settings."""
    return dict(_settings)


# ─── Paths ───────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")))
OPENCODE_DATA = DATA_DIR / "opencode"

# Database path: settings override > XDG default
_db_override = _settings.get("db_path_override")
DB_PATH = Path(_db_override) if _db_override else (OPENCODE_DATA / "opencode.db")

STORAGE_DIR = OPENCODE_DATA / "storage"

TEMP_DIR = Path(os.environ.get("TEMP", os.path.expanduser("~/AppData/Local/Temp")))
OPENCODE_TEMP = TEMP_DIR / "opencode"


# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"


# ─── Logging ─────────────────────────────────────────────────────────────────

def setup_logging(name: str = "opencode-cleanup", level: int = logging.INFO) -> logging.Logger:
    """Configure and return a logger with console output."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        logger.addHandler(handler)

    return logger


# ─── Formatting ──────────────────────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def format_time(ts_ms: Optional[int]) -> str:
    """Convert Unix-ms timestamp to local datetime string."""
    if ts_ms is None:
        return "N/A"
    try:
        return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "invalid timestamp"


def print_header(title: str, width: int = 78):
    """Print a consistently formatted section header."""
    print()
    print("─" * width)
    print(f"  {title}")
    print("─" * width)


def print_separator(width: int = 78):
    print("─" * width)


# ─── Process Detection ───────────────────────────────────────────────────────

def is_opencode_running() -> bool:
    """Check if opencode.exe is currently running."""
    try:
        result = subprocess.run(
            ["tasklist", "/fi", "imagename eq opencode.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
        )
        return "opencode.exe" in result.stdout.lower()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 "Get-Process -Name opencode -ErrorAction SilentlyContinue | Select-Object -First 1"],
                capture_output=True, text=True, timeout=10,
            )
            return "opencode" in result.stdout.lower()
        except Exception:
            return False


def ensure_opencode_stopped(log: logging.Logger):
    """Exit if opencode.exe is running."""
    if is_opencode_running():
        log.error("opencode.exe is still running!")
        log.error("  Close OpenCode completely before running this script.")
        log.error("  Verify: tasklist | findstr opencode")
        log.error("  Force kill: taskkill /f /im opencode.exe")
        sys.exit(1)
    log.info("opencode.exe is not running.")


# ─── Database ────────────────────────────────────────────────────────────────

def get_db_connection(timeout_ms: int = 30000) -> sqlite3.Connection:
    """Open a safe SQLite connection with WAL recovery enabled."""
    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=timeout_ms / 1000.0,
        isolation_level=None,
    )
    conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
