"""CLI entry point for drilldown subcommand."""

import sys
from pathlib import Path

from utilities import print_header, setup_logging

log = setup_logging("drilldown")


def register_subparser(subparsers):
    """Register drilldown subcommand for unified CLI (main.py)."""
    p = subparsers.add_parser(
        "drilldown",
        help="Single-session agent call topology visualization",
    )
    p.add_argument(
        "--session",
        help="Session ID to visualize (default: latest)",
    )
    p.add_argument(
        "--text",
        action="store_true",
        help="Terminal tree view instead of HTML",
    )
    p.add_argument(
        "-o", "--output",
        default=None,
        help="Output HTML file path (default: auto-save to drilldown@och storage)",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="Don't open browser after generation",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List recent sessions available for drilldown",
    )
    p.add_argument(
        "--no-recurse",
        action="store_true",
        help="Don't drill into child sub-agent sessions (default: recurse for root sessions)",
    )
    p.set_defaults(func=run)


def run(args):
    """Execute drilldown subcommand."""
    if args.list:
        _list_sessions()
        return

    from drilldown.graph import build_graph, get_latest_session_id

    session_id = args.session or get_latest_session_id()
    if not session_id:
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    graph = build_graph(session_id, recurse=not args.no_recurse)
    if graph is None:
        print(f"Session {session_id} not found.", file=sys.stderr)
        sys.exit(1)

    if args.text:
        from drilldown.render import render_terminal_tree
        print(render_terminal_tree(graph))
    else:
        from drilldown.render import render_html
        from drilldown.storage import get_output_path
        output_path = args.output or str(get_output_path(session_id, graph.title))
        render_html(graph, output_path)
        print(f"Generated: {output_path}")
        if not args.no_open:
            import webbrowser
            webbrowser.open(f"file://{Path(output_path).resolve()}")


def _list_sessions():
    """Print recent sessions available for drilldown."""
    from drilldown.graph import list_sessions

    sessions = list_sessions()
    if not sessions:
        print("No sessions found.")
        return

    print_header("Recent Sessions")
    id_w = max(len(s["id"]) for s in sessions)
    id_w = max(id_w, 10)

    print(f"  {'ID':<{id_w}}  {'Parts':>6}  Title")
    print(f"  {'─' * id_w}  {'─' * 6}  {'─' * 40}")
    for s in sessions:
        title = (s["title"] or "(untitled)")[:40]
        print(f"  {s['id']:<{id_w}}  {s['part_count']:>6}  {title}")
