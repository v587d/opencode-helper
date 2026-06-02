# AGENTS.md — OpenCode Helper

## Purpose

Personal OpenCode helper project — extending OpenCode beyond official capabilities for better daily usage. Includes cleanup utilities and usage analytics. This is a **Python-only** repository with zero runtime dependencies.

## Repository Structure

```
├── main.py              # Unified CLI entry point (subcommand dispatcher)
├── utilities.py         # Shared utilities (config, logging, DB, paths)
├── pyproject.toml       # Build config + console_scripts (osh)
├── settings.jsonc       # Configuration (JSONC format with comments)
├── tests/               # Unit tests (stdlib unittest)
│   └── test_utilities.py
├── cleanup/
│   ├── __init__.py
│   ├── session.py       # Session cleanup (SQLite database)
│   └── tempfile.py      # Temp file cleanup (%TEMP%\opencode\)
├── analysis/
│   ├── __init__.py
│   ├── common.py        # SQL helpers, free-model discovery, AI invocation
│   ├── harness.py       # Overall session review
│   ├── tools.py         # Tool usage efficiency
│   ├── mcp.py           # MCP tool call analysis
│   ├── models.py        # Model usage patterns
│   ├── skills.py        # Skill usage + platform compat
│   └── prompts/         # AI prompt templates (per-analysis .md)
├── .github/             # CI + issue templates
│   ├── workflows/smoke.yml
│   └── ISSUE_TEMPLATE/
└── .sisyphus/           # OpenCode internal state (ignore)
```

## Critical Commands

### Via `osh` (recommended, after `pip install -e .`)
```bash
osh --help
osh session              # dry-run session cleanup
osh session --execute    # delete old sessions
osh tempfile --execute   # delete temp files
osh harness              # overall session review
osh tools                # tool efficiency analysis
osh mcp                  # MCP analysis
osh models               # model usage patterns
osh skills               # skill usage + platform compat
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
- Validates `--help` for all 7 subcommands
- Validates all module imports
- Matrix: Python 3.10, 3.11, 3.12, 3.13

## Architecture & Key Facts

### Database Location
- **Default**: `~/.local/share/opencode/opencode.db` (XDG_DATA_HOME)
- **Override**: Set `db_path_override` in `settings.jsonc`
- **Important**: Database uses **WAL mode** — always check if OpenCode is running before cleanup

### Safety Mechanism
Session cleanup exits if `opencode.exe` is running during `--execute` (write operations).
Dry-run mode is read-only and safe to run at any time.
- Checks via `tasklist` (Windows) or PowerShell fallback
- Error message shows how to verify: `tasklist | findstr opencode`
- Force kill: `taskkill /f /im opencode.exe`

### Configuration (`settings.jsonc`)
- Uses **JSONC format** (JSON with `//` line comments)
- Session retention: `session_retention_days` (default: 7)
- Temp file retention: `temp_script_retention_days` (default: 1)
- Save-list: `session_save_list` — sessions here are NEVER deleted
- Analysis language: `analysis_language` (en, zh-CN, ja, etc.)

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

## Development Notes

### Running Scripts
- Must run from repository root or use `osh` command (if installed)
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
- Tests cover: `format_size`, `format_time`, JSONC parsing
- Add new tests in `tests/` following the existing pattern

## Common Pitfalls

1. **OpenCode running**: Always check first — scripts exit if opencode.exe is active during `--execute` (write operations). Dry-run is always safe.
2. **Wrong directory**: Temp cleanup only affects `%TEMP%\opencode\`, not data directory
3. **Dry-run by default**: Must pass `--execute` to actually delete anything
4. **WAL mode**: Database may have WAL file — VACUUM handles checkpointing safely
5. **Save-list**: Add critical session IDs to `session_save_list` in settings.jsonc to prevent deletion
6. **Private schema**: `opencode.db` schema may change on OpenCode upgrade — report breakage via GitHub Issues
