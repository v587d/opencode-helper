"""
storage.py - Page storage for drilldown-generated HTML visualizations.

Stores generated HTML files under ~/.local/share/opencode/drilldown@och/
with a deterministic naming convention:

    {session_id[:10]}_{sanitized_title}_{timestamp}.html

The ``@och`` suffix prevents namespace collision with a potential future
official ``drilldown`` directory.

Zero external dependencies - stdlib only.
"""

import re
from datetime import datetime
from pathlib import Path

from utilities import OPENCODE_DATA

# ---------------------------------------------------------------------------
#  Storage directory
# ---------------------------------------------------------------------------

DRILLDOWN_DIR = OPENCODE_DATA / "drilldown@och"

# Characters not allowed in filenames on Windows or Unix
_FILENAME_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Collapse runs of whitespace and dashes
_FILENAME_COLLAPSE = re.compile(r'[\s_]+')
# Strip leading/trailing noise
_FILENAME_TRIM = re.compile(r'^[-_.]+|[-_.]+$')


def _sanitize_filename(name: str, max_len: int = 50) -> str:
    """Turn a session title into a filename-safe slug.

    - Replaces illegal chars with dashes
    - Collapses whitespace/underscore runs into single dashes
    - Truncates to ``max_len`` chars (trimming at word boundaries when possible)
    """
    slug = _FILENAME_ILLEGAL.sub('-', name)
    slug = _FILENAME_COLLAPSE.sub('-', slug)
    slug = _FILENAME_TRIM.sub('', slug)
    if not slug:
        slug = "untitled"
    if len(slug) > max_len:
        # Try to cut at last dash within limit to avoid mid-word truncation
        slug = slug[:max_len]
        cut = slug.rfind('-')
        if cut > max_len // 2:
            slug = slug[:cut]
    return slug


def get_output_path(session_id: str, title: str) -> Path:
    """Build the canonical output path for a drilldown HTML file.

    Format: ``DRILLDOWN_DIR/{session_id[:10]}_{slug}_{ts}.html``

    The directory is created if it does not exist.
    """
    DRILLDOWN_DIR.mkdir(parents=True, exist_ok=True)

    sid_prefix = session_id[:10] if len(session_id) >= 10 else session_id
    slug = _sanitize_filename(title)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")

    filename = f"{sid_prefix}_{slug}_{ts}.html"
    return DRILLDOWN_DIR / filename
