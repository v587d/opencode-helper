#!/usr/bin/env python3
"""
Session Harness — Overall Review Module
========================================

Ties together insights from all analysis dimensions into a single
overview: session efficiency, lifecycle patterns, agent switching
behavior, and optimization suggestions.

Can run standalone or be imported:
    python -m analysis.harness [args]
"""

import argparse
import sys
from datetime import datetime, timedelta

from utilities import print_header, print_separator, setup_logging
from analysis.common import query, invoke, render_prompt

log = setup_logging("harness")


# ═════════════════════════════════════════════════════════════════════
#  Subcommand registration
# ═════════════════════════════════════════════════════════════════════

def register_subparser(subparsers):
    """Register harness subcommand for unified CLI (main.py)."""
    p = subparsers.add_parser(
        "harness",
        help="Overall session review — efficiency, lifecycle, and optimization",
    )
    p.add_argument("--no-ai", action="store_true",
                   help="Skip AI optimization suggestions (data-only mode)")
    p.add_argument("--days", type=int, default=7,
                   help="Analyze last N days (default: 7)")
    p.set_defaults(func=run)


# ═════════════════════════════════════════════════════════════════════
#  Data queries
# ═════════════════════════════════════════════════════════════════════

SQL_SESSION_OVERVIEW = """
SELECT COUNT(*) as sessions,
       (SELECT COUNT(*) FROM message m
        JOIN session s ON m.session_id = s.id
        WHERE s.time_created > ?) as messages,
       SUM(tokens_input) as total_input,
       SUM(tokens_output) as total_output,
       SUM(cost) as total_cost
FROM session WHERE time_created > ?
"""

SQL_SESSION_LIFECYCLE = """
SELECT s.id, s.title, s.agent,
       COUNT(m.id) as msg_count,
       (MAX(m.time_created) - MIN(m.time_created)) / 60000.0 as duration_min,
       s.tokens_input, s.cost
FROM session s
JOIN message m ON m.session_id = s.id
WHERE s.time_created > ?
GROUP BY s.id ORDER BY msg_count DESC
"""

SQL_AGENT_SWITCHING = """
SELECT sm.type, COUNT(*) as cnt
FROM session_message sm
JOIN session s ON sm.session_id = s.id
WHERE s.time_created > ?
GROUP BY sm.type
"""

SQL_ARCHIVE_STATUS = """
SELECT
  SUM(CASE WHEN time_archived IS NULL THEN 1 ELSE 0 END) as active,
  SUM(CASE WHEN time_archived IS NOT NULL THEN 1 ELSE 0 END) as archived
FROM session WHERE time_created > ?
"""


# ═════════════════════════════════════════════════════════════════════
#  Formatting helpers
# ═════════════════════════════════════════════════════════════════════

def _fmt_tokens(n) -> str:
    """Format token counts with K/M suffix."""
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_cost(v) -> str:
    """Format cost with dollar sign."""
    if v is None:
        return "—"
    return f"${v:.4f}"


def _fmt_duration(minutes) -> str:
    """Format duration in minutes to human-readable string."""
    if minutes is None:
        return "—"
    m = float(minutes)
    if m < 1:
        return f"{m * 60:.0f}s"
    if m < 60:
        return f"{m:.1f}m"
    h = m / 60
    if h < 24:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


# ═════════════════════════════════════════════════════════════════════
#  Display sections
# ═════════════════════════════════════════════════════════════════════

def show_session_overview(cutoff_ms: int) -> dict:
    """Display session overview and return raw data for AI prompt."""
    rows = query(SQL_SESSION_OVERVIEW, (cutoff_ms, cutoff_ms))
    if not rows or rows[0] is None:
        print("  No session data found for the specified period.")
        return {}

    sessions, messages, total_input, total_output, total_cost = rows[0]

    print_header("Session Overview")
    print(f"  Sessions:      {sessions or 0}")
    print(f"  Messages:      {messages or 0}")
    print(f"  Input tokens:  {_fmt_tokens(total_input)}")
    print(f"  Output tokens:  {_fmt_tokens(total_output)}")
    print(f"  Total cost:    {_fmt_cost(total_cost)}")

    if sessions and messages:
        print(f"  Avg messages:  {messages / sessions:.1f} per session")
    if sessions and total_cost:
        print(f"  Avg cost:      {_fmt_cost(total_cost / sessions)} per session")

    return {
        "sessions": sessions or 0,
        "messages": messages or 0,
        "total_input": total_input or 0,
        "total_output": total_output or 0,
        "total_cost": total_cost or 0,
    }


def show_session_lifecycle(cutoff_ms: int, limit: int = 15) -> list:
    """Display session lifecycle details and return rows for AI prompt."""
    rows = query(SQL_SESSION_LIFECYCLE, (cutoff_ms,))
    if not rows:
        print("\n  No session lifecycle data found.")
        return []

    print_header("Session Lifecycle")
    print(f"  {'ID':<36} {'Title':<24} {'Agent':<14} {'Msgs':>5} {'Duration':>10} {'Cost':>10}")
    print_separator()

    for row in rows[:limit]:
        sid, title, agent, msg_count, duration, _, cost = row
        title_display = (title or "(no title)")[:24]
        agent_display = (agent or "—")[:14]
        print(f"  {sid:<36} {title_display:<24} {agent_display:<14} "
              f"{msg_count:>5} {_fmt_duration(duration):>10} {_fmt_cost(cost):>10}")

    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more session(s)")

    # Summary statistics
    durations = [r[4] for r in rows if r[4] is not None and r[4] > 0]
    msg_counts = [r[3] for r in rows]
    if durations:
        avg_dur = sum(durations) / len(durations)
        min_dur = min(durations)
        max_dur = max(durations)
        print()
        print(f"  Duration — avg: {_fmt_duration(avg_dur)}, "
              f"min: {_fmt_duration(min_dur)}, max: {_fmt_duration(max_dur)}")
    if msg_counts:
        avg_msgs = sum(msg_counts) / len(msg_counts)
        print(f"  Messages — avg: {avg_msgs:.1f}, "
              f"min: {min(msg_counts)}, max: {max(msg_counts)}")

    return rows


def show_agent_switching(cutoff_ms: int) -> list:
    """Display agent/model switching events and return rows for AI prompt."""
    rows = query(SQL_AGENT_SWITCHING, (cutoff_ms,))
    if not rows:
        print("\n  No agent/model switching events found.")
        return []

    print_header("Agent/Model Switching Events")
    print(f"  {'Event Type':<30} {'Count':>8}")
    print_separator()

    total_events = 0
    for evt_type, cnt in rows:
        print(f"  {(evt_type or '(unknown)'):<30} {cnt:>8}")
        total_events += cnt

    print_separator()
    print(f"  {'TOTAL':<30} {total_events:>8}")

    return rows


def show_archive_status(cutoff_ms: int) -> dict:
    """Display archive vs active session counts."""
    rows = query(SQL_ARCHIVE_STATUS, (cutoff_ms,))
    if not rows or rows[0] is None:
        print("\n  No archive status data found.")
        return {}

    active, archived = rows[0]
    total = (active or 0) + (archived or 0)

    print_header("Session Status")
    print(f"  Active:    {active or 0}")
    print(f"  Archived:  {archived or 0}")
    if total > 0:
        print(f"  Archive %: {(archived or 0) / total * 100:.1f}%")

    return {"active": active or 0, "archived": archived or 0}


def show_efficiency_snapshot(cutoff_ms: int) -> dict:
    """Quick glance at tool/MCP/skill efficiency from the parts table."""
    # Tool call stats
    tool_sql = """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN json_extract(data, '$.state.status') = 'error'
                   THEN 1 ELSE 0 END) as errors
        FROM part
        WHERE json_extract(data, '$.type') = 'tool'
          AND session_id IN (SELECT id FROM session WHERE time_created > ?)
    """
    tool_rows = query(tool_sql, (cutoff_ms,))

    # MCP call stats
    mcp_sql = """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN json_extract(data, '$.state.status') = 'error'
                   THEN 1 ELSE 0 END) as errors
        FROM part
        WHERE json_extract(data, '$.type') = 'tool'
          AND json_extract(data, '$.tool') LIKE 'mcp_%'
          AND session_id IN (SELECT id FROM session WHERE time_created > ?)
    """
    mcp_rows = query(mcp_sql, (cutoff_ms,))

    # Skill invocation stats
    skill_sql = """
        SELECT COUNT(*) as total
        FROM part
        WHERE json_extract(data, '$.type') = 'tool'
          AND json_extract(data, '$.tool') = 'skill'
          AND session_id IN (SELECT id FROM session WHERE time_created > ?)
    """
    skill_rows = query(skill_sql, (cutoff_ms,))

    print_header("Efficiency Snapshot")

    if tool_rows and tool_rows[0]:
        total_tools, tool_errors = tool_rows[0]
        total_tools = total_tools or 0
        tool_errors = tool_errors or 0
        err_rate = tool_errors * 100.0 / total_tools if total_tools else 0
        print(f"  Tool calls:    {total_tools}  ({tool_errors} errors, {err_rate:.1f}% error rate)")
    else:
        total_tools, tool_errors = 0, 0
        print("  Tool calls:    no data")

    if mcp_rows and mcp_rows[0]:
        total_mcp, mcp_errors = mcp_rows[0]
        total_mcp = total_mcp or 0
        mcp_errors = mcp_errors or 0
        mcp_rate = mcp_errors * 100.0 / total_mcp if total_mcp else 0
        print(f"  MCP calls:     {total_mcp}  ({mcp_errors} errors, {mcp_rate:.1f}% error rate)")
    else:
        total_mcp, mcp_errors = 0, 0
        print("  MCP calls:     no data")

    if skill_rows and skill_rows[0]:
        total_skills = skill_rows[0][0] or 0
        print(f"  Skill calls:   {total_skills}")
    else:
        total_skills = 0
        print("  Skill calls:   no data")

    return {
        "tool_total": total_tools,
        "tool_errors": tool_errors,
        "mcp_total": total_mcp,
        "mcp_errors": mcp_errors,
        "skill_total": total_skills,
    }


# ═════════════════════════════════════════════════════════════════════
#  AI interpretation
# ═════════════════════════════════════════════════════════════════════

def run_ai_analysis(overview: dict, lifecycle_rows: list,
                    switch_rows: list, archive: dict, efficiency: dict,
                    days: int):
    """Render prompt and invoke AI for overall optimization suggestions."""
    # Compression settings (all 0 = disabled, opt-in)
    from utilities import get_settings
    _cs = get_settings()
    _max_rows = int(_cs.get("analysis_max_rows_per_section", 0) or 0)

    # Session-lifecycle cap (default 10 if not set; opt-in can override)
    lifecycle_cap = _max_rows if _max_rows > 0 else 10
    switch_cap = _max_rows if _max_rows > 0 else len(switch_rows)

    sections = []

    # Session overview
    if overview:
        sections.append(
            "## Session Overview\n"
            f"- Sessions: {overview.get('sessions', 0)}\n"
            f"- Messages: {overview.get('messages', 0)}\n"
            f"- Input tokens: {overview.get('total_input', 0)}\n"
            f"- Output tokens: {overview.get('total_output', 0)}\n"
            f"- Total cost: ${overview.get('total_cost', 0):.4f}\n"
        )

    # Lifecycle summary
    if lifecycle_rows:
        # Stats computed across ALL rows (not just the displayed slice)
        durations = [r[4] for r in lifecycle_rows if r[4] is not None and r[4] > 0]
        msg_counts = [r[3] for r in lifecycle_rows]
        lifecycle_slice = lifecycle_rows[:lifecycle_cap]
        lines = [f"Top {len(lifecycle_slice)} sessions by message count:"]
        for row in lifecycle_slice:
            sid, title, agent, msg_count, duration, _, cost = row
            lines.append(
                f"  {sid} | {(title or '(no title)')[:30]} | "
                f"agent={agent or '?'} | msgs={msg_count} | "
                f"dur={_fmt_duration(duration)} | cost={_fmt_cost(cost)}"
            )
        if _max_rows > 0 and len(lifecycle_rows) > lifecycle_cap:
            lines.append(f"  ... and {len(lifecycle_rows) - lifecycle_cap} more session(s)")
        if durations:
            lines.append(
                f"\nDuration stats: avg={_fmt_duration(sum(durations)/len(durations))}, "
                f"min={_fmt_duration(min(durations))}, max={_fmt_duration(max(durations))}"
            )
        if msg_counts:
            lines.append(
                f"Message stats: avg={sum(msg_counts)/len(msg_counts):.1f}, "
                f"min={min(msg_counts)}, max={max(msg_counts)}"
            )
        sections.append("## Session Lifecycle\n" + "\n".join(lines))

    # Switching events
    if switch_rows:
        switch_slice = switch_rows[:switch_cap]
        lines = ["Event type | Count"]
        lines.append("-" * 40)
        for evt_type, cnt in switch_slice:
            lines.append(f"{evt_type or '(unknown)'} | {cnt}")
        if _max_rows > 0 and len(switch_rows) > switch_cap:
            lines.append(f"... and {len(switch_rows) - switch_cap} more event(s)")
        sections.append("## Agent/Model Switching\n" + "\n".join(lines))

    # Archive status
    if archive:
        sections.append(
            "## Session Status\n"
            f"- Active: {archive.get('active', 0)}\n"
            f"- Archived: {archive.get('archived', 0)}\n"
        )

    # Efficiency snapshot
    if efficiency:
        sections.append(
            "## Efficiency Snapshot\n"
            f"- Tool calls: {efficiency.get('tool_total', 0)} "
            f"({efficiency.get('tool_errors', 0)} errors)\n"
            f"- MCP calls: {efficiency.get('mcp_total', 0)} "
            f"({efficiency.get('mcp_errors', 0)} errors)\n"
            f"- Skill calls: {efficiency.get('skill_total', 0)}\n"
        )

    data_block = "\n\n".join(sections)

    prompt = render_prompt("harness", command="harness", days=str(days), data=data_block)
    if not prompt:
        prompt = (
            f"Analyze the following OpenCode session data from the last {days} days. "
            "Identify efficiency patterns, lifecycle issues, and optimization opportunities. "
            "Focus on: session duration patterns, cost efficiency, agent switching overhead, "
            "and actionable suggestions for improvement.\n\n"
            f"{data_block}"
        )

    print_header("AI Optimization Suggestions")
    result = invoke(prompt)
    if result:
        print(result)
    else:
        print("  (AI analysis unavailable — the model / OpenCode CLI returned no usable response.)")
        print("  Try: --no-ai for data-only output, or change analysis_model / analysis_variant in settings.jsonc.")
        log.warning("AI invocation returned empty result.")


# ═════════════════════════════════════════════════════════════════════
#  Main entry point
# ═════════════════════════════════════════════════════════════════════

def run(args: argparse.Namespace):
    """Compute and display overall session review."""
    cutoff_ms = int((datetime.now() - timedelta(days=args.days)).timestamp() * 1000)

    print_header(f"Session Harness — Last {args.days} Day(s)")
    print(f"  Cutoff: {datetime.fromtimestamp(cutoff_ms / 1000).strftime('%Y-%m-%d %H:%M')}")

    # ── Data collection and display ──────────────────────────────────
    overview = show_session_overview(cutoff_ms)
    lifecycle_rows = show_session_lifecycle(cutoff_ms)
    switch_rows = show_agent_switching(cutoff_ms)
    archive = show_archive_status(cutoff_ms)
    efficiency = show_efficiency_snapshot(cutoff_ms)

    # ── AI interpretation ────────────────────────────────────────────
    if not args.no_ai:
        run_ai_analysis(overview, lifecycle_rows, switch_rows,
                        archive, efficiency, args.days)
    else:
        print("\n  (--no-ai: skipping AI optimization suggestions)")


def main():
    parser = argparse.ArgumentParser(
        description="Overall session review — efficiency, lifecycle, and optimization.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m analysis.harness                  # last 7 days\n"
               "  python -m analysis.harness --days 30        # last 30 days\n"
               "  python -m analysis.harness --no-ai           # data only, no AI",
    )
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip AI optimization suggestions (data-only mode)")
    parser.add_argument("--days", type=int, default=7,
                        help="Analyze last N days (default: 7)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()