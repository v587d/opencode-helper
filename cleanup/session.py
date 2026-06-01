#!/usr/bin/env python3
"""
Session Cleanup Module
======================

Deletes old sessions from OpenCode's SQLite database and reclaims
disk space via VACUUM. Archived sessions (time_archived IS NOT NULL)
and sessions in the save-list are permanently preserved.

Can run standalone or be imported:
    python -m cleanup.session [args]
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from utilities import (
    DB_PATH, SETTINGS_PATH, STORAGE_DIR, OPENCODE_DATA,
    ensure_opencode_stopped, format_size, format_time,
    get_db_connection, get_settings, print_header, setup_logging,
)

log = setup_logging("session")


def register_subparser(subparsers):
    """Register session subcommand for unified CLI (main.py)."""
    default_days = get_settings()["session_retention_days"]
    p = subparsers.add_parser(
        "session",
        help="Clean up old OpenCode sessions and reclaim disk space",
    )
    p.add_argument("--execute", action="store_true",
                   help="Apply changes (default: dry-run only)")
    p.add_argument("--days", type=int, default=default_days,
                   help=f"Retention period in days (default: {default_days})")
    p.add_argument("--no-vacuum", action="store_true",
                   help="Skip VACUUM after deletion")
    p.add_argument("--no-backup", action="store_true",
                   help="Skip automatic backup")
    p.add_argument("--add", metavar="SESSION_ID",
                   help="Add session to save list (auto-labels from DB title)")
    p.add_argument("--label", metavar="DESCRIPTION",
                   help="Custom label for --add (default: DB title)")
    p.add_argument("--remove", metavar="SESSION_ID",
                   help="Remove session from save list")
    p.add_argument("--list", action="store_true",
                   help="Show save list contents with DB status")
    p.set_defaults(func=run)


# ─── Save List ───────────────────────────────────────────────────────────────

def load_save_list() -> set:
    """Load session IDs to permanently preserve from settings.jsonc."""
    data = get_settings().get("session_save_list", {})
    if isinstance(data, list):
        return set(data)
    elif isinstance(data, dict):
        return set(data.keys())
    return set()


def _modify_settings_save_list(add_id: str = None, remove_id: str = None,
                               label: str = None, conn=None) -> bool:
    """Modify session_save_list in settings.jsonc, preserving file structure.
    
    Uses line-level editing to keep comments and formatting intact outside
    the save_list block.  Comments inside the save_list block are NOT
    preserved — re-add them manually if needed.
    """
    text = SETTINGS_PATH.read_text(encoding="utf-8")
    lines = text.split("\n")

    # Locate the save_list block with brace-depth tracking
    start_line = None
    depth = 0
    for i, line in enumerate(lines):
        if not start_line and '"session_save_list"' in line:
            start_line = i
        if start_line is not None:
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                end_line = i
                break  # reached closing brace
    else:
        print("Error: could not locate session_save_list in settings.jsonc")
        return False

    # Parse current entries from the block (strip comments)
    block_text = "\n".join(lines[start_line:end_line + 1])
    entries = {}
    for m in re.finditer(r'"([^"]+)"\s*:\s*"([^"]*)"', block_text):
        entries[m.group(1)] = m.group(2)

    if add_id:
        if add_id in entries:
            print(f"Already in save list: {add_id}")
            return False
        if label:
            entries[add_id] = label
        elif conn:
            row = conn.execute(
                "SELECT title FROM session WHERE id = ?", (add_id,)
            ).fetchone()
            entries[add_id] = (row[0] or "(no title)") if row else "(unknown)"
        else:
            entries[add_id] = "(manual)"
        print(f"Added: {add_id}  ({entries[add_id]})")

    if remove_id:
        if remove_id not in entries:
            print(f"Not in save list: {remove_id}")
            return False
        del entries[remove_id]
        print(f"Removed: {remove_id}")

    # Reconstruct block
    base_indent = lines[start_line][:len(lines[start_line])
                                     - len(lines[start_line].lstrip())]
    entry_indent = base_indent + "    "

    new_lines = [f'{base_indent}"session_save_list": {{']
    if entries:
        for sid, lbl in entries.items():
            new_lines.append(f'{entry_indent}"{sid}": "{lbl}"')
    new_lines.append(f"{base_indent}}},")

    # Splice back
    result = lines[:start_line] + new_lines + lines[end_line + 1:]
    SETTINGS_PATH.write_text("\n".join(result), encoding="utf-8")
    return True


def list_save_entries():
    """Display save list with session status from the database."""
    save_data = get_settings().get("session_save_list", {})
    if isinstance(save_data, list):
        entries = {sid: "(no label)" for sid in save_data}
    elif isinstance(save_data, dict):
        entries = dict(save_data)
    else:
        entries = {}

    if not entries:
        print("Save list is empty.")
        return

    # Open DB read-only to check existence
    conn = None
    db_info = {}
    if DB_PATH.exists():
        try:
            conn = get_db_connection()
            for sid in entries:
                row = conn.execute(
                    "SELECT title, time_archived IS NOT NULL, "
                    "datetime(time_created/1000,'unixepoch','localtime') "
                    "FROM session WHERE id = ?", (sid,)
                ).fetchone()
                db_info[sid] = row  # (title, is_archived, created) or None
        except Exception:
            pass
    if conn:
        conn.close()

    print_header("Session Save List")
    print(f"  {'ID':<42} {'Status':<10} {'Created':<18}  Label / Title")
    print(f"  {'─'*42} {'─'*10} {'─'*18}  {'─'*40}")
    for sid, lbl in entries.items():
        info = db_info.get(sid)
        if info is None:
            status, created, detail = "MISSING", "—", "(not in database)"
        else:
            status = "archived" if info[1] else "active"
            created = info[2] or "—"
            detail = lbl if lbl else (info[0] or "(no title)")
        print(f"  {sid:<42} {status:<10} {created:<18}  {detail[:50]}")


# ─── Database Operations ─────────────────────────────────────────────────────

def check_db_integrity(conn) -> bool:
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    return result == "ok"


def get_db_stats(conn) -> dict:
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    session_count = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    return {
        "page_count": page_count,
        "freelist_count": freelist,
        "page_size": page_size,
        "total_bytes": page_count * page_size,
        "free_bytes": freelist * page_size,
        "used_bytes": (page_count - freelist) * page_size,
        "journal_mode": journal,
        "session_count": session_count,
    }


def vacuum_database(conn):
    """
    Safe VACUUM sequence for WAL-mode SQLite:
      1. Pre-checkpoint (TRUNCATE) — start clean
      2. VACUUM — rebuilds compact DB
      3. Post-checkpoint (TRUNCATE) — shrink WAL to zero
      4. PRAGMA optimize — refresh query planner stats
    """
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if journal_mode == "wal":
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    if journal_mode == "wal":
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA optimize")


def list_candidate_sessions(conn, cutoff_ms: int, save_ids: set) -> list:
    """Return sessions older than cutoff that are NOT in the save list."""
    rows = conn.execute(
        """SELECT s.id, s.title, s.project_id, s.directory,
                  s.time_created, s.time_updated, s.time_archived,
                  s.cost, s.tokens_input, s.tokens_output,
                  (SELECT COUNT(*) FROM message WHERE session_id = s.id),
                  (SELECT COUNT(*) FROM part WHERE session_id = s.id)
           FROM session s
           WHERE s.time_created < ?
             AND s.time_archived IS NULL
           ORDER BY s.time_created ASC""",
        (cutoff_ms,),
    ).fetchall()

    candidates = []
    for row in rows:
        sid = row[0]
        if sid in save_ids:
            continue
        candidates.append({
            "id": sid,
            "title": row[1] or "(no title)",
            "project_id": row[2],
            "directory": row[3],
            "time_created": row[4],
            "time_updated": row[5],
            "time_archived": row[6],
            "cost": row[7] or 0,
            "tokens_input": row[8] or 0,
            "tokens_output": row[9] or 0,
            "msg_count": row[10] or 0,
            "part_count": row[11] or 0,
        })
    return candidates


def delete_sessions(conn, session_ids: list) -> int:
    """Delete sessions. CASCADE handles message/part/todo/session_share.
    Also cleans orphan event_sequence and event records."""
    if not session_ids:
        return 0
    placeholders = ",".join("?" * len(session_ids))
    deleted = conn.execute(
        f"DELETE FROM session WHERE id IN ({placeholders})", session_ids
    ).rowcount
    conn.execute(
        """DELETE FROM event WHERE aggregate_id IN (
               SELECT aggregate_id FROM event_sequence
               WHERE aggregate_id NOT IN (SELECT id FROM session))"""
    )
    conn.execute(
        "DELETE FROM event_sequence WHERE aggregate_id NOT IN (SELECT id FROM session)"
    )
    return deleted


def clean_storage_json(session_ids: list) -> int:
    """Delete matching session JSON files in storage subdirectories."""
    cleaned = 0
    for subdir in ("session_diff", "agent-usage-reminder"):
        dir_path = STORAGE_DIR / subdir
        if not dir_path.exists():
            continue
        for sid in session_ids:
            file_path = dir_path / f"{sid}.json"
            if file_path.exists():
                file_path.unlink()
                cleaned += 1
    return cleaned


def backup_database() -> Optional[Path]:
    """Create a timestamped backup using SQLite backup API."""
    if not DB_PATH.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = DB_PATH.with_suffix(f".backup_{timestamp}.db")
    import sqlite3
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
        log.info("Backup created: %s (%s)", backup_path.name, format_size(backup_path.stat().st_size))
        return backup_path
    finally:
        src.close()
        dst.close()


# ─── Display ─────────────────────────────────────────────────────────────────

def print_stats(stats: dict, label: str = "Database"):
    print(f"\n{label}:")
    print(f"  Journal mode:    {stats['journal_mode']}")
    print(f"  Total size:      {format_size(stats['total_bytes'])}")
    print(f"  Used data:       {format_size(stats['used_bytes'])}")
    if stats['total_bytes'] > 0:
        pct = stats['free_bytes'] * 100 // stats['total_bytes']
        print(f"  Free space:      {format_size(stats['free_bytes'])} ({pct}%)")
    print(f"  Sessions:        {stats['session_count']}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace):
    # ── Save-list management (does not require opencode stopped) ────────
    if args.list:
        list_save_entries()
        return

    if args.add:
        conn = get_db_connection() if DB_PATH.exists() else None
        try:
            _modify_settings_save_list(add_id=args.add, label=args.label, conn=conn)
        finally:
            if conn:
                conn.close()
        return

    if args.remove:
        _modify_settings_save_list(remove_id=args.remove)
        return

    # ── Phase 1: Analysis (read-only, safe with OpenCode running) ─────
    print_header("Session Cleanup")

    if not DB_PATH.exists():
        log.error("Database not found at %s", DB_PATH)
        sys.exit(1)

    db_size_before = DB_PATH.stat().st_size
    print(f"Database: {DB_PATH}")
    print(f"  Current size: {format_size(db_size_before)}")

    save_ids = load_save_list()
    if save_ids:
        print(f"\nSave list: {len(save_ids)} session(s) permanently preserved.")
        for sid in save_ids:
            print(f"  - {sid}")

    conn = get_db_connection()
    try:
        if not check_db_integrity(conn):
            log.error("Database integrity check failed! Aborting.")
            sys.exit(1)
        log.info("Integrity check passed.")

        stats_before = get_db_stats(conn)
        print_stats(stats_before)

        # ── Phase 2: Analysis ────────────────────────────────────────────
        cutoff = datetime.now() - timedelta(days=args.days)
        cutoff_ms = int(cutoff.timestamp() * 1000)

        print_header(f"Candidates (older than {args.days} days)")
        print(f"  Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M')}")

        candidates = list_candidate_sessions(conn, cutoff_ms, save_ids)

        if not candidates:
            print("  No sessions to delete.")
        else:
            total_cost = sum(c["cost"] for c in candidates)
            total_tokens = sum(c["tokens_input"] + c["tokens_output"] for c in candidates)
            print(f"  Candidates: {len(candidates)} session(s)")
            print(f"  Total cost:  ${total_cost:.4f}")
            print(f"  Total tokens: {total_tokens:,}\n")
            header = f"  {'ID':<42} {'Created':<18} {'Msgs':>5} {'Parts':>6}  {'Directory':<36} Title"
            print(header)
            print("  " + "─" * (len(header) - 2))
            for c in candidates:
                print(f"  {c['id']:<42} {format_time(c['time_created']):<18} "
                      f"{c['msg_count']:>5} {c['part_count']:>6}  {c['directory']:<36} {c['title'][:30]}")

        # ── Phase 3: Execute ─────────────────────────────────────────────
        if not args.execute:
            print_header("DRY RUN — add --execute to apply (read-only, safe with OpenCode running)")
            if candidates:
                print(f"  Would delete: {len(candidates)} sessions")
            reclaimable = format_size(stats_before["free_bytes"])
            print(f"  Would VACUUM: reclaim ~{reclaimable}")
            return

        print_header("EXECUTING")
        ensure_opencode_stopped(log)

        # Backup
        if not args.no_backup:
            backup_database()
        else:
            log.warning("Backup skipped (--no-backup).")

        actions = []

        # Delete sessions
        if candidates:
            ids = [c["id"] for c in candidates]
            log.info("Deleting %d session(s)...", len(ids))
            deleted = delete_sessions(conn, ids)
            log.info("Deleted %d session(s) from database.", deleted)
            actions.append(f"Deleted {deleted} sessions")

            json_cleaned = clean_storage_json(ids)
            if json_cleaned:
                log.info("Cleaned %d storage JSON file(s).", json_cleaned)
                actions.append(f"Cleaned {json_cleaned} JSON files")
            conn.commit()

        # VACUUM
        if not args.no_vacuum:
            stats_mid = get_db_stats(conn)
            if stats_mid["free_bytes"] > 1 * 1024 * 1024 or not candidates:
                log.info("Running VACUUM...")
                vacuum_database(conn)
                conn.commit()
                stats_final = get_db_stats(conn)
                saved = db_size_before - DB_PATH.stat().st_size
                if saved > 0:
                    log.info("Reclaimed %s.", format_size(saved))
                    actions.append(f"Reclaimed {format_size(saved)}")
            else:
                log.info("Database already compact; skipping VACUUM.")

        # ── Summary ──────────────────────────────────────────────────────
        print_header("Summary")
        for a in actions:
            print(f"  {a}")
        final_size = DB_PATH.stat().st_size
        if final_size < db_size_before:
            print(f"\n  {format_size(db_size_before)} → {format_size(final_size)}")
        print()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Clean up old OpenCode sessions and reclaim disk space.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m cleanup.session                         # dry-run\n"
               "  python -m cleanup.session --execute               # delete + vacuum\n"
               "  python -m cleanup.session --execute --days 14     # keep 14 days\n"
               "  python -m cleanup.session --add ses_xxx           # add to save list\n"
               "  python -m cleanup.session --add ses_xxx --label \"my note\"\n"
               "  python -m cleanup.session --remove ses_xxx        # remove from save list\n"
               "  python -m cleanup.session --list             # show save list",
    )
    parser.add_argument("--execute", action="store_true", help="Apply changes (default: dry-run only)")
    parser.add_argument("--days", type=int, default=get_settings()["session_retention_days"],
                        help="Retention period in days (see settings.jsonc)")
    parser.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM after deletion")
    parser.add_argument("--no-backup", action="store_true", help="Skip automatic backup")
    parser.add_argument("--add", metavar="SESSION_ID", help="Add session to save list")
    parser.add_argument("--label", metavar="DESCRIPTION", help="Custom label for --add")
    parser.add_argument("--remove", metavar="SESSION_ID", help="Remove session from save list")
    parser.add_argument("--list", action="store_true", help="Show save list contents")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
