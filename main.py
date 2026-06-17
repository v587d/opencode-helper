#!/usr/bin/env python3
"""
OpenCode Helper — unified CLI entry point.

Usage:
    python main.py                    # show help
    python main.py session --help     # session subcommand help
    python main.py session --execute  # run session cleanup
    python main.py tempfile           # dry-run tempfile cleanup

Each module can still be run standalone:
    python -m cleanup.session --execute
    python -m cleanup.tempfile --execute
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cleanup.session import register_subparser as register_session
from cleanup.tempfile import register_subparser as register_tempfile
from analysis.mcp import register_subparser as register_mcp
from analysis.models import register_subparser as register_models
from analysis.skills import register_subparser as register_skills
from analysis.harness import register_subparser as register_harness
from analysis.tools import register_subparser as register_tools


def main():
    parser = argparse.ArgumentParser(
        prog="och",
        description="OpenCode Helper — third-party companion tool for OpenCode (not affiliated with the OpenCode team)",
    )
    subparsers = parser.add_subparsers(
        title="commands",
        dest="command",
        description="Available tools:",
    )

    register_session(subparsers)
    register_tempfile(subparsers)
    register_mcp(subparsers)
    register_models(subparsers)
    register_skills(subparsers)
    register_harness(subparsers)
    register_tools(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()