# Changelog

All notable changes to Opencode-Helper.

## [Unreleased]

### Added
- **Configurable AI analysis model** (`analysis_model` in `settings.jsonc`). If set, use specified model; if `null`, auto-select free model.
- **Model variant support** (`analysis_variant` in `settings.jsonc`). Passes `--variant` to `opencode run` (e.g. `"low"` reduces reasoning / tool-calling overhead).
- **Automatic model fallback**: if the configured model fails, retries with a free model before giving up.
- **Prompt delivery via `-f` attachment**: full analysis prompt written to a UTF-8 `.md` file and attached, avoiding Windows cmd.exe Unicode truncation issues.
- **New tests** (`tests/test_analysis_common.py`): model selection priority, invoke fallback, variant reading (6 tests).

### Changed
- `analysis/common.py` `invoke()` refactored with `_run_opencode()` / `_extract_response()` helpers.
- Uses `shell=True` file redirection for stdout capture instead of `capture_output=True` (avoids OpenCode server pipe errors on Windows).
- Improved all AI-failure messages to suggest `--no-ai` or adjusting `analysis_model` / `analysis_variant`.

### Fixed
- AI analysis now works reliably on Windows with non-ASCII prompts (Chinese, Japanese, etc.).

## [1.0.1] — 2026-06-02

### Added
- GitHub Actions CI smoke test (Python 3.10–3.13)
- `pyproject.toml` with `osh` console_scripts entry point (`pip install -e .`)
- Unit tests for `utilities.py` pure functions (17 tests, stdlib unittest)
- `CONTRIBUTING.md` and issue templates (bug report, feature request)
- `Limitations` section in README (en + zh)

### Changed
- README now documents known limitations: private SQLite schema, opencode CLI dependency, Windows-only process detection

## [1.0.0] — 2026-06-01

### Initial Release
- Unified CLI (`python main.py`) with 7 subcommands
- Session cleanup: delete old sessions + VACUUM, backup, save list
- Temp file cleanup: purge `%TEMP%\opencode\` with configurable retention
- Analysis suite: harness, tools, mcp, models, skills
- AI-powered interpretation via `opencode run` (auto-selects free model)
- Zero external dependencies (Python stdlib only)
- JSONC configuration (`settings.jsonc`)
- Bilingual documentation (en + zh-CN)
- Safety: dry-run default, process check, automatic backup
