# Contributing to Opencode-Helper

> This is an independent, community-maintained project — not affiliated with or endorsed by the [OpenCode](https://github.com/sst/opencode) team.

Thanks for considering a contribution! This is a personal project, but PRs and issues are welcome.

## Getting Started

```bash
git clone https://github.com/v587d/opencode-helper.git
cd opencode-helper
pip install -e .             # installs 'osh' CLI globally

# Run tests
python -m unittest discover tests -v

# Verify CLI works
osh --help
```

## Project Philosophy

- **Zero runtime dependencies**. Python stdlib only. Tests use `unittest` (also stdlib).
- **Dry-run by default**. Every destructive command must preview before executing.
- **Safety first**. Write operations refuse to run if OpenCode is active.

## How to Contribute

1. **Fork** the repo and create a branch.
2. **Write tests** for any new functionality. Run `python -m unittest discover tests -v`.
3. **Match existing style**: docstrings, type hints, `register_subparser` pattern.
4. **Open a PR** with a clear description of what changed and why.

## Adding a New Subcommand

Each subcommand module must expose:

```python
def register_subparser(subparsers):
    p = subparsers.add_parser("name", help="...")
    p.add_argument(...)
    p.set_defaults(func=run)
```

Then register it in `main.py` — one import, one function call at the bottom.

## Commit Style

Use conventional commit prefixes:
- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation
- `test:` — tests
- `ci:` — CI / workflows
- `refactor:` — code changes without feature/fix

## Questions?

Open an issue on GitHub.
