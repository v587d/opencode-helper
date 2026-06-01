#!/usr/bin/env python3
"""
Temporary File Cleanup
======================
Cleans up files that OpenCode accumulates in the system temp directory
(``%TEMP%\\opencode\\``) but never purges.  This is where AI‑generated
scripts, cloned repos, and data files pile up indefinitely.

OpenCode creates this directory on startup but has **no built‑in cleanup**
for it — unlike ``tool-output/`` or ``snapshot/``, which get hourly pruning.

Scope: **only** ``%TEMP%\\opencode\\`` and its contents.
Does **not** touch ``~/.local/share/opencode/`` or any other location.

Usage
-----
    python -m cleanup.tempfile                # dry-run: preview only
    python -m cleanup.tempfile --execute      # actually delete
    python -m cleanup.tempfile --execute --days 3   # aggressive retention
"""

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from utilities import (
    OPENCODE_TEMP,
    ensure_opencode_stopped, format_size, get_settings, print_header, setup_logging,
)

log = setup_logging("tempfile")

# ─── Classification Rules ────────────────────────────────────────────────────

# File extensions the AI commonly creates as one‑off scripts
SCRIPT_EXTENSIONS = {".py", ".ps1", ".sh", ".js", ".mjs", ".ts", ".rb", ".go"}

# File extensions for small data outputs
DATA_EXTENSIONS = {".json", ".txt", ".csv", ".xml", ".yaml", ".yml", ".md", ".html", ".svg"}

# Files/directories that suggest a directory is a cloned/scaffolded project
_PROJECT_SIGNATURES = {
    ".git", "node_modules", "package.json", "Cargo.toml", "go.mod",
    "setup.py", "pyproject.toml", "Gemfile", "Makefile", "CMakeLists.txt",
}


# ─── Unified CLI Registration ────────────────────────────────────────────────

def register_subparser(subparsers):
    """Register tempfile subcommand for unified CLI (main.py)."""
    p = subparsers.add_parser(
        "tempfile",
        help="Clean up AI-generated temp files (%%TEMP%%\\opencode\\)",
    )
    p.add_argument("--execute", action="store_true",
                   help="Actually delete files (default: dry-run only)")
    p.add_argument("--scripts", type=int, default=None,
                   help="Retention days for loose files (see settings.jsonc)")
    p.add_argument("--projects", type=int, default=None,
                   help="Retention days for project directories (see settings.jsonc)")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-file deletion messages")
    p.set_defaults(func=run)


# ─── Data Structures ─────────────────────────────────────────────────────────

class CleanTarget(NamedTuple):
    path: Path
    size_bytes: int
    age_days: int
    is_project: bool          # True = directory that looks like a cloned project
    

# ─── Discovery ───────────────────────────────────────────────────────────────

def _looks_like_project(path: Path) -> bool:
    """Heuristic: does this directory resemble a cloned/scaffolded project?"""
    if not path.is_dir():
        return False
    try:
        names = {e.name for e in path.iterdir()}
    except PermissionError:
        return False
    return bool(names & _PROJECT_SIGNATURES) or len(list(path.rglob("*.py"))) > 5


def scan(script_retention: int, project_retention: int) -> list[CleanTarget]:
    """Scan ``%TEMP%\\opencode\\`` and return candidates for deletion."""
    results: list[CleanTarget] = []
    if not OPENCODE_TEMP.exists():
        log.info("Directory not found: %s", OPENCODE_TEMP)
        return results

    now = datetime.now()
    script_cutoff = now - timedelta(days=script_retention)
    project_cutoff = now - timedelta(days=project_retention)

    try:
        for entry in OPENCODE_TEMP.iterdir():
            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            except OSError:
                continue

            age = (now - mtime).days

            if entry.is_dir():
                # Directories use the project retention
                if mtime > project_cutoff:
                    continue
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                is_project = _looks_like_project(entry)
            else:
                # Loose files use the script retention
                if mtime > script_cutoff:
                    continue
                size = entry.stat().st_size
                is_project = False

            results.append(CleanTarget(entry, size, age, is_project))
    except PermissionError:
        log.warning("Permission denied scanning %s", OPENCODE_TEMP)

    return results


# ─── Execution ───────────────────────────────────────────────────────────────

def _delete(target: CleanTarget) -> bool:
    """Delete a file or directory tree.  Returns True on success."""
    try:
        if target.path.is_dir():
            shutil.rmtree(target.path, ignore_errors=False)
        else:
            target.path.unlink()
        return True
    except (OSError, PermissionError) as e:
        log.error("Failed to delete %s: %s", target.path, e)
        return False


def run(args: argparse.Namespace) -> None:
    print_header("Temp File Cleanup  (%TEMP%\\opencode\\)")
    ensure_opencode_stopped(log)

    settings = get_settings()
    script_retention = args.scripts if args.scripts is not None else settings["temp_script_retention_days"]
    project_retention = args.projects if args.projects is not None else settings["temp_project_retention_days"]

    # ── Discover ─────────────────────────────────────────────────────────
    log.info("Scanning: %s", OPENCODE_TEMP)
    targets = scan(script_retention, project_retention)

    if not targets:
        print("  No files to clean.\n")
        return

    # ── Report ───────────────────────────────────────────────────────────
    scripts = [t for t in targets if not t.is_project]
    projects = [t for t in targets if t.is_project]
    total_size = sum(t.size_bytes for t in targets)

    print(f"  Retention: scripts >{script_retention}d, projects >{project_retention}d")
    print(f"  Candidates: {len(targets)} items  ({format_size(total_size)})\n")

    for label, items in [("AI‑generated scripts / data", scripts),
                          ("Cloned / scaffolded projects", projects)]:
        if not items:
            continue
        cat_size = sum(i.size_bytes for i in items)
        print(f"  [{label}]  —  {len(items)} items, {format_size(cat_size)}")
        top = sorted(items, key=lambda x: x.size_bytes, reverse=True)[:5]
        for t in top:
            type_tag = "[DIR]" if t.path.is_dir() else "[FILE]"
            print(f"    {format_size(t.size_bytes):>8}  {t.age_days:>3}d ago  {type_tag}  {t.path.name}")
        if len(items) > 5:
            print(f"    ... and {len(items) - 5} more")
        print()

    # ── Execute ──────────────────────────────────────────────────────────
    if not args.execute:
        print_header("DRY RUN — add --execute to apply")
        return

    print_header("EXECUTING")

    deleted_count = 0
    deleted_size = 0
    failed_count = 0

    for target in targets:
        if not args.quiet:
            log.info("Deleting: %s", target.path)
        if _delete(target):
            deleted_count += 1
            deleted_size += target.size_bytes
        else:
            failed_count += 1

    # ── Summary ──────────────────────────────────────────────────────────
    print_header("Summary")
    print(f"  Deleted: {deleted_count} items  ({format_size(deleted_size)})")
    if failed_count:
        print(f"  Failed:  {failed_count} items")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up AI-generated temp files from %%TEMP%%\\opencode\\.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m cleanup.tempfile                         # dry-run\n"
               "  python -m cleanup.tempfile --execute               # delete\n"
               "  python -m cleanup.tempfile --execute --scripts 3   # aggressive script cleanup",
    )
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete files (default: dry-run only)")
    parser.add_argument("--scripts", type=int, default=None,
                        help="Retention days for loose files (see settings.jsonc)")
    parser.add_argument("--projects", type=int, default=None,
                        help="Retention days for project directories (see settings.jsonc)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-file deletion messages")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
