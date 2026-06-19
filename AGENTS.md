# AGENTS.md — OpenCode Helper

## Purpose

Personal OpenCode helper project — extending OpenCode beyond official capabilities for better daily usage. Includes cleanup utilities and usage analytics. This is a **Python-only** repository with zero runtime dependencies.

## Repository Structure

```
├── main.py              # Unified CLI entry point (subcommand dispatcher)
├── utilities.py         # Shared utilities (config, logging, DB, paths)
├── pyproject.toml       # Build config + console_scripts (och)
├── settings.jsonc       # Configuration (JSONC format with comments)
├── tests/               # Unit tests (stdlib unittest, zero deps)
│   ├── test_utilities.py        # format_size, format_time, JSONC parsing
│   ├── test_analysis_common.py  # model selection, invoke fallback, variant
│   └── test_compression.py      # truncate, head_tail, dedup_errors, budget
├── cleanup/
│   ├── __init__.py
│   ├── session.py       # Session cleanup (SQLite database)
│   └── tempfile.py      # Temp file cleanup (%TEMP%\opencode\)
├── analysis/
│   ├── __init__.py
│   ├── common.py        # SQL helpers, free-model discovery, AI invocation, compression
│   ├── harness.py       # Overall session review
│   ├── tools.py         # Tool usage efficiency
│   ├── mcp.py           # MCP tool call analysis
│   ├── models.py        # Model usage patterns
│   ├── skills.py        # Skill usage + platform compat
│   └── prompts/         # AI prompt templates (per-analysis .md)
├── drilldown/           # Single-session agent call topology visualization
│   ├── cli.py           # Subcommand registration + run()
│   ├── graph.py         # Build SessionGraph from DB (AgentStep → ToolCall, SpawnGroup)
│   ├── render.py        # HTML (embedded SVG/CSS/JS) + ANSI terminal tree renderers
│   └── storage.py       # Output file naming and storage under drilldown@och
├── .github/             # CI + issue templates
│   ├── workflows/smoke.yml
│   └── ISSUE_TEMPLATE/
├── assets/              # Screenshots for README documentation
└── .sisyphus/           # OpenCode internal state (ignore)
```

## Critical Commands

### Via `och` (recommended, after `pip install -e .`)
```bash
och --help
och session              # dry-run session cleanup
och session --execute    # delete old sessions
och tempfile --execute   # delete temp files
och harness              # overall session review
och tools                # tool efficiency analysis
och mcp                  # MCP analysis
och models               # model usage patterns
och skills               # skill usage + platform compat
och drilldown            # visualize latest session (HTML, opens browser)
och drilldown --text     # terminal tree view instead of HTML
och drilldown --list     # list recent sessions available for drilldown
```

### Via `python main.py` (no install needed)
```bash
python main.py --help
python main.py session             # dry-run
python main.py session --execute   # delete old sessions
```

Each module can also run standalone (backward compatible):
```bash
python -m cleanup.session --execute
python -m cleanup.tempfile --execute
```

### Running Tests
```bash
# All tests (zero dependencies)
python -m unittest discover tests -v

# Single test file
python -m unittest tests.test_utilities -v
```

## CI

GitHub Actions smoke test (`.github/workflows/smoke.yml`) runs on push/PR:
- Validates `--help` for 7 of 8 subcommands (`session`, `tempfile`, `harness`, `tools`, `mcp`, `models`, `skills`)
- **`drilldown` is NOT in CI yet** — its `--help` is not validated. If you change drilldown, run `python main.py drilldown --help` locally.
- Validates module imports (utilities, cleanup.*, analysis.* — drilldown not imported in CI)
- Matrix: Python 3.10, 3.11, 3.12, 3.13

## Architecture & Key Facts

### Database Location
- **Default**: `~/.local/share/opencode/opencode.db` (XDG_DATA_HOME)
- **Override**: Set `db_path_override` in `settings.jsonc`
- **Important**: Database uses **WAL mode** — always check if OpenCode is running before cleanup

### Safety Mechanism
Session cleanup (`session --execute`) exits if `opencode.exe` is running during write operations. Dry-run mode is read-only and safe to run at any time.
- Checks via `tasklist` (Windows) or PowerShell fallback
- Error message shows how to verify: `tasklist | findstr opencode`
- Force kill: `taskkill /f /im opencode.exe`

**`tempfile` is stricter**: it calls `ensure_opencode_stopped()` unconditionally at the start of `run()` — so `och tempfile` (even dry-run) **exits if opencode.exe is running**. Close OpenCode before any tempfile invocation. (Save-list management via `session --add/--remove/--list` does NOT require opencode stopped — those return before the gate.)

### Configuration (`settings.jsonc`)
- Uses **JSONC format** (JSON with `//` line comments)
- Session retention: `session_retention_days` (default: 7)
- Temp file retention: `temp_script_retention_days` + `temp_project_retention_days` (default: 1 each — two buckets: loose files vs. project dirs)
- Save-list: `session_save_list` — sessions here are NEVER deleted (accepts dict or list form)
- Analysis language: `analysis_language` (en, zh-CN, ja, fr, etc., or any plain instruction like "in French")
- Analysis model: `analysis_model` (null = auto-select free model; set to `provider/model` to pin)
- Analysis variant: `analysis_variant` (null, or "low"/"medium"/"high" reasoning effort — "low" reduces tool-calling overhead)
- **Compression knobs (all opt-in, default 0 = OFF)**: `analysis_max_error_chars`, `analysis_max_total_chars`, `analysis_max_rows_per_section`, `analysis_error_dedup_prefix`, `analysis_error_dedup_top_k`. Set to positive values to cap prompt size / dedup similar errors.

### Cleanup Behavior
1. **Session cleanup**: Deletes from SQLite DB + storage JSON files, then VACUUM
2. **Temp cleanup**: Only touches `%TEMP%\opencode\` — never touches `~/.local/share/opencode/`
3. **Backup**: Automatic timestamped backup before deletion (can skip with `--no-backup`)
4. **Dry-run**: Always preview first — add `--execute` to apply changes

### Analysis Behavior
1. **Read-only**: All analysis commands are pure SELECT — safe with OpenCode running
2. **AI interpretation**: `analysis/*` spawns `opencode run` with auto-selected free model
3. **Data-only mode**: `--no-ai` skips AI, prints raw data tables
4. **Prompts**: Templates in `analysis/prompts/` — Markdown with `{variable}` substitution
5. **Model selection**: `analysis_model` setting → auto-discovered free model → hardcoded fallback (`opencode/mimo-v2.5-free`). Free-model discovery runs `opencode models <provider> --verbose` and filters cost.input==0 AND cost.output==0 AND toolcall capability (default providers: `opencode`, `opencode-go`; sorted by context window descending).
6. **Auto-fallback**: If the configured `analysis_model` fails (nonzero exit / empty stdout), `invoke()` retries once with a free model — but only when the caller did NOT explicitly pass a model.
7. **Windows AI-invocation quirk**: `_run_opencode` uses **file redirection** (`>out 2>err`), NOT `capture_output=True` (PIPE), because PIPE triggers server errors in opencode's internal server on Windows. The full prompt is written to a temp `.md` file and passed via `-f` (attachment) to avoid shell encoding issues with long Unicode prompts. Inline message is a short ASCII instruction. Uses `--dangerously-skip-permissions` and `--format json`; collects `text` events from JSON Lines output.
8. **Prompt budget**: When `analysis_max_total_chars > 0`, `enforce_budget()` drops lowest-priority `## ` sections first (per-subcommand priority lists in `analysis/common.py::_SECTION_PRIORITY`); the intro section is never dropped. Hard-truncates as a last resort.

### Drilldown Behavior
1. **Read-only**: pure SELECT against `message` and `part` tables — safe with OpenCode running
2. **Data model**: `SessionGraph` → `AgentStep` (one assistant message) → `ToolCall` (one tool-type part). Parallel detection via `SpawnGroup` (messages sharing the same `parentID` that ran concurrently; groups of size ≥2 only).
3. **Sub-agent recursion**: when querying a root session (parent_id IS NULL), `build_graph()` recursively includes all child sub-agent sessions via `session.parent_id`. Each `AgentStep` is annotated with `is_subagent`, `subagent_depth`, and `session_id`. Use `--no-recurse` to disable and show only the root session's steps.
4. **Two renderers**: HTML (default, self-contained with embedded SVG/CSS/JS, dark theme, pan/zoom/click-to-focus/hover tooltips, opens browser) and `--text` (ANSI terminal tree). Sub-agent steps show `[dN]` depth badges and session IDs. Zero external deps.
5. **Self-contained**: duplicates some query logic from `analysis/common.py` (intentionally) to keep drilldown importable standalone.

## Development Notes

### Running Scripts
- Must run from repository root or use `och` command (if installed)
- Scripts handle `sys.path` insertion for relative imports
- No external dependencies — stdlib only (sqlite3, json, pathlib, etc.)

### Shared Utilities
- `utilities.py` at project root contains all shared code (config, logging, DB, paths)
- All modules import from `utilities` directly: `from utilities import ...`

### Adding New Categories
Each category module must expose a `register_subparser(subparsers)` function:
```python
def register_subparser(subparsers):
    p = subparsers.add_parser("name", help="...")
    p.add_argument(...)
    p.set_defaults(func=run)  # route to module's run(args)
```
Then register it in `main.py` — one import, one function call. No other changes needed.

### Configuration Parsing
- Settings loaded at module import time via `_load_settings()`
- JSONC parsing strips `//` and `/* */` comments before `json.loads()`
- Unknown keys in settings.jsonc are silently ignored

### Database Schema
- Tables: `session`, `message`, `part`, `event_sequence`, `event`
- CASCADE delete: sessions → messages, parts, todos, session_share
- Orphan cleanup: event_sequence and event records without sessions

### Testing
- `unittest` from stdlib — zero test dependencies
- Three test files:
  - `test_utilities.py` — `format_size`, `format_time`, JSONC comment stripping
  - `test_analysis_common.py` — `pick_analysis_model` priority, `invoke()` fallback behavior, `--variant` passing
  - `test_compression.py` — `truncate`, `head_tail`, `dedup_errors`, `enforce_budget`, `render_prompt` budget integration, opt-in defaults
- Tests use `@patch` on `get_settings` to override knobs without mutating on-disk state
- Add new tests in `tests/` following the existing pattern (sys.path insertion + unittest)

## Common Pitfalls

1. **OpenCode running**: Always check first. `session --execute` and `tempfile` (even dry-run) exit if opencode.exe is active. `session` dry-run and all `analysis/*` / `drilldown` commands are safe anytime.
2. **Wrong directory**: Temp cleanup only affects `%TEMP%\opencode\`, not data directory
3. **Dry-run by default**: Must pass `--execute` to actually delete anything (except tempfile, which gates even dry-run on opencode being stopped)
4. **WAL mode**: Database may have WAL file — VACUUM handles checkpointing safely
5. **Save-list**: Add critical session IDs to `session_save_list` in settings.jsonc to prevent deletion
6. **Private schema**: `opencode.db` schema may change on OpenCode upgrade — report breakage via GitHub Issues
7. **drilldown not in CI**: If you modify drilldown, run `python main.py drilldown --help` and `python -m unittest discover tests -v` locally — CI won't catch drilldown breakage.
