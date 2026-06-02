<div align="center">

# Opencode-Helper

**Figure out what your OpenCode has done. Optimize your configuration.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-success.svg)]()

A unified Python CLI that extends [OpenCode](https://github.com/sst/opencode) beyond its official capabilities — clean up session cruft, audit temp files, and analyze your usage patterns (models, tools, MCP, skills) to find optimization opportunities.

[English](README.md) · [简体中文](README_zh.md)

</div>

---

## Table of Contents

- [Why Opencode-Helper?](#why-opencode-helper)
- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Commands](#commands)
  - [cleanup](#cleanup)
  - [analysis](#analysis)
- [Configuration](#configuration)
- [Safety Model](#safety-model)
- [Limitations](#limitations)
- [Requirements](#requirements)
- [Architecture](#architecture)
- [License](#license)

---

## Why Opencode-Helper?

OpenCode is a fantastic AI coding agent, but it leaves some gaps in daily use:

1. **Session bloat** — sessions accumulate in `~/.local/share/opencode/opencode.db` indefinitely. After weeks of use, the SQLite file can balloon to gigabytes, with no built-in cleanup UI.
2. **Temp file accumulation** — OpenCode creates `%TEMP%\opencode\` on startup but never purges it. AI-generated scripts, cloned repos, and data files pile up forever.
3. **No visibility** — there's no built-in way to see which models you actually use, which tools fail most often, which skills are loaded, or whether bash is breaking on Windows.

**Opencode-Helper fixes all three**, plus adds AI-powered analysis of your usage patterns so you can optimize your configuration.

## Features

### 🧹 Cleanup

| Command | What it does |
|---|---|
| `session` | Delete old sessions from the SQLite database, vacuum disk space, optional backup. Per-session save list to preserve critical work. |
| `tempfile` | Purge `%TEMP%\opencode\` of AI-generated scripts and cloned repos. Configurable retention for loose files vs. project directories. |

### 📊 Analysis

Every analysis command reads the live OpenCode database and produces a report. Most commands also call `opencode run` (using an auto-selected **free model**) to produce an AI-written interpretation.

| Command | What it does |
|---|---|
| `harness` | **Start here.** Overall session review: efficiency, lifecycle, agent switching, archive status. AI-powered optimization suggestions. |
| `tools` | Tool usage efficiency: Read:Edit ratio (target >6.0), error rates, retry-chain detection (3+ consecutive failures). |
| `mcp` | MCP tool call patterns: per-server breakdown, error clustering, AI root-cause diagnosis. |
| `models` | Model usage distribution: calls, cost, tokens. Model switching events. Agent-model cross analysis. |
| `skills` | Skill invocation counts and error rates. Platform compatibility check (flags bash-on-Windows misuse). |

### ✨ Design highlights

- **Zero external dependencies** — pure Python stdlib (sqlite3, json, pathlib, subprocess, …). No `pip install`, no virtualenv required.
- **Dry-run by default** — every destructive command previews what it will do. Add `--execute` to actually run.
- **Bilingual prompts** — AI-generated reports follow your `analysis_language` setting (`en`, `zh-CN`, `ja`, `fr`, etc.).
- **Safe for live OpenCode** — analysis is read-only, safe to run any time. Cleanup refuses to run `--execute` while `opencode.exe` is active.
- **XDG-compliant** — database and storage paths auto-detected via `XDG_DATA_HOME`. Override only when needed.

## Installation

```bash
# Clone the repo
git clone https://github.com/v587d/opencode-helper.git
cd opencode-helper

# Option A: Install globally (recommended)
pip install -e .
och --help              # ready to use from anywhere

# Option B: Run directly (no install needed)
python main.py --help   # must run from project root
```

The CLI is invoked through `och` (if installed) or `python main.py`. For analysis subcommands you also need the [`opencode`](https://github.com/sst/opencode) binary on your `PATH` (it spawns `opencode run` for AI interpretation).

## Quick Start

```bash
# 1. List every command
och --help

# 2. See what OpenCode has been doing
och harness

# 3. Check tool efficiency
och tools

# 4. Find cleanup candidates (dry-run, safe with OpenCode running)
och session

# 5. Actually clean (must exit OpenCode first)
och session --execute

# 6. Purge stale temp files
och tempfile --execute
```

## Commands

### `cleanup`

#### `session` — clean up old sessions

```bash
# Dry-run: show what would be deleted
och session

# Execute: delete + VACUUM
och session --execute

# Keep 14 days instead of 7
och session --execute --days 14

# Skip backup (not recommended)
och session --execute --no-backup

# Preserve specific sessions
och session --add ses_abc123
och session --add ses_abc123 --label "My refactor session"
och session --list
och session --remove ses_abc123
```

- **Default retention**: `session_retention_days` from `settings.jsonc` (7 days).
- **Backup**: timestamped copy created before deletion. Auto-restorable.
- **VACUUM**: runs automatically to reclaim free pages.
- **CASCADE**: deleting a session also cleans up its messages, parts, todos, and share records.
- **Save list**: sessions listed in `settings.jsonc::session_save_list` are never deleted, regardless of age.

#### `tempfile` — purge `%TEMP%\opencode\`

```bash
# Dry-run
och tempfile

# Execute
och tempfile --execute

# Aggressive: 3 days for scripts, 5 for projects
och tempfile --execute --scripts 3 --projects 5

# Quiet mode (no per-file messages)
och tempfile --execute --quiet
```

- **Scope is narrow on purpose** — only `%TEMP%\opencode\`. Will not touch your actual data directory, system temp, or anything else.
- **Two retention buckets**: loose files (scripts, data) vs. project directories (cloned repos, scaffolds).
- **Project detection** via signature files (`.git`, `package.json`, `pyproject.toml`, …).

### `analysis`

#### `harness` — overall session review *(start here)*

```bash
# Last 7 days (default), with AI interpretation
och harness

# Last 30 days
och harness --days 30

# Data only, skip AI
och harness --no-ai
```

Produces: session overview, lifecycle (duration / message count), agent-switching events, archive vs. active counts, efficiency snapshot, **AI optimization suggestions**.

#### `tools` — tool usage efficiency

```bash
# All sessions
och tools

# Single session
och tools --session ses_abc123

# Data only
och tools --no-ai
```

Produces: tool call distribution with error rates, **Read:Edit ratio** (target >6.0), tool error details, retry-chain detection (3+ consecutive same-tool errors).

#### `mcp` — MCP tool analysis

```bash
# All MCP servers
och mcp

# One server only
och mcp --server tavily
och mcp --server websearch
och mcp --server context7

# Data only
och mcp --no-ai
```

Produces: per-tool overview, per-server summary, error breakdown grouped by tool, **AI root-cause diagnosis** of recurring errors.

#### `models` — model usage patterns

```bash
# Top 10 models
och models

# Top 20
och models --limit 20

# Data only
och models --no-ai
```

Produces: model usage distribution (calls, cost, tokens), model switching events, agent-model cross analysis, **AI interpretation**.

#### `skills` — skill usage and platform compatibility

```bash
# All skills
och skills

# Top 10
och skills --limit 10

# Data only
och skills --no-ai
```

Produces: skill invocation counts and error rates, shell tool usage (flags bash-on-Windows), skills referenced in user messages, **AI compatibility diagnosis**.

## Configuration

All knobs live in [`settings.jsonc`](settings.jsonc) (JSON-with-comments format). Defaults are sensible — you usually only need to touch it to extend the **session save list** or change the **analysis language**.

```jsonc
{
    // Session retention in days
    "session_retention_days": 7,

    // Backup and VACUUM behavior
    "session_auto_backup": true,
    "session_auto_vacuum": true,

    // Temp file retention (loose files vs. project dirs)
    "temp_script_retention_days": 1,
    "temp_project_retention_days": 1,

    // Sessions here are NEVER deleted
    "session_save_list": {
        // "ses_abc123": "My refactor session"
    },

    // Override DB path (default: XDG_DATA_HOME/opencode/opencode.db)
    "db_path_override": null,

    // AI analysis output language
    // Supported: "en", "zh-CN", "zh-TW", "ja", "ko", "fr", "de", "es", "pt", "ru"
    // or any plain instruction like "in French"
    "analysis_language": "en"
}
```

Unknown keys are silently ignored, so you can leave comments and dead entries without breaking anything.

## Safety Model

Opencode-Helper is built to be hard to misuse:

| Layer | Protection |
|---|---|
| **Dry-run by default** | Every destructive command (`session`, `tempfile`) previews changes. Add `--execute` to actually mutate state. |
| **Process check** | `session --execute` and `tempfile --execute` refuse to run if `opencode.exe` is still running. The check uses `tasklist` with a PowerShell fallback. |
| **Automatic backup** | `session` creates a timestamped `*.backup_YYYYMMDD_HHMMSS.db` file before any deletion. Skip with `--no-backup` (not recommended). |
| **Save list** | Add session IDs to `session_save_list` to mark them immortal. |
| **Narrow scope** | `tempfile` only touches `%TEMP%\opencode\`. Never your data directory, never system temp root, never anything else. |
| **Read-only analysis** | All `analysis/*` commands are pure read against the database. Safe to run with OpenCode live. |
| **WAL-safe VACUUM** | The cleanup sequence pre-checkpoints and post-checkpoints to keep WAL mode consistent. |

## Limitations

Opencode-Helper works by directly inspecting OpenCode's internals. This brings some inherent limitations you should be aware of:

| Limitation | Impact | Mitigation |
|---|---|---|
| **Private SQLite schema** | `opencode.db` schema is OpenCode's internal contract — not a public API. A future OpenCode update that renames tables, adds columns, or changes data formats **may break this tool** without warning. | Most analysis queries are simple SELECTs on stable tables (`session`, `message`, `part`). Cleanup uses CASCADE deletes which are robust to schema additions. Report breakage via GitHub Issues. |
| **`opencode` CLI dependency** | `analysis/*` subcommands spawn `opencode run` for AI interpretation. If OpenCode is not installed or not on `PATH`, these subcommands will fail. | Use `--no-ai` to get data-only output (no CLI needed). Cleanup subcommands (`session`, `tempfile`) **never** require OpenCode. |
| **Windows-only process detection** | The safety check (`opencode.exe` is running?) uses `tasklist` and PowerShell, which are Windows-specific. On macOS/Linux, this check simply won't fire — you must close OpenCode manually before running `--execute`. | PRs welcome for `pgrep` / `ps`-based detection on Unix. |

## Requirements

- **Python 3.10+** (uses `dict[str, str]` type hints, `list[...]`, `|` union syntax)
- **No Python packages** required — stdlib only
- **`opencode` binary** on `PATH` (only for `analysis/*` subcommands that invoke AI interpretation)
- **SQLite database** at `~/.local/share/opencode/opencode.db` (or override via `settings.jsonc`)

Tested on:
- Windows 11 + Python 3.12
- The code uses `tasklist` and PowerShell for the process check, so non-Windows platforms would need that helper swapped out.

## Architecture

```
Opencode-Helper/
├── main.py                 # Unified CLI dispatcher
├── utilities.py            # Shared: config, logging, DB, paths, process check
├── settings.jsonc          # User-editable configuration
│
├── cleanup/                # Disk-space recovery
│   ├── session.py          # Delete old sessions + VACUUM
│   └── tempfile.py         # Purge %TEMP%\opencode\
│
├── analysis/               # Usage analytics
│   ├── common.py           # Shared: SQL queries, free-model discovery, AI invocation
│   ├── harness.py          # Overall review
│   ├── tools.py            # Tool efficiency
│   ├── mcp.py              # MCP analysis
│   ├── models.py           # Model usage
│   ├── skills.py           # Skill + platform compatibility
│   └── prompts/            # AI prompt templates (per-analysis)
│       ├── harness.md
│       ├── tool_efficiency.md
│       ├── mcp_analysis.md
│       ├── models.md
│       └── skills.md
│
├── README.md               # This file
├── README_zh.md            # 简体中文文档
├── LICENSE                 # MIT
└── .gitignore
```

Each category module exposes a `register_subparser(subparsers)` function. Adding a new category is one import + one function call in `main.py`. See `cleanup/` or `analysis/` for the pattern.

## License

[MIT](LICENSE) — use freely, no warranty.
