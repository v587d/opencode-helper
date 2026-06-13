#!/usr/bin/env python3
"""
MCP Tool Call Analysis
======================

Analyzes MCP (Model Context Protocol) tool call patterns from OpenCode's
SQLite database — focusing on error diagnosis and per-server breakdowns.

Since OpenCode stores MCP calls as regular tool calls in the part table,
we identify MCP tools by prefix patterns (tavily_*, websearch_*, etc.).

Usage
-----
    python -m analysis.mcp                  # full analysis with AI diagnosis
    python -m analysis.mcp --no-ai          # data only, skip AI diagnosis
    python -m analysis.mcp --server tavily   # filter to a specific MCP server
"""

import argparse
import sys
from collections import defaultdict

from utilities import print_header, print_separator, setup_logging
from analysis.common import query, invoke, render_prompt

log = setup_logging("mcp")

# ─── MCP Server Classification ────────────────────────────────────────────────

# Maps tool-name prefixes to human-readable server labels.
# Order matters: longer/more-specific prefixes should come first so they
# match before shorter ones (e.g. "chrome-mcp-server" before "chrome").
SERVER_PREFIXES: list[tuple[str, str]] = [
    ("tavily_tavily_", "tavily"),
    ("websearch_web_search_exa", "websearch"),
    ("websearch_", "websearch"),
    ("context7_", "context7"),
    ("chrome-mcp-server_", "chrome"),
    ("grep_app_", "github-search"),
    ("PaddleOCR-VL_", "paddleocr"),
]

# SQL LIKE patterns used to identify MCP tool rows in the part table.
MCP_LIKE_CLAUSES = [
    "json_extract(p.data, '$.tool') LIKE 'tavily_%'",
    "json_extract(p.data, '$.tool') LIKE 'websearch%'",
    "json_extract(p.data, '$.tool') LIKE 'context7_%'",
    "json_extract(p.data, '$.tool') LIKE 'chrome-mcp%'",
    "json_extract(p.data, '$.tool') LIKE '%mcp%'",
    "json_extract(p.data, '$.tool') LIKE 'grep_app_%'",
    "json_extract(p.data, '$.tool') LIKE 'PaddleOCR-VL_%'",
]


def classify_tool(tool_name: str) -> str:
    """Map a tool name to its MCP server label.

    Falls back to 'other' if no known prefix matches, or 'mcp-generic'
    if the name contains 'mcp' but doesn't match a known server.
    """
    for prefix, label in SERVER_PREFIXES:
        if tool_name.startswith(prefix):
            return label
    if "mcp" in tool_name.lower():
        return "mcp-generic"
    return "other"


# ─── Unified CLI Registration ─────────────────────────────────────────────────

def register_subparser(subparsers):
    """Register mcp subcommand for unified CLI (main.py)."""
    p = subparsers.add_parser(
        "mcp",
        help="Analyze MCP tool call patterns and diagnose errors",
    )
    p.add_argument("--no-ai", action="store_true",
                   help="Skip AI-powered root cause diagnosis")
    p.add_argument("--server", metavar="NAME",
                   help="Filter to a specific MCP server (e.g. tavily, websearch)")
    p.set_defaults(func=run)


# ─── Data Queries ──────────────────────────────────────────────────────────────

def _mcp_where() -> str:
    """Build the SQL WHERE clause for MCP tool identification."""
    return " OR\n      ".join(MCP_LIKE_CLAUSES)


def get_mcp_overview() -> list[tuple]:
    """Return (tool, total_calls, error_count) per MCP tool."""
    sql = f"""
        SELECT json_extract(p.data, '$.tool') as tool,
               COUNT(*) as total,
               SUM(CASE WHEN json_extract(p.data, '$.state.status') = 'error'
                        THEN 1 ELSE 0 END) as errors
        FROM part p
        WHERE json_extract(p.data, '$.type') = 'tool'
          AND ({_mcp_where()})
        GROUP BY tool
        ORDER BY total DESC
    """
    return query(sql)


def get_mcp_errors() -> list[tuple]:
    """Return (tool, error_msg, count) for MCP tool errors."""
    sql = f"""
        SELECT json_extract(p.data, '$.tool') as tool,
               json_extract(p.data, '$.state.error') as error_msg,
               COUNT(*) as cnt
        FROM part p
        WHERE json_extract(p.data, '$.type') = 'tool'
          AND json_extract(p.data, '$.state.status') = 'error'
          AND ({_mcp_where()})
        GROUP BY tool, error_msg
        ORDER BY cnt DESC
    """
    return query(sql)


# ─── Display ───────────────────────────────────────────────────────────────────

def display_overview(rows: list[tuple], server_filter: str | None = None):
    """Print MCP tool overview table."""
    print_header("MCP Tool Overview")

    if not rows:
        print("  No MCP tool calls found in the database.\n")
        return

    # Filter by server if requested
    if server_filter:
        filtered = [(t, total, errs) for t, total, errs in rows
                     if classify_tool(t) == server_filter]
        if not filtered:
            print(f"  No MCP calls found for server '{server_filter}'.\n")
            return
        rows = filtered

    # Column widths
    total_calls = sum(r[1] for r in rows)
    total_errors = sum(r[2] for r in rows)

    print(f"  {'Tool':<40} {'Calls':>7} {'Errors':>7} {'Err%':>7}")
    print(f"  {'─' * 40} {'─' * 7} {'─' * 7} {'─' * 7}")
    for tool, total, errs in rows:
        err_pct = f"{errs / total * 100:.1f}%" if total > 0 else "—"
        print(f"  {tool:<40} {total:>7} {errs:>7} {err_pct:>7}")

    print()
    overall_pct = f"{total_errors / total_calls * 100:.1f}%" if total_calls > 0 else "—"
    print(f"  Total: {total_calls} calls, {total_errors} errors ({overall_pct})")
    print()


def display_error_breakdown(rows: list[tuple], server_filter: str | None = None):
    """Print MCP error breakdown grouped by tool."""
    print_header("MCP Error Breakdown")

    if not rows:
        print("  No MCP errors found.\n")
        return

    # Filter by server if requested
    if server_filter:
        rows = [(t, msg, cnt) for t, msg, cnt in rows
                if classify_tool(t) == server_filter]
        if not rows:
            print(f"  No errors found for server '{server_filter}'.\n")
            return

    # Group by tool
    by_tool: dict[str, list[tuple]] = defaultdict(list)
    for tool, msg, cnt in rows:
        by_tool[tool].append((msg, cnt))

    for tool in sorted(by_tool):
        errors = by_tool[tool]
        total = sum(cnt for _, cnt in errors)
        print(f"  {tool}  ({total} errors)")
        for msg, cnt in errors:
            display_msg = (msg or "(empty message)")[:120]
            print(f"    [{cnt:>3}x] {display_msg}")
        print()


def display_server_summary(overview_rows: list[tuple]):
    """Print per-server aggregate summary."""
    print_header("Per-Server Summary")

    if not overview_rows:
        print("  No data.\n")
        return

    server_stats: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "tools": []}
    )
    for tool, total, errs in overview_rows:
        server = classify_tool(tool)
        server_stats[server]["calls"] += total
        server_stats[server]["errors"] += errs
        server_stats[server]["tools"].append(tool)

    print(f"  {'Server':<16} {'Calls':>7} {'Errors':>7} {'Err%':>7}  Tools")
    print(f"  {'─' * 16} {'─' * 7} {'─' * 7} {'─' * 7}  {'─' * 30}")
    for server in sorted(server_stats):
        stats = server_stats[server]
        err_pct = f"{stats['errors'] / stats['calls'] * 100:.1f}%" if stats['calls'] > 0 else "—"
        tools_str = ", ".join(sorted(stats["tools"]))
        print(f"  {server:<16} {stats['calls']:>7} {stats['errors']:>7} {err_pct:>7}  {tools_str}")

    print()


# ─── Main ──────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace):
    """Entry point for the mcp subcommand."""
    server_filter = getattr(args, "server", None)
    if server_filter:
        server_filter = server_filter.lower()

    # ── Data queries ──────────────────────────────────────────────────────
    overview = get_mcp_overview()
    errors = get_mcp_errors()

    # ── Display ────────────────────────────────────────────────────────────
    display_overview(overview, server_filter)
    display_error_breakdown(errors, server_filter)
    display_server_summary(overview)

    # ── AI diagnosis ───────────────────────────────────────────────────────
    if not args.no_ai and (overview or errors):
        print_header("AI Root Cause Diagnosis")
        print("  Querying AI for error analysis...\n")

        # Build context for the prompt
        overview_lines = []
        for tool, total, errs in overview:
            if server_filter and classify_tool(tool) != server_filter:
                continue
            err_pct = f"{errs / total * 100:.1f}%" if total > 0 else "0%"
            overview_lines.append(f"  {tool}: {total} calls, {errs} errors ({err_pct})")

        error_lines = []
        for tool, msg, cnt in errors:
            if server_filter and classify_tool(tool) != server_filter:
                continue
            error_lines.append(f"  {tool} [{cnt}x]: {msg or '(empty)'}")

        prompt = render_prompt(
            "mcp_analysis",
            overview_data="\n".join(overview_lines) if overview_lines else "  (no data)",
            error_data="\n".join(error_lines) if error_lines else "  (no errors)",
            server_filter=server_filter or "all",
        )

        if prompt:
            result = invoke(prompt)
            if result:
                print(result)
            else:
                print("  (AI analysis unavailable — the model / OpenCode CLI returned no usable response.)")
                print("  Try: --no-ai for data-only output, or change analysis_model / analysis_variant in settings.jsonc.")
        else:
            print("  (Prompt template not found — skipping AI diagnosis)")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Analyze MCP tool call patterns and diagnose errors.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m analysis.mcp                     # full analysis\n"
               "  python -m analysis.mcp --no-ai              # data only\n"
               "  python -m analysis.mcp --server tavily      # tavily only",
    )
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip AI-powered root cause diagnosis")
    parser.add_argument("--server", metavar="NAME",
                        help="Filter to a specific MCP server (e.g. tavily, websearch)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()