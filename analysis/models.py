#!/usr/bin/env python3
"""
Model Usage Analysis Module
===========================

Analyzes model usage patterns from OpenCode's SQLite database:
- Model usage distribution (calls, cost, tokens)
- Model switching events
- Agent-model cross analysis
- Optional AI interpretation via OpenCode

Can run standalone or be imported:
    python -m analysis.models [args]
"""

import argparse
import sys

from utilities import print_header, print_separator, setup_logging
from analysis.common import query, invoke, render_prompt

log = setup_logging("models")


# ═══════════════════════════════════════════════════════════════════
#  Subcommand registration
# ═══════════════════════════════════════════════════════════════════

def register_subparser(subparsers):
    """Register models subcommand for unified CLI (main.py)."""
    p = subparsers.add_parser(
        "models",
        help="Analyze model usage patterns from OpenCode database",
    )
    p.add_argument("--no-ai", action="store_true",
                   help="Skip AI analysis (data only)")
    p.add_argument("--limit", type=int, default=10,
                   help="Max rows per section (default: 10)")
    p.set_defaults(func=run)


# ═══════════════════════════════════════════════════════════════════
#  Data queries
# ═══════════════════════════════════════════════════════════════════

SQL_MODEL_USAGE = """
SELECT json_extract(m.data, '$.modelID') as model,
       COUNT(*) as calls,
       ROUND(SUM(json_extract(m.data, '$.cost')), 4) as total_cost,
       SUM(json_extract(m.data, '$.tokens.input')) as total_input,
       SUM(json_extract(m.data, '$.tokens.output')) as total_output
FROM message m
WHERE json_extract(m.data, '$.role') = 'assistant'
GROUP BY model
ORDER BY calls DESC
"""

SQL_MODEL_SWITCHES = """
SELECT sm.session_id, sm.type,
       json_extract(sm.data, '$.model.id') as from_model,
       json_extract(sm.data, '$.model') as model_info,
       sm.time_created
FROM session_message sm
WHERE sm.type = 'model-switched'
ORDER BY sm.time_created DESC
"""

SQL_AGENT_MODEL = """
SELECT json_extract(m.data, '$.agent') as agent,
       json_extract(m.data, '$.modelID') as model,
       COUNT(*) as calls
FROM message m
WHERE json_extract(m.data, '$.role') = 'assistant'
  AND json_extract(m.data, '$.agent') IS NOT NULL
GROUP BY agent, model
ORDER BY calls DESC
"""


# ═══════════════════════════════════════════════════════════════════
#  Display helpers
# ═══════════════════════════════════════════════════════════════════

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


def _fmt_ts(ms) -> str:
    """Format millisecond timestamp to local datetime."""
    if ms is None:
        return "—"
    from datetime import datetime
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "—"


# ═══════════════════════════════════════════════════════════════════
#  Analysis sections
# ═══════════════════════════════════════════════════════════════════

def show_model_usage(limit: int) -> list:
    """Display model usage distribution and return rows for AI prompt."""
    rows = query(SQL_MODEL_USAGE)
    if not rows:
        print("  No model usage data found.")
        return []

    print_header("Model Usage Distribution")
    print(f"  {'Model':<40} {'Calls':>7} {'Cost':>10} {'Input':>10} {'Output':>10}")
    print_separator()

    total_calls = 0
    total_cost = 0.0
    total_input = 0
    total_output = 0

    for row in rows[:limit]:
        model, calls, cost, inp, out = row
        print(f"  {model:<40} {calls:>7} {_fmt_cost(cost):>10} "
              f"{_fmt_tokens(inp):>10} {_fmt_tokens(out):>10}")
        total_calls += calls or 0
        total_cost += cost or 0
        total_input += inp or 0
        total_output += out or 0

    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more model(s)")

    print_separator()
    print(f"  {'TOTAL':<40} {total_calls:>7} {_fmt_cost(total_cost):>10} "
          f"{_fmt_tokens(total_input):>10} {_fmt_tokens(total_output):>10}")

    return rows


def show_model_switches(limit: int) -> list:
    """Display model switching events and return rows for AI prompt."""
    rows = query(SQL_MODEL_SWITCHES)
    if not rows:
        print("\n  No model switching events found.")
        return []

    print_header("Model Switching Events")
    print(f"  {'Session':<36} {'From Model':<36} {'Time':<18}")
    print_separator()

    for row in rows[:limit]:
        session_id, evt_type, from_model, model_info, ts = row
        from_display = from_model or "(unknown)"
        print(f"  {session_id:<36} {from_display:<36} {_fmt_ts(ts):<18}")

    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more event(s)")

    return rows


def show_agent_model(limit: int) -> list:
    """Display agent-model cross analysis and return rows for AI prompt."""
    rows = query(SQL_AGENT_MODEL)
    if not rows:
        print("\n  No agent-model data found.")
        return []

    print_header("Agent-Model Cross Analysis")
    print(f"  {'Agent':<30} {'Model':<40} {'Calls':>7}")
    print_separator()

    for row in rows[:limit]:
        agent, model, calls = row
        print(f"  {agent:<30} {model:<40} {calls:>7}")

    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more combination(s)")

    return rows


# ═══════════════════════════════════════════════════════════════════
#  AI interpretation
# ═══════════════════════════════════════════════════════════════════

def run_ai_analysis(usage_rows, switch_rows, agent_rows, limit: int):
    """Render prompt and invoke AI for interpretation."""
    # Build data summary for the prompt
    sections = []

    if usage_rows:
        lines = ["Model | Calls | Cost | Input Tokens | Output Tokens"]
        lines.append("-" * 70)
        for row in usage_rows[:limit]:
            model, calls, cost, inp, out = row
            lines.append(
                f"{model} | {calls} | {cost:.4f} | {inp or 0} | {out or 0}"
            )
        sections.append("## Model Usage Distribution\n" + "\n".join(lines))

    if switch_rows:
        lines = ["Session | From Model | Time"]
        lines.append("-" * 70)
        for row in switch_rows[:limit]:
            sid, _, from_model, _, ts = row
            lines.append(f"{sid} | {from_model or '?'} | {_fmt_ts(ts)}")
        sections.append("## Model Switching Events\n" + "\n".join(lines))

    if agent_rows:
        lines = ["Agent | Model | Calls"]
        lines.append("-" * 70)
        for row in agent_rows[:limit]:
            agent, model, calls = row
            lines.append(f"{agent} | {model} | {calls}")
        sections.append("## Agent-Model Cross Analysis\n" + "\n".join(lines))

    data_block = "\n\n".join(sections)

    # Try template first, fall back to inline prompt
    prompt = render_prompt("models", data=data_block)
    if not prompt:
        prompt = (
            "Analyze the following OpenCode model usage data. "
            "Identify patterns, anomalies, and optimization opportunities.\n\n"
            f"{data_block}"
        )

    print_header("AI Analysis")
    result = invoke(prompt)
    if result:
        print(result)
    else:
        print("  (AI analysis unavailable — no response received)")
        log.warning("AI invocation returned empty result.")


# ═══════════════════════════════════════════════════════════════════
#  Main entry point
# ═══════════════════════════════════════════════════════════════════

def run(args: argparse.Namespace):
    """Run model usage analysis."""
    limit = args.limit

    usage_rows = show_model_usage(limit)
    switch_rows = show_model_switches(limit)
    agent_rows = show_agent_model(limit)

    if not args.no_ai:
        run_ai_analysis(usage_rows, switch_rows, agent_rows, limit)
    else:
        print("\n  (--no-ai: skipping AI interpretation)")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze model usage patterns from OpenCode database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m analysis.models                  # full analysis\n"
               "  python -m analysis.models --no-ai           # data only\n"
               "  python -m analysis.models --limit 5         # top 5 per section",
    )
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip AI analysis (data only)")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max rows per section (default: 10)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()