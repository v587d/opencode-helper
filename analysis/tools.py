#!/usr/bin/env python3
"""
Tool Usage Efficiency Analysis
==============================

Analyzes OpenCode's SQLite database for tool usage patterns:
  - Tool call distribution and error rates
  - Read:Edit ratio (benchmark: >6 healthy, <2 degraded)
  - Tool error details with sample messages
  - Retry chain detection (3+ consecutive same-tool failures)

Can run standalone or be imported:
    python -m analysis.tools [args]
"""

import argparse
import sys

from utilities import print_header, print_separator, setup_logging
from analysis.common import query, invoke, render_prompt, truncate, head_tail, dedup_errors

log = setup_logging("tools")

# ─── Read:Edit ratio benchmarks ──────────────────────────────────────────────

RATIO_TIERS = [
    (6.0, "healthy", "target >6.0"),
    (2.0, "transitional", "target >6.0"),
    (0.0, "degraded", "target >6.0"),
]


def classify_ratio(ratio: float) -> tuple[str, str]:
    """Return (tier_label, benchmark_note) for a Read:Edit ratio."""
    for threshold, label, note in RATIO_TIERS:
        if ratio >= threshold:
            return label, note
    return RATIO_TIERS[-1][1], RATIO_TIERS[-1][2]


# ─── Subcommand registration ─────────────────────────────────────────────────

def register_subparser(subparsers):
    """Register tools subcommand for unified CLI (main.py)."""
    p = subparsers.add_parser(
        "tools",
        help="Analyze tool usage efficiency from OpenCode database",
    )
    p.add_argument("--no-ai", action="store_true",
                   help="Skip AI interpretation (data-only mode)")
    p.add_argument("--session", metavar="SID", dest="session",
                   help="Analyze a specific session (default: all sessions)")
    p.set_defaults(func=run)


# ─── Data queries ─────────────────────────────────────────────────────────────

def _session_filter(session_id: str | None) -> tuple[str, tuple]:
    """Return (WHERE clause fragment, params) for optional session filter."""
    if session_id:
        return "AND p.session_id = ?", (session_id,)
    return "", ()


def get_tool_distribution(session_id: str | None = None) -> list[tuple]:
    """Tool usage distribution: (tool, total, errors, error_rate%)."""
    where, params = _session_filter(session_id)
    sql = f"""
        SELECT json_extract(p.data, '$.tool') as tool,
               COUNT(*) as total,
               SUM(CASE WHEN json_extract(p.data, '$.state.status') = 'error'
                    THEN 1 ELSE 0 END) as errors,
               ROUND(SUM(CASE WHEN json_extract(p.data, '$.state.status') = 'error'
                    THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) as error_rate
        FROM part p
        WHERE json_extract(p.data, '$.type') = 'tool'
        {where}
        GROUP BY tool ORDER BY total DESC
    """
    return query(sql, params)


def get_read_edit_ratio(session_id: str | None = None) -> tuple[int, int, float]:
    """Return (reads, edits, ratio).  ratio = reads / edits (0 if no edits)."""
    where, params = _session_filter(session_id)
    sql = f"""
        SELECT
          SUM(CASE WHEN json_extract(data, '$.tool') = 'read' THEN 1 ELSE 0 END) as reads,
          SUM(CASE WHEN json_extract(data, '$.tool') = 'edit' THEN 1 ELSE 0 END) as edits
        FROM part WHERE json_extract(data, '$.type') = 'tool'
        {where}
    """
    rows = query(sql, params)
    if not rows or rows[0] is None:
        return 0, 0, 0.0
    reads = rows[0][0] or 0
    edits = rows[0][1] or 0
    ratio = reads / edits if edits > 0 else 0.0
    return reads, edits, ratio


def get_tool_errors(session_id: str | None = None) -> list[tuple]:
    """Tool errors: (tool, error_msg, count)."""
    where, params = _session_filter(session_id)
    sql = f"""
        SELECT json_extract(p.data, '$.tool') as tool,
               json_extract(p.data, '$.state.error') as error_msg,
               COUNT(*) as cnt
        FROM part p
        WHERE json_extract(p.data, '$.type') = 'tool'
          AND json_extract(p.data, '$.state.status') = 'error'
        {where}
        GROUP BY tool, error_msg ORDER BY cnt DESC
    """
    return query(sql, params)


def get_retry_chains(session_id: str | None = None) -> list[tuple]:
    """Retry chains: 3+ consecutive same-tool errors in one session.
    Returns (session_id, tool, consecutive_count)."""
    where, params = _session_filter(session_id)
    sql = f"""
        SELECT p.session_id, json_extract(p.data, '$.tool') as tool, COUNT(*) as consecutive
        FROM part p
        WHERE json_extract(p.data, '$.type') = 'tool'
          AND json_extract(p.data, '$.state.status') = 'error'
        {where}
        GROUP BY p.session_id, tool
        HAVING COUNT(*) >= 3
        ORDER BY consecutive DESC
    """
    return query(sql, params)


# ─── Display ──────────────────────────────────────────────────────────────────

def display_distribution(rows: list[tuple]):
    """Print tool usage distribution table."""
    print_header("Tool Usage Distribution")
    if not rows:
        print("  No tool usage data found.")
        return

    # Column widths
    tool_w = max(len(str(r[0])) for r in rows)
    tool_w = max(tool_w, 8)  # minimum header width

    print(f"  {'Tool':<{tool_w}}  {'Calls':>7}  {'Errors':>7}  {'Err%':>7}")
    print(f"  {'─' * tool_w}  {'─' * 7}  {'─' * 7}  {'─' * 7}")
    for tool, total, errors, err_rate in rows:
        print(f"  {tool:<{tool_w}}  {total:>7}  {errors:>7}  {err_rate:>6.1f}%")

    total_calls = sum(r[1] for r in rows)
    total_errors = sum(r[2] for r in rows)
    overall_rate = total_errors * 100.0 / total_calls if total_calls else 0
    print()
    print(f"  {'TOTAL':<{tool_w}}  {total_calls:>7}  {total_errors:>7}  {overall_rate:>6.1f}%")


def display_read_edit_ratio(reads: int, edits: int, ratio: float):
    """Print Read:Edit ratio with benchmark classification."""
    print_header("Read:Edit Ratio")
    tier, note = classify_ratio(ratio)
    print(f"  Reads:  {reads}")
    print(f"  Edits:  {edits}")
    print(f"  Ratio:  {ratio:.1f}  ({tier}, {note})")


def display_tool_errors(rows: list[tuple]):
    """Print tool error details."""
    print_header("Tool Error Details")
    if not rows:
        print("  No tool errors found.")
        return

    tool_w = max(len(str(r[0])) for r in rows)
    tool_w = max(tool_w, 8)

    print(f"  {'Tool':<{tool_w}}  {'Count':>6}  Error Message")
    print(f"  {'─' * tool_w}  {'─' * 6}  {'─' * 40}")
    for tool, error_msg, cnt in rows:
        msg = (error_msg or "(empty)")[:80]
        print(f"  {tool:<{tool_w}}  {cnt:>6}  {msg}")


def display_retry_chains(rows: list[tuple]):
    """Print retry chain detection results."""
    print_header("Retry Chain Detection (3+ consecutive same-tool errors)")
    if not rows:
        print("  No retry chains detected.")
        return

    print(f"  {'Session':<42}  {'Tool':<16}  {'Failures':>9}")
    print(f"  {'─' * 42}  {'─' * 16}  {'─' * 9}")
    for sid, tool, consecutive in rows:
        print(f"  {sid:<42}  {tool:<16}  {consecutive:>9}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace):
    """Compute and display tool usage efficiency analysis."""
    session_id = getattr(args, "session", None)
    no_ai = getattr(args, "no_ai", False)

    # ── Data collection ────────────────────────────────────────────────
    distribution = get_tool_distribution(session_id)
    reads, edits, ratio = get_read_edit_ratio(session_id)
    errors = get_tool_errors(session_id)
    chains = get_retry_chains(session_id)

    # ── Display ────────────────────────────────────────────────────────
    scope = f"session {session_id}" if session_id else "all sessions"
    print_header(f"Tool Usage Efficiency Analysis — {scope}")

    display_distribution(distribution)
    display_read_edit_ratio(reads, edits, ratio)
    display_tool_errors(errors)
    display_retry_chains(chains)

    # ── AI interpretation ──────────────────────────────────────────────
    if not no_ai:
        print_separator()
        print("  Invoking AI for interpretation...")

        # Compression settings (all 0 = disabled, opt-in)
        from utilities import get_settings
        _cs = get_settings()
        _max_err = int(_cs.get("analysis_max_error_chars", 0) or 0)
        _max_rows = int(_cs.get("analysis_max_rows_per_section", 0) or 0)
        _dedup_pre = int(_cs.get("analysis_error_dedup_prefix", 0) or 0)
        _dedup_k = int(_cs.get("analysis_error_dedup_top_k", 0) or 0)

        # Optional: fold similar error messages into shared buckets
        if _dedup_pre > 0:
            errors_for_prompt = dedup_errors(errors, prefix_len=_dedup_pre, top_k=_dedup_k)
        else:
            errors_for_prompt = errors

        # Optional: cap rows per section
        dist_slice = distribution[:_max_rows] if _max_rows > 0 else distribution
        err_slice = errors_for_prompt[:_max_rows] if _max_rows > 0 else errors_for_prompt
        chain_slice = chains[:_max_rows] if _max_rows > 0 else chains

        # Build data summary for the prompt
        dist_lines = []
        for tool, total, errs, rate in dist_slice:
            dist_lines.append(f"  {tool}: {total} calls, {errs} errors ({rate:.1f}%)")
        if _max_rows > 0 and len(distribution) > _max_rows:
            dist_lines.append(f"  ... and {len(distribution) - _max_rows} more tool(s)")
        dist_text = "\n".join(dist_lines) if dist_lines else "  (no data)"

        error_lines = []
        for tool, msg, cnt in err_slice:
            # Optional: truncate + head/tail sample for long error messages
            display_msg = msg or "(empty)"
            if _max_err > 0:
                display_msg = head_tail(truncate(display_msg, _max_err), 80, 40)
            error_lines.append(f"  {tool}: [{cnt}x] {display_msg}")
        if _max_rows > 0 and len(errors_for_prompt) > _max_rows:
            error_lines.append(f"  ... and {len(errors_for_prompt) - _max_rows} more error pattern(s)")
        error_text = "\n".join(error_lines) if error_lines else "  (no errors)"

        chain_lines = []
        for sid, tool, cnt in chain_slice:
            chain_lines.append(f"  {sid}: {tool} × {cnt}")
        if _max_rows > 0 and len(chains) > _max_rows:
            chain_lines.append(f"  ... and {len(chains) - _max_rows} more retry chain(s)")
        chain_text = "\n".join(chain_lines) if chain_lines else "  (none)"

        tier, _ = classify_ratio(ratio)

        prompt = render_prompt(
            "tool_efficiency",
            command="tools",  # for enforce_budget section priority
            read_edit_ratio=f"{ratio:.1f}",
            ratio_tier=tier,
            total_reads=str(reads),
            total_edits=str(edits),
            tool_distribution=dist_text,
            error_details=error_text,
            retry_chains=chain_text,
            scope=scope,
        )

        if prompt:
            result = invoke(prompt)
            if result:
                print_header("AI Interpretation")
                print(result)
            else:
                print("  (AI analysis unavailable — the model / OpenCode CLI returned no usable response.)")
                print("  Try: --no-ai for data-only output, or change analysis_model / analysis_variant in settings.jsonc.")
        else:
            print("  Prompt template not found — skipping AI interpretation.")
            print("  Create analysis/prompts/tool_efficiency.md to enable this feature.")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze tool usage efficiency from OpenCode database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m analysis.tools                     # full analysis\n"
               "  python -m analysis.tools --no-ai             # data only, no AI\n"
               "  python -m analysis.tools --session ses_xxx   # specific session",
    )
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip AI interpretation (data-only mode)")
    parser.add_argument("--session", metavar="SID", dest="session",
                        help="Analyze a specific session (default: all)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()