#!/usr/bin/env python3
"""
Skill Usage Analysis Module
============================

Analyzes skill invocation patterns from OpenCode's SQLite database:
- Which skills were loaded (extracted from user message system prompts)
- Skill invocation counts and error rates
- Platform compatibility issues (bash vs PowerShell on Windows)
- Optional AI diagnosis of compatibility problems

Can run standalone or be imported:
    python -m analysis.skills [args]
"""

import argparse
import json
import platform
import sys

from utilities import print_header, print_separator, setup_logging
from analysis.common import query, invoke, render_prompt

log = setup_logging("skills")


# ═════════════════════════════════════════════════════════════════════
#  Subcommand registration
# ═════════════════════════════════════════════════════════════════════

def register_subparser(subparsers):
    """Register skills subcommand for unified CLI (main.py)."""
    p = subparsers.add_parser(
        "skills",
        help="Analyze skill usage and platform compatibility from OpenCode database",
    )
    p.add_argument("--no-ai", action="store_true",
                   help="Skip AI diagnosis (data only)")
    p.add_argument("--limit", type=int, default=20,
                   help="Max rows per section (default: 20)")
    p.set_defaults(func=run)


# ═════════════════════════════════════════════════════════════════════
#  Data queries
# ═════════════════════════════════════════════════════════════════════

SQL_SKILL_INVOCATIONS = """
SELECT json_extract(p.data, '$.state.input.name') as skill_name,
       COUNT(*) as calls,
       SUM(CASE WHEN json_extract(p.data, '$.state.status') = 'error' THEN 1 ELSE 0 END) as errors
FROM part p
WHERE json_extract(p.data, '$.type') = 'tool'
  AND json_extract(p.data, '$.tool') = 'skill'
GROUP BY skill_name
ORDER BY calls DESC
"""

SQL_SHELL_USAGE = """
SELECT json_extract(p.data, '$.tool') as tool,
       COUNT(*) as calls
FROM part p
WHERE json_extract(p.data, '$.type') = 'tool'
  AND json_extract(p.data, '$.tool') IN ('bash', 'powershell', 'cmd')
GROUP BY tool
"""

SQL_SKILLS_IN_PROMPTS = """
SELECT m.session_id, m.data
FROM message m
WHERE json_extract(m.data, '$.role') = 'user'
  AND m.data LIKE '%skill%'
LIMIT 5
"""


# ═════════════════════════════════════════════════════════════════════
#  Display helpers
# ═════════════════════════════════════════════════════════════════════

def _extract_skill_names_from_messages(rows: list) -> set:
    """Extract unique skill names from user message data containing skill references.

    Parses the JSON data field looking for skill name references in
    system prompt content or tool invocations.
    """
    names = set()
    for row in rows:
        try:
            data = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        except (json.JSONDecodeError, TypeError):
            continue

        # Walk the data recursively for skill name patterns
        _collect_skill_names(data, names)
    return names


def _collect_skill_names(obj, names: set, depth: int = 0):
    """Recursively collect skill names from nested JSON structures."""
    if depth > 10:
        return
    if isinstance(obj, str):
        # Match skill name patterns like /command-name or skill references
        if "skill" in obj.lower() and "/" in obj:
            # Extract skill-like tokens (e.g. "/equity-investment-thesis")
            import re
            for m in re.finditer(r'/([a-z][\w-]+)', obj):
                candidate = m.group(1)
                if any(kw in candidate.lower() for kw in
                       ("skill", "thesis", "analysis", "finder", "builder",
                        "decoder", "review", "snapshot", "radar", "ladder",
                        "discipline", "shift", "debrief", "expert", "sizer")):
                    names.add(candidate)
    elif isinstance(obj, dict):
        # Check for explicit skill name fields
        if "name" in obj and isinstance(obj["name"], str):
            val = obj["name"]
            if val and not val.startswith("$"):
                names.add(val)
        for v in obj.values():
            _collect_skill_names(v, names, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_skill_names(item, names, depth + 1)


# ═════════════════════════════════════════════════════════════════════
#  Analysis sections
# ═════════════════════════════════════════════════════════════════════

def show_skill_invocations(limit: int) -> list:
    """Display skill invocation counts and error rates. Returns raw rows."""
    rows = query(SQL_SKILL_INVOCATIONS)
    if not rows:
        print("  No skill invocations found in database.")
        return []

    print_header("Skill Invocation Summary")
    print(f"  {'Skill Name':<45} {'Calls':>7} {'Errors':>7} {'Err%':>7}")
    print_separator()

    total_calls = 0
    total_errors = 0

    for row in rows[:limit]:
        name, calls, errors = row
        name = name or "(unknown)"
        calls = calls or 0
        errors = errors or 0
        err_pct = f"{errors / calls * 100:.1f}%" if calls else "—"
        print(f"  {name:<45} {calls:>7} {errors:>7} {err_pct:>7}")
        total_calls += calls
        total_errors += errors

    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more skill(s)")

    print_separator()
    total_err_pct = f"{total_errors / total_calls * 100:.1f}%" if total_calls else "—"
    print(f"  {'TOTAL':<45} {total_calls:>7} {total_errors:>7} {total_err_pct:>7}")

    return rows


def show_shell_usage(limit: int) -> list:
    """Display shell/tool usage for platform compatibility analysis. Returns raw rows."""
    rows = query(SQL_SHELL_USAGE)
    if not rows:
        print("\n  No shell tool usage found in database.")
        return []

    is_windows = platform.system() == "Windows"

    print_header("Shell / Platform Tool Usage")
    print(f"  {'Tool':<20} {'Calls':>7}")
    print_separator()

    total_calls = 0
    bash_calls = 0
    for row in rows:
        tool, calls = row
        print(f"  {tool:<20} {calls:>7}")
        total_calls += calls
        if tool == "bash":
            bash_calls = calls

    print_separator()
    print(f"  {'TOTAL':<20} {total_calls:>7}")

    # Platform compatibility warning
    if is_windows and bash_calls > 0:
        pct = bash_calls / total_calls * 100 if total_calls else 0
        print()
        print(f"  [WARN] Detected {bash_calls} bash calls ({pct:.0f}% of shell usage) on Windows.")
        print(f"    Consider using PowerShell or cmd for better compatibility.")
        print(f"    Bash on Windows (Git Bash / WSL) may cause path and encoding issues.")

    return rows


def show_skills_in_prompts(limit: int) -> tuple[list, set]:
    """Display skills found in user message system prompts. Returns (rows, skill_names)."""
    rows = query(SQL_SKILLS_IN_PROMPTS)
    if not rows:
        print("\n  No skill references found in user messages.")
        return [], set()

    print_header("Skills Referenced in User Messages")
    skill_names = _extract_skill_names_from_messages(rows)

    if skill_names:
        print(f"  Found {len(skill_names)} unique skill reference(s):")
        for name in sorted(skill_names):
            print(f"    - {name}")
    else:
        print("  No specific skill names could be extracted from message content.")

    print(f"\n  (Scanned {len(rows)} user message(s) containing 'skill')")

    return rows, skill_names


# ═════════════════════════════════════════════════════════════════════
#  AI interpretation
# ═════════════════════════════════════════════════════════════════════

def run_ai_analysis(invocation_rows, shell_rows, prompt_rows, skill_names, limit: int):
    """Render prompt and invoke AI for compatibility diagnosis."""
    sections = []

    # Skill invocations
    if invocation_rows:
        lines = ["Skill Name | Calls | Errors"]
        lines.append("-" * 60)
        for row in invocation_rows[:limit]:
            name, calls, errors = row
            name = name or "(unknown)"
            lines.append(f"{name} | {calls} | {errors or 0}")
        sections.append("## Skill Invocations\n" + "\n".join(lines))

    # Shell usage
    if shell_rows:
        lines = ["Tool | Calls"]
        lines.append("-" * 30)
        for row in shell_rows:
            tool, calls = row
            lines.append(f"{tool} | {calls}")
        is_windows = platform.system() == "Windows"
        lines.append(f"\nPlatform: {platform.system()} ({platform.platform()})")
        if is_windows:
            bash_count = sum(r[1] for r in shell_rows if r[0] == "bash")
            lines.append(f"WARNING: {bash_count} bash invocations on Windows")
        sections.append("## Shell / Platform Usage\n" + "\n".join(lines))

    # Skills in prompts
    if skill_names:
        lines = sorted(skill_names)
        sections.append("## Skills Referenced in Prompts\n" + "\n".join(lines))

    data_block = "\n\n".join(sections)

    # Try template first, fall back to inline prompt
    prompt = render_prompt("skills", data=data_block)
    if not prompt:
        prompt = (
            "Analyze the following OpenCode skill usage and platform compatibility data. "
            "Identify potential issues, especially around bash/PowerShell usage on Windows, "
            "skill error patterns, and optimization opportunities.\n\n"
            f"{data_block}"
        )

    print_header("AI Compatibility Diagnosis")
    result = invoke(prompt)
    if result:
        print(result)
    else:
        print("  (AI analysis unavailable — no response received)")
        log.warning("AI invocation returned empty result.")


# ═════════════════════════════════════════════════════════════════════
#  Main entry point
# ═════════════════════════════════════════════════════════════════════

def run(args: argparse.Namespace):
    """Run skill usage analysis."""
    limit = args.limit

    invocation_rows = show_skill_invocations(limit)
    shell_rows = show_shell_usage(limit)
    prompt_rows, skill_names = show_skills_in_prompts(limit)

    if not args.no_ai:
        run_ai_analysis(invocation_rows, shell_rows, prompt_rows, skill_names, limit)
    else:
        print("\n  (--no-ai: skipping AI diagnosis)")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze skill usage and platform compatibility from OpenCode database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python -m analysis.skills                  # full analysis\n"
               "  python -m analysis.skills --no-ai           # data only\n"
               "  python -m analysis.skills --limit 10       # top 10 per section",
    )
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip AI diagnosis (data only)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max rows per section (default: 20)")
    run(parser.parse_args())


if __name__ == "__main__":
    main()