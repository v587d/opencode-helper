# Changelog

All notable changes to Opencode-Helper.

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
