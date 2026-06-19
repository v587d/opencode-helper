"""
render.py - Rendering layer for drilldown agent call topology.

Three rendering modes from SessionGraph data:
  1. graph_to_topology()  - JSON-compatible dict for JS consumption
  2. render_terminal_tree() - ANSI-colored terminal tree view
  3. render_html()         - Self-contained HTML with embedded SVG renderer

Zero external dependencies - stdlib only.
"""

import json
from typing import Any

from drilldown.graph import SessionGraph, AgentStep, ToolCall, SpawnGroup


# ============================================================================
#  MCP tool classification (self-contained — mirrors analysis/mcp.py)
# ============================================================================

# Maps tool-name prefixes to MCP server labels.  Order matters: longer
# prefixes first so "chrome-mcp-server" matches before "chrome".
_SERVER_PREFIXES: list[tuple[str, str]] = [
    ("tavily_tavily_", "tavily"),
    ("websearch_web_search_exa", "websearch"),
    ("websearch_", "websearch"),
    ("context7_", "context7"),
    ("chrome-mcp-server_", "chrome"),
    ("grep_app_", "github-search"),
    ("PaddleOCR-VL_", "paddleocr"),
]


def _classify_tool(tool_name: str) -> str:
    """Map a tool name to its MCP server label, or ``"other"`` if not MCP."""
    for prefix, label in _SERVER_PREFIXES:
        if tool_name.startswith(prefix):
            return label
    if "mcp" in tool_name.lower():
        return "mcp-generic"
    return "other"


# ============================================================================
#  Chain (SpawnGroup) detail builder
# ============================================================================

def _build_chain_detail(group: SpawnGroup, graph: SessionGraph) -> dict:
    """Build detailed info for a chain (spawn group) node.

    Enriches the chain node with:
    - Parent message text (the message that spawned the parallel agents)
    - Per-child summaries (agent name, model, tokens, cost, tools, errors)
    - Aggregated tool list with MCP server classification
    - Rollup stats (total tools, errors, tokens, cost, unique agents)
    """
    # ── Find parent message text ──────────────────────────────────────
    parent_role = "user" if group.parent_is_user else "assistant"
    parent_text = ""

    if group.parent_is_user:
        for msg in graph.user_messages:
            if msg.get("id") == group.parent_message_id:
                parent_text = msg.get("text", "")
                break
    else:
        for step in graph.steps:
            if step.message_id == group.parent_message_id:
                parent_text = "\n".join(step.text_output) if step.text_output else ""
                break

    # ── Aggregate children + tools ────────────────────────────────────
    # Children are grouped by (agent_name, model_id) so that N parallel
    # invocations of the same agent collapse into one row with an
    # ``invocations`` count, instead of N near-identical rows.
    child_aggregates: dict[tuple, dict] = {}
    tool_aggregates: dict[str, dict] = {}

    for idx in group.child_indices:
        if idx >= len(graph.steps):
            continue
        step = graph.steps[idx]

        key = (step.agent_name, step.model_id)
        agg = child_aggregates.get(key)
        if agg is None:
            agg = {
                "agent_name": step.agent_name,
                "model_id": step.model_id,
                "invocations": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost": 0.0,
                "duration_ms": 0,
                "error_count": 0,
                "tool_count": 0,
                "invocation_texts": [],  # per-invocation response/reasoning
            }
            child_aggregates[key] = agg
        agg["invocations"] += 1
        agg["tokens_input"] += step.tokens_input
        agg["tokens_output"] += step.tokens_output
        agg["cost"] += step.cost
        agg["duration_ms"] += step.agent_duration_ms
        agg["error_count"] += step.error_count
        agg["tool_count"] += len(step.tools)

        # Per-invocation response/reasoning text
        agg["invocation_texts"].append({
            "message_id": step.message_id,
            "text_output": _truncate("\n".join(step.text_output), 2000) if step.text_output else "",
            "reasoning": _truncate("\n".join(step.reasoning), 2000) if step.reasoning else "",
        })

        for tool in step.tools:
            server = _classify_tool(tool.name)
            if tool.name not in tool_aggregates:
                tool_aggregates[tool.name] = {
                    "name": tool.name,
                    "count": 0,
                    "errors": 0,
                    "server": server,
                    "is_mcp": server != "other",
                    "invocations": [],  # per-call details (args + result)
                }
            tool_aggregates[tool.name]["count"] += 1
            if tool.is_error:
                tool_aggregates[tool.name]["errors"] += 1

            # Attach per-call detail (same shape as tool-node detail_entry)
            result_str = tool.result or ""
            tool_aggregates[tool.name]["invocations"].append({
                "status": tool.status,
                "duration_ms": tool.duration_ms,
                "arguments": tool.arguments,
                "arguments_full": _truncate(
                    json.dumps(tool.arguments, ensure_ascii=False), 1000
                ) if tool.arguments is not None else "",
                "result_preview": _truncate(result_str, 500),
                "result_full": _truncate(result_str, 2000),
                "call_id": tool.call_id,
                "time_start": tool.time_start,
            })

    # Children sorted by invocation count descending, then name for stability
    children = sorted(
        child_aggregates.values(),
        key=lambda c: (-c["invocations"], c["agent_name"]),
    )

    # Sort tools by count descending
    tools_list = sorted(tool_aggregates.values(), key=lambda t: -t["count"])
    mcp_tools = [t for t in tools_list if t["is_mcp"]]
    local_tools = [t for t in tools_list if not t["is_mcp"]]
    mcp_servers = sorted(set(t["server"] for t in mcp_tools))

    # ── Rollup stats ──────────────────────────────────────────────────
    total_tools = sum(c["tool_count"] for c in children)
    total_errors = sum(c["error_count"] for c in children)
    total_tokens_in = sum(c["tokens_input"] for c in children)
    total_tokens_out = sum(c["tokens_output"] for c in children)
    total_cost = sum(c["cost"] for c in children)
    unique_agents = list(dict.fromkeys(c["agent_name"] for c in children))
    total_invocations = sum(c["invocations"] for c in children)

    return {
        "parent_message_id": group.parent_message_id,
        "parent_role": parent_role,
        # NOTE: parent_text is NOT truncated — chain drawer's Expand
        # must show the full original message regardless of length.
        # Other text fields (agent_detail.text_output/reasoning, tool
        # results) remain truncated for prompt-size discipline.
        "parent_text": parent_text,
        "children": children,
        "tools": tools_list,
        "mcp_tools": mcp_tools,
        "local_tools": local_tools,
        "mcp_servers": mcp_servers,
        "total_tools": total_tools,
        "total_errors": total_errors,
        "total_tokens_input": total_tokens_in,
        "total_tokens_output": total_tokens_out,
        "total_cost": total_cost,
        "unique_agents": unique_agents,
        "total_invocations": total_invocations,
    }


def _truncate(s: str, limit: int) -> str:
    """Truncate a string to ``limit`` chars, appending a marker if cut."""
    if len(s) <= limit:
        return s
    return s[:limit] + "...[truncated]"


# ============================================================================
#  A. Topology conversion
# ============================================================================

def graph_to_topology(graph: SessionGraph) -> dict:
    """Convert SessionGraph to {nodes: [...], edges: [...]} for JS rendering.

    Node mapping:
      - AgentStep  -> id="agent:{agent_name}", type="agent"
      - ToolCall   -> id="tool:{tool.name}",   type="tool"
      - SpawnGroup -> id="group:{parent_id[:12]}", type="chain"

    Nodes are deduplicated by id; invocation_count, error_count, and
    avg_duration_ms are aggregated across duplicates.

    Edge mapping:
      - AgentStep -> ToolCall:  source="agent:{name}", target="tool:{name}"
      - Parent -> Child spawn:  source="agent:{parent}", target="agent:{child}"
      - SpawnGroup membership:  source="group:{id}", target="agent:{member}"

    Edges are deduplicated by source+target key; count is incremented.
    """
    # --- Collect nodes (deduplicated, aggregated with per-call details) ---
    node_map: dict[str, dict[str, Any]] = {}

    for step in graph.steps:
        # --- Agent node ---
        aid = f"agent:{step.agent_name}"
        if aid in node_map:
            n = node_map[aid]
            n["invocation_count"] += 1
            n["error_count"] += step.error_count
            if step.agent_duration_ms > 0:
                prev_total = n["avg_duration_ms"] * (n["invocation_count"] - 1)
                n["avg_duration_ms"] = (prev_total + step.agent_duration_ms) // n["invocation_count"]
            # Sum tokens and cost across occurrences
            if n.get("agent_detail"):
                n["agent_detail"]["tokens_input"] += step.tokens_input
                n["agent_detail"]["tokens_output"] += step.tokens_output
                n["agent_detail"]["cost"] += step.cost
        else:
            node_map[aid] = {
                "id": aid,
                "type": "agent",
                "invocation_count": 1,
                "error_count": step.error_count,
                "avg_duration_ms": step.agent_duration_ms,
                "agent_detail": {
                    "message_id": step.message_id,
                    "model_id": step.model_id,
                    "provider_id": step.provider_id,
                    "tokens_input": step.tokens_input,
                    "tokens_output": step.tokens_output,
                    "cost": step.cost,
                    "finish_reason": step.finish_reason,
                    "agent_duration_ms": step.agent_duration_ms,
                    "text_output": _truncate("\n".join(step.text_output), 2000) if step.text_output else "",
                    "reasoning": _truncate("\n".join(step.reasoning), 2000) if step.reasoning else "",
                    "is_subagent": step.is_subagent,
                    "subagent_depth": step.subagent_depth,
                    "source_session_id": step.session_id,
                },
            }

        # --- Tool nodes ---
        for tool in step.tools:
            tid = f"tool:{tool.name}"
            errors = 1 if tool.is_error else 0
            status = tool.status
            duration_ms = tool.duration_ms

            # Prepare arguments
            if tool.arguments is not None:
                arguments_dict = tool.arguments
                arguments_json = _truncate(
                    json.dumps(tool.arguments, ensure_ascii=False), 1000
                )
            else:
                arguments_dict = None
                arguments_json = ""

            # Prepare result
            result_str = tool.result or ""
            result_preview = _truncate(result_str, 500)
            result_full = _truncate(result_str, 2000)

            detail_entry = {
                "status": status,
                "duration_ms": duration_ms,
                "arguments": arguments_dict,
                "arguments_full": arguments_json,
                "result_preview": result_preview,
                "result_full": result_full,
                "call_id": tool.call_id,
                "time_start": tool.time_start,
            }

            if tid in node_map:
                n = node_map[tid]
                n["invocation_count"] += 1
                n["error_count"] += errors
                if duration_ms > 0:
                    prev_total = n["avg_duration_ms"] * (n["invocation_count"] - 1)
                    n["avg_duration_ms"] = (prev_total + duration_ms) // n["invocation_count"]
                n["details"].append(detail_entry)
            else:
                node_map[tid] = {
                    "id": tid,
                    "type": "tool",
                    "invocation_count": 1,
                    "error_count": errors,
                    "avg_duration_ms": duration_ms,
                    "details": [detail_entry],
                }

    for group in graph.spawn_groups:
        gid = f"group:{group.parent_message_id[:12]}"
        if gid not in node_map:
            detail = _build_chain_detail(group, graph)
            node_map[gid] = {
                "id": gid,
                "type": "chain",
                "invocation_count": 1,
                "error_count": detail["total_errors"],
                "avg_duration_ms": 0,
                "chain_detail": detail,
            }
        else:
            # Rare: 12-char prefix collision across different parent IDs.
            # Bump counts; first group's detail wins.
            node_map[gid]["invocation_count"] += 1

    # --- Collect edges (deduplicated, count incremented) ---
    edge_map: dict[str, dict[str, Any]] = {}

    def _add_edge(source: str, target: str):
        key = f"{source}->{target}"
        if key in edge_map:
            edge_map[key]["count"] += 1
        else:
            edge_map[key] = {"source": source, "target": target, "count": 1}

    # Agent -> Tool edges
    for step in graph.steps:
        for tool in step.tools:
            _add_edge(f"agent:{step.agent_name}", f"tool:{tool.name}")

    # Parent -> Child spawn edges
    # Build a message_id -> agent_name lookup
    msg_agent: dict[str, str] = {}
    for step in graph.steps:
        msg_agent[step.message_id] = step.agent_name

    for group in graph.spawn_groups:
        parent_name = msg_agent.get(group.parent_message_id, "")
        if parent_name:
            for idx in group.child_indices:
                if idx < len(graph.steps):
                    child_name = graph.steps[idx].agent_name
                    if child_name != parent_name:
                        _add_edge(f"agent:{parent_name}", f"agent:{child_name}")

    # SpawnGroup membership edges
    for group in graph.spawn_groups:
        gid = f"group:{group.parent_message_id[:12]}"
        for idx in group.child_indices:
            if idx < len(graph.steps):
                _add_edge(gid, f"agent:{graph.steps[idx].agent_name}")

    return {
        "nodes": list(node_map.values()),
        "edges": list(edge_map.values()),
    }


# ============================================================================
#  B. Terminal tree view
# ============================================================================

# ANSI escape codes
_C = "\033["  # CSI prefix
_RESET = f"{_C}0m"
_BOLD = f"{_C}1m"
_DIM = f"{_C}2m"
_CYAN = f"{_C}36m"
_RED = f"{_C}31m"
_GREEN = f"{_C}32m"
_YELLOW = f"{_C}33m"
_BLUE = f"{_C}34m"
_MAGENTA = f"{_C}35m"


def render_terminal_tree(graph: SessionGraph) -> str:
    """Generate an ANSI-colored terminal tree view of the session graph.

    Layout:
      Session header
      Each AgentStep as a tree node with connectors
      Tool calls indented under their agent step
      Shows: agent name, model, tokens, cost, error count
      For each tool: name, status, duration
    """
    lines: list[str] = []

    # Session header
    lines.append(f"{_BOLD}{_CYAN}Session{_RESET}: {graph.session_id}")
    lines.append(f"{_DIM}Title: {graph.title}{_RESET}")
    stats_parts = [
        f"Agents: {len(graph.steps)}",
        f"Tools: {graph.total_tools}",
        f"Errors: {graph.total_errors}",
    ]
    if graph.child_sessions:
        stats_parts.append(f"Subagents: {len(graph.child_sessions)}")
    lines.append(f"{_DIM}{'  '.join(stats_parts)}{_RESET}")
    lines.append("")

    n_steps = len(graph.steps)
    for i, step in enumerate(graph.steps):
        is_last = (i == n_steps - 1)
        connector = "\u2514\u2500" if is_last else "\u251c\u2500"  # └─ / ├─

        # Agent line — prefix with depth badge for sub-agents
        depth_prefix = ""
        if step.is_subagent:
            depth_prefix = f"[d{step.subagent_depth}] "
        agent_label = f"{_BOLD}{_CYAN}{depth_prefix}{step.agent_name}{_RESET}"
        model_label = f"{_DIM}{step.model_id}{_RESET}"
        tokens_in = step.tokens_input
        tokens_out = step.tokens_output
        token_str = f"{_DIM}\u2191{tokens_in}/\u2193{tokens_out}{_RESET}"
        cost_str = f"{_DIM}${step.cost:.4f}{_RESET}" if step.cost else ""

        err_str = ""
        if step.error_count > 0:
            err_str = f" {_RED}{step.error_count} err{_RESET}"

        lines.append(
            f"  {connector} {agent_label}  {model_label}  {token_str}  {cost_str}{err_str}"
        )

        # Sub-agent session id sub-line
        if step.is_subagent:
            sid = step.session_id
            lines.append(
                f"  {'   ' if is_last else '\u2502  '}"
                f"  {_DIM}session={sid}{_RESET}"
            )

        # Tool calls under this agent
        n_tools = len(step.tools)
        prefix = "  " + ("   " if is_last else "\u2502  ")  # "   " / "│  "
        for j, tool in enumerate(step.tools):
            is_last_tool = (j == n_tools - 1)
            tconn = "\u2514\u2500" if is_last_tool else "\u251c\u2500"

            status_icon = f"{_GREEN}OK{_RESET}" if not tool.is_error else f"{_RED}ERR{_RESET}"
            tool_label = f"{_YELLOW}{tool.name}{_RESET}"
            dur_str = f"{_DIM}{tool.duration_ms}ms{_RESET}" if tool.duration_ms else f"{_DIM}?ms{_RESET}"

            lines.append(f"{prefix}{tconn} {status_icon} {tool_label}  {dur_str}")

    # Spawn groups summary
    if graph.spawn_groups:
        lines.append("")
        lines.append(f"{_BOLD}{_MAGENTA}Spawn Groups{_RESET}:")
        for group in graph.spawn_groups:
            children = [graph.steps[idx].agent_name for idx in group.child_indices
                        if idx < len(graph.steps)]
            role = "user" if group.parent_is_user else "agent"
            lines.append(
                f"  {_DIM}parent={group.parent_message_id[:12]}... ({role})"
                f" -> {', '.join(children)}{_RESET}"
            )

    return "\n".join(lines)


# ============================================================================
#  C. HTML rendering
# ============================================================================

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<title>OpenCode Session: __TITLE__</title>
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --text-dim: #8b949e;
  --node-agent: #2ea043;
  --node-tool: #d29922;
  --node-chain: #6e7681;
  --node-error: #f85149;
  --accent: #58a6ff;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  height: 100vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.app-header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 12px 24px;
  flex-shrink: 0;
}
.app-header h1 {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 4px;
}
.session-info {
  font-size: 13px;
  color: var(--text-dim);
}
main {
  flex: 1;
  position: relative;
  overflow: hidden;
}
#graph-container {
  width: 100%;
  height: 100%;
}
#graph-container svg {
  width: 100%;
  height: 100%;
  display: block;
}
#graph-tooltip {
  position: absolute;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.5;
  pointer-events: none;
  z-index: 100;
  max-width: 280px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}
#graph-tooltip.hidden { display: none; }
#graph-tooltip .tt-name { font-weight: 600; margin-bottom: 4px; }
#graph-tooltip .tt-row { color: var(--text-dim); }
.graph-legend {
  position: absolute;
  bottom: 16px;
  left: 16px;
  display: flex;
  gap: 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 14px;
  font-size: 12px;
  color: var(--text-dim);
}
.legend-item { display: flex; align-items: center; gap: 6px; }
.legend-shape {
  width: 14px;
  height: 14px;
  display: inline-block;
}
.legend-shape.agent { background: var(--node-agent); clip-path: polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%); }
.legend-shape.tool { background: var(--node-tool); border-radius: 3px; }
.legend-shape.chain { background: var(--node-chain); clip-path: polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%); }
/* SVG node styles */
.node-shape { cursor: pointer; transition: opacity 0.2s; }
.node-shape:hover { filter: brightness(1.3); }
.node-shape.subagent { stroke-dasharray: 4,3; }
.node-label { font-size: 11px; fill: var(--text); text-anchor: middle; pointer-events: none; }
.node-count { font-size: 9px; fill: var(--text); text-anchor: middle; pointer-events: none; }
.node-depth-badge { font-size: 9px; fill: var(--text-dim); text-anchor: start; }
.dim .node-shape { opacity: 0.15; }
.dim .node-label { opacity: 0.15; }
.dim .node-count { opacity: 0.15; }
.focus .node-shape { filter: brightness(1.4); }
.edge-line { stroke: var(--border); stroke-width: 1.5; fill: none; }
.edge-line.dim { opacity: 0.08; }
.edge-line.focus { stroke: var(--accent); stroke-width: 2; }
.col-header { font-size: 13px; fill: var(--text-dim); text-anchor: middle; font-weight: 600; letter-spacing: 1px; }
.col-band { fill: var(--surface); opacity: 0.3; }
/* Drawer */
.drawer-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  z-index: 200; transition: opacity 0.2s;
}
.drawer-backdrop.hidden { opacity: 0; pointer-events: none; }
.drawer {
  position: fixed; top: 0; right: 0; width: 420px; max-width: 90vw; height: 100vh;
  background: var(--surface); border-left: 1px solid var(--border);
  z-index: 201; transform: translateX(105%);
  transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  display: flex; flex-direction: column; box-shadow: -4px 0 24px rgba(0,0,0,0.4);
}
.drawer.open { transform: translateX(0); }
.drawer-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.drawer-header h3 { font-size: 14px; font-weight: 600; margin: 0; }
.drawer-close-btn {
  background: none; border: none; color: var(--text-dim); font-size: 22px;
  cursor: pointer; padding: 0 4px; line-height: 1; transition: color 0.15s;
}
.drawer-close-btn:hover { color: var(--text); }
.drawer-body {
  flex: 1; overflow-y: auto; padding: 18px;
  font-size: 13px; line-height: 1.5;
}
.drawer-body h4 {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.7px; color: var(--text-dim); margin: 0 0 10px;
}
.drawer-section { margin-bottom: 20px; }
.drawer-section:last-child { margin-bottom: 0; }

/* Stats grid in drawer */
.drawer-stats {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  margin-bottom: 16px;
}
.drawer-stat {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 12px;
}
.drawer-stat .stat-label {
  display: block; font-size: 9.5px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.7px; color: var(--text-dim);
  margin-bottom: 4px;
}
.drawer-stat .stat-value {
  font-size: 15px; font-weight: 700; color: var(--text);
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.drawer-stat .stat-value.error { color: var(--node-error); }

/* Tool call detail cards */
.tool-detail-card {
  background: var(--bg); border: 1px solid var(--border);
  border-left: 3px solid var(--border); border-radius: 6px;
  padding: 12px; margin-bottom: 10px;
}
.tool-detail-card.error { border-left-color: var(--node-error); background: rgba(248,81,73,0.06); }
.tool-detail-card .tdc-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
}
.tool-detail-card .tdc-status {
  font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
}
.tool-detail-card .tdc-status.ok { background: rgba(46,160,67,0.2); color: var(--node-agent); }
.tool-detail-card .tdc-status.err { background: rgba(248,81,73,0.2); color: var(--node-error); }
.tool-detail-card .tdc-duration {
  font-size: 11px; color: var(--text-dim); margin-left: auto;
}
.tool-detail-card .tdc-section { margin-bottom: 8px; }
.tool-detail-card .tdc-section:last-child { margin-bottom: 0; }
.tool-detail-card .tdc-label {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--text-dim); margin-bottom: 4px;
}
.tool-detail-card .tdc-content {
  font-size: 12px; font-family: ui-monospace, SFMono-Regular, monospace;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 4px; padding: 8px 10px; white-space: pre-wrap;
  word-break: break-all; max-height: 200px; overflow-y: auto;
  line-height: 1.4;
}
.tool-detail-card .tdc-content.expanded { max-height: none; }
.tool-detail-card .tdc-expand {
  font-size: 11px; color: var(--accent); cursor: pointer; margin-top: 4px;
  display: inline-block;
}
.tool-detail-card .tdc-expand:hover { text-decoration: underline; }

/* Expand/collapse toggle — shared by tool cards, agent text blocks, chain parent message */
.tdc-expand {
  font-size: 11px; color: var(--accent); cursor: pointer; margin-top: 4px;
  display: inline-block;
}
.tdc-expand:hover { text-decoration: underline; }

/* Agent text blocks */
.agent-text-block {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 12px; margin-bottom: 10px;
}
.agent-text-block .atb-label {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--text-dim); margin-bottom: 8px;
}
.agent-text-block .atb-content {
  font-size: 12px; white-space: pre-wrap; word-break: break-word;
  max-height: 300px; overflow-y: auto; line-height: 1.5;
}
.agent-text-block .atb-content.expanded { max-height: none; }

/* No data state */
.drawer-empty {
  text-align: center; padding: 40px 20px; color: var(--text-dim);
}
.drawer-empty p { font-size: 13px; font-style: italic; }

/* ── Chain node: hero section ─────────────────────────────────────── */
.drawer-hero {
  background: linear-gradient(180deg, var(--bg) 0%, var(--surface) 100%);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px;
  margin-bottom: 18px;
}
.hero-row {
  display: flex; align-items: center; gap: 12px; margin-bottom: 14px;
}
.hero-icon {
  flex-shrink: 0; width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; border-radius: 8px;
  background: var(--bg); border: 1px solid var(--border);
}
.hero-icon-chain { border-color: var(--node-chain); }
.hero-text { flex: 1; min-width: 0; }
.hero-kind {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.7px; color: var(--text-dim); margin-bottom: 2px;
}
.hero-name {
  font-size: 15px; font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* ── Count pill (section header badge) ────────────────────────────── */
.count-pill {
  display: inline-block; font-size: 11px; font-weight: 600;
  color: var(--text-dim); background: var(--bg);
  border: 1px solid var(--border); border-radius: 999px;
  padding: 1px 7px; margin-left: 4px; vertical-align: middle;
}

/* ── Connection item (clickable child row) ────────────────────────── */
.connection-item {
  display: grid;
  grid-template-columns: 32px 1fr auto;
  align-items: center; gap: 10px;
  width: 100%; background: var(--bg);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 12px; cursor: pointer;
  transition: border-color 180ms, background 180ms, transform 120ms;
  margin-bottom: 6px; color: var(--text); font-family: inherit;
  text-align: left;
}
.connection-item:hover {
  border-color: var(--accent); background: rgba(88,166,255,0.08);
  transform: translateX(2px);
}
.connection-item:active { transform: translateX(0); }
.conn-icon {
  width: 28px; height: 28px; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 700;
}
.conn-icon-agent { background: rgba(46,160,67,0.15); color: var(--node-agent); }
.conn-icon-tool { background: rgba(210,153,34,0.15); color: var(--node-tool); }
.conn-text { min-width: 0; }
.conn-name {
  display: block; font-size: 13px; font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.conn-type {
  display: block; font-size: 11px; color: var(--text-dim); margin-top: 1px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.conn-arrow { color: var(--text-dim); font-size: 16px; flex-shrink: 0; }

/* ── Spawned agent row (collapsible, no navigation) ──────────────── */
.child-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 6px;
  color: var(--text);
  font-family: inherit;
  text-align: left;
}
.child-row-collapsible {
  cursor: pointer;
  user-select: none;
  transition: border-color 180ms, background 180ms;
}
.child-row-collapsible:hover {
  border-color: var(--accent);
  background: rgba(88,166,255,0.08);
}
.child-row-collapsible.open {
  border-color: var(--accent);
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 0;
  margin-bottom: 0;
}
.child-chevron {
  color: var(--text-dim); font-size: 16px; flex-shrink: 0;
  margin-left: auto;
  transition: transform 180ms;
  transform: rotate(90deg); /* collapsed: points down */
}
.child-row-collapsible:hover .child-chevron { color: var(--accent); }
.child-row-collapsible.open .child-chevron {
  transform: rotate(-90deg); /* expanded: points up */
  color: var(--accent);
}

/* ── Collapsible response panel (per-invocation texts) ────────────── */
.child-responses {
  margin-bottom: 8px;
  padding: 8px 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0 0 8px 8px;
  border-top: none;
}
.response-entry {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 8px;
}
.response-entry:last-child { margin-bottom: 0; }
.response-meta {
  font-size: 10px;
  font-family: ui-monospace, SFMono-Regular, monospace;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.response-label {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-dim);
  margin-bottom: 4px;
  margin-top: 8px;
}
.response-label:first-of-type { margin-top: 0; }
.response-content {
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 300px;
  overflow-y: auto;
  line-height: 1.5;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 8px 10px;
}
.response-content.expanded { max-height: none; }

/* ── Tool row (aggregated tool list) ──────────────────────────────── */
.tool-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 10px; margin-bottom: 3px;
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 5px; font-size: 12px;
}
.tool-row .tool-name {
  flex: 1; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tool-row .tool-count {
  color: var(--text-dim); font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 11px; flex-shrink: 0;
}
.tool-row .err-badge {
  font-size: 10px; font-weight: 600; color: var(--node-error);
  background: rgba(248,81,73,0.12); border-radius: 999px;
  padding: 1px 6px; flex-shrink: 0;
}

/* ── Collapsible tool row (chain drawer — click to reveal invocations) ── */
.tool-row-collapsible {
  cursor: pointer; user-select: none;
  transition: border-color 180ms, background 180ms;
}
.tool-row-collapsible:hover {
  border-color: var(--accent); background: rgba(88,166,255,0.06);
}
.tool-row-collapsible.open {
  border-color: var(--accent);
  border-bottom-left-radius: 0; border-bottom-right-radius: 0;
  margin-bottom: 0;
}
.tool-row-chevron {
  color: var(--text-dim); font-size: 16px; flex-shrink: 0;
  transition: transform 180ms;
  transform: rotate(90deg); /* collapsed: points down */
}
.tool-row-collapsible:hover .tool-row-chevron { color: var(--accent); }
.tool-row-collapsible.open .tool-row-chevron {
  transform: rotate(-90deg); /* expanded: points up */
  color: var(--accent);
}
.tool-invocations {
  border: 1px solid var(--accent); border-top: none;
  border-radius: 0 0 5px 5px;
  padding: 8px; margin-bottom: 3px;
  background: var(--surface);
}
.tool-invocations .tool-detail-card { margin-bottom: 8px; }
.tool-invocations .tool-detail-card:last-child { margin-bottom: 0; }

/* ── MCP server group (tools grouped by server) ───────────────────── */
.mcp-server-group {
  margin-bottom: 10px; padding: 8px 10px;
  background: rgba(57,197,207,0.04);
  border: 1px solid rgba(57,197,207,0.15);
  border-radius: 8px;
}
.mcp-server-label {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; color: #39c5cf; margin-bottom: 6px;
}
</style>
</head>
<body>
<header class="app-header">
<h1>OpenCode Session Flow</h1>
<div class="session-info">__SESSION_INFO__</div>
</header>
<main>
<div id="graph-container"></div>
<div id="graph-tooltip" class="hidden"></div>
<div class="graph-legend">
<div class="legend-item"><span class="legend-shape agent"></span>Agent</div>
<div class="legend-item"><span class="legend-shape tool"></span>Tool</div>
<div class="legend-item"><span class="legend-shape chain"></span>Chain</div>
</div>
<div id="drawer-backdrop" class="drawer-backdrop hidden"></div>
<div id="drawer" class="drawer">
  <div class="drawer-header">
    <h3 id="drawer-title">Node Detail</h3>
    <button id="drawer-close" class="drawer-close-btn" title="Close (Esc)">&times;</button>
  </div>
  <div class="drawer-body" id="drawer-body"></div>
</div>
</main>
<script>
(function() {
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var COL_W = 220, ROW_H = 100, PAD = 60, HEADER_H = 48, NODE_R = 32;
  var KIND_COLUMNS = ['agent', 'tool', 'chain'];

  var TOPOLOGY = __TOPOLOGY_JSON__;

  function el(tag, attrs, children) {
    var e = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (var k in attrs) {
        if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]);
      }
    }
    if (children) {
      if (!Array.isArray(children)) children = [children];
      children.forEach(function(c) {
        if (typeof c === 'string') {
          e.appendChild(document.createTextNode(c));
        } else if (c) {
          e.appendChild(c);
        }
      });
    }
    return e;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  var nodeMap = {};
  var edgeList = [];
  var positions = {};
  var focused = null;
  var svgRoot = null;

  function layout() {
    var byKind = { agent: [], tool: [], chain: [] };
    TOPOLOGY.nodes.forEach(function(n) {
      if (byKind[n.type]) byKind[n.type].push(n);
    });
    for (var kind in byKind) {
      byKind[kind].sort(function(a, b) { return a.id.localeCompare(b.id); });
    }
    var svgW = PAD * 2 + COL_W * 3;
    var maxRows = Math.max(byKind.agent.length, byKind.tool.length, byKind.chain.length, 1);
    var svgH = HEADER_H + PAD + maxRows * ROW_H + PAD;

    var colX = {};
    KIND_COLUMNS.forEach(function(kind, ci) {
      colX[kind] = PAD + ci * COL_W + COL_W / 2;
    });

    for (var kind in byKind) {
      var nodes = byKind[kind];
      var totalH = nodes.length * ROW_H;
      var startY = HEADER_H + PAD + (maxRows * ROW_H - totalH) / 2 + ROW_H / 2;
      nodes.forEach(function(n, ri) {
        positions[n.id] = { x: colX[kind], y: startY + ri * ROW_H };
        nodeMap[n.id] = n;
      });
    }

    edgeList = TOPOLOGY.edges;
    return { w: svgW, h: svgH };
  }

  function hexPoints(cx, cy, r) {
    var pts = [];
    for (var i = 0; i < 6; i++) {
      var angle = Math.PI / 3 * i - Math.PI / 6;
      pts.push((cx + r * Math.cos(angle)).toFixed(1) + ',' + (cy + r * Math.sin(angle)).toFixed(1));
    }
    return pts.join(' ');
  }

  function diamondPoints(cx, cy, r) {
    return [
      (cx) + ',' + (cy - r),
      (cx + r) + ',' + (cy),
      (cx) + ',' + (cy + r),
      (cx - r) + ',' + (cy)
    ].join(' ');
  }

  function build(svg, dims) {
    svg.setAttribute('viewBox', '0 0 ' + dims.w + ' ' + dims.h);
    svg.setAttribute('width', dims.w);
    svg.setAttribute('height', dims.h);

    // Column bands + headers
    KIND_COLUMNS.forEach(function(kind, ci) {
      var x = PAD + ci * COL_W;
      svg.appendChild(el('rect', {
        x: x, y: 0, width: COL_W, height: dims.h,
        class: 'col-band'
      }));
      svg.appendChild(el('text', {
        x: x + COL_W / 2, y: HEADER_H - 10,
        class: 'col-header'
      }, [kind.toUpperCase()]));
    });

    // Defs: arrowhead marker
    var defs = el('defs');
    var marker = el('marker', {
      id: 'arrowhead', markerWidth: 8, markerHeight: 6,
      refX: 8, refY: 3, orient: 'auto'
    });
    marker.appendChild(el('polygon', {
      points: '0 0, 8 3, 0 6', fill: cssVar('--border') || '#30363d'
    }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    // Edges
    edgeList.forEach(function(edge, ei) {
      var sp = positions[edge.source];
      var tp = positions[edge.target];
      if (!sp || !tp) return;
      var line = el('line', {
        x1: sp.x, y1: sp.y, x2: tp.x, y2: tp.y,
        class: 'edge-line',
        'data-source': edge.source,
        'data-target': edge.target,
        'marker-end': 'url(#arrowhead)'
      });
      svg.appendChild(line);
    });

    // Nodes
    TOPOLOGY.nodes.forEach(function(n) {
      var pos = positions[n.id];
      if (!pos) return;
      var g = el('g', { 'data-id': n.id, class: 'node-group' });

      var fillColor, strokeColor;
      if (n.type === 'agent') { fillColor = cssVar('--node-agent') || '#2ea043'; strokeColor = fillColor; }
      else if (n.type === 'tool') { fillColor = cssVar('--node-tool') || '#d29922'; strokeColor = fillColor; }
      else { fillColor = cssVar('--node-chain') || '#6e7681'; strokeColor = fillColor; }

      var strokeW = 2;
      if (n.error_count > 0) {
        strokeColor = cssVar('--node-error') || '#f85149';
        strokeW = 3;
      }

      var shape;
      if (n.type === 'agent') {
        var agentClasses = 'node-shape';
        if (n.agent_detail && n.agent_detail.is_subagent) {
          agentClasses += ' subagent';
        }
        shape = el('polygon', {
          points: hexPoints(pos.x, pos.y, NODE_R),
          fill: fillColor, stroke: strokeColor, 'stroke-width': strokeW,
          class: agentClasses
        });
      } else if (n.type === 'tool') {
        shape = el('rect', {
          x: pos.x - NODE_R, y: pos.y - NODE_R * 0.65,
          width: NODE_R * 2, height: NODE_R * 1.3,
          rx: 7, ry: 7,
          fill: fillColor, stroke: strokeColor, 'stroke-width': strokeW,
          class: 'node-shape'
        });
      } else {
        shape = el('polygon', {
          points: diamondPoints(pos.x, pos.y, NODE_R),
          fill: fillColor, stroke: strokeColor, 'stroke-width': strokeW,
          class: 'node-shape'
        });
      }
      g.appendChild(shape);

      // Name label below shape
      var label = n.id.split(':')[1] || n.id;
      if (label.length > 18) label = label.substring(0, 16) + '..';
      g.appendChild(el('text', {
        x: pos.x, y: pos.y + NODE_R + 14,
        class: 'node-label'
      }, [label]));

      // Depth badge for subagent nodes
      if (n.agent_detail && n.agent_detail.is_subagent) {
        var depthLabel = '[d' + n.agent_detail.subagent_depth + ']';
        g.appendChild(el('text', {
          x: pos.x + NODE_R * 0.3, y: pos.y + NODE_R + 26,
          class: 'node-depth-badge'
        }, [depthLabel]));
      }

      // Invocation count bubble (top-right)
      if (n.invocation_count > 1) {
        var bx = pos.x + NODE_R * 0.6;
        var by = pos.y - NODE_R * 0.6;
        g.appendChild(el('circle', {
          cx: bx, cy: by, r: 9,
          fill: cssVar('--surface') || '#161b22',
          stroke: strokeColor, 'stroke-width': 1
        }));
        g.appendChild(el('text', {
          x: bx, y: by + 3.5,
          class: 'node-count', 'font-weight': '600'
        }, [String(n.invocation_count)]));
      }

      svg.appendChild(g);
    });
  }

  function wireInteractions(svg, dims) {
    var tooltip = document.getElementById('graph-tooltip');
    var vb = { x: 0, y: 0, w: dims.w, h: dims.h };
    var dragging = false, dragStart = { x: 0, y: 0 }, vbStart = { x: 0, y: 0 };

    function updateVB() {
      svg.setAttribute('viewBox', vb.x + ' ' + vb.y + ' ' + vb.w + ' ' + vb.h);
    }

    // Pan
    svg.addEventListener('mousedown', function(e) {
      if (e.target === svg || e.target.classList.contains('col-band')) {
        dragging = true;
        dragStart = { x: e.clientX, y: e.clientY };
        vbStart = { x: vb.x, y: vb.y };
        svg.style.cursor = 'grabbing';
      }
    });
    window.addEventListener('mousemove', function(e) {
      if (!dragging) return;
      var scale = vb.w / svg.clientWidth;
      vb.x = vbStart.x - (e.clientX - dragStart.x) * scale;
      vb.y = vbStart.y - (e.clientY - dragStart.y) * scale;
      updateVB();
    });
    window.addEventListener('mouseup', function() {
      dragging = false;
      svg.style.cursor = '';
    });

    // Zoom
    svg.addEventListener('wheel', function(e) {
      e.preventDefault();
      var factor = e.deltaY > 0 ? 1.1 : 0.9;
      var newW = vb.w * factor;
      var newH = vb.h * factor;
      var minW = dims.w * 0.33;
      var maxW = dims.w * 2;
      if (newW < minW || newW > maxW) return;
      var mx = e.clientX / svg.clientWidth;
      var my = e.clientY / svg.clientHeight;
      vb.x += (vb.w - newW) * mx;
      vb.y += (vb.h - newH) * my;
      vb.w = newW;
      vb.h = newH;
      updateVB();
    }, { passive: false });

    // Click node: toggle focus + open drawer
    svg.addEventListener('click', function(e) {
      var g = e.target.closest('[data-id]');
      if (!g) {
        clearFocus();
        closeDrawer();
        return;
      }
      var nid = g.getAttribute('data-id');
      if (focused === nid) {
        clearFocus();
        closeDrawer();
      } else {
        setFocus(nid);
        openDrawer(nid);
      }
    });

    // setFocus / clearFocus are defined at IIFE level (shared with drawer)

    // Hover: tooltip
    svg.addEventListener('mouseover', function(e) {
      var g = e.target.closest('[data-id]');
      if (!g) { tooltip.classList.add('hidden'); return; }
      var nid = g.getAttribute('data-id');
      var n = nodeMap[nid];
      if (!n) return;
      var name = nid.split(':')[1] || nid;
      tooltip.innerHTML =
        '<div class="tt-name">' + name + '</div>' +
        '<div class="tt-row">Type: ' + n.type + '</div>' +
        '<div class="tt-row">Invocations: ' + n.invocation_count + '</div>' +
        '<div class="tt-row">Errors: ' + n.error_count + '</div>' +
        '<div class="tt-row">Avg duration: ' + n.avg_duration_ms + 'ms</div>';
      tooltip.classList.remove('hidden');
      var rect = svg.getBoundingClientRect();
      var tx = e.clientX - rect.left + 14;
      var ty = e.clientY - rect.top + 14;
      tooltip.style.left = tx + 'px';
      tooltip.style.top = ty + 'px';
    });
    svg.addEventListener('mouseout', function(e) {
      var g = e.target.closest('[data-id]');
      if (g) return;
      tooltip.classList.add('hidden');
    });
  }

  // --- Focus management (IIFE level — shared by svg click + drawer navigation) ---
  function setFocus(nid) {
    if (!svgRoot) return;
    focused = nid;
    var neighbors = new Set([nid]);
    edgeList.forEach(function(e) {
      if (e.source === nid) neighbors.add(e.target);
      if (e.target === nid) neighbors.add(e.source);
    });
    svgRoot.querySelectorAll('.node-group').forEach(function(g) {
      var id = g.getAttribute('data-id');
      g.classList.remove('dim', 'focus');
      if (neighbors.has(id)) g.classList.add('focus');
      else g.classList.add('dim');
    });
    svgRoot.querySelectorAll('.edge-line').forEach(function(line) {
      var s = line.getAttribute('data-source');
      var t = line.getAttribute('data-target');
      line.classList.remove('dim', 'focus');
      if (s === nid || t === nid) line.classList.add('focus');
      else line.classList.add('dim');
    });
  }

  function clearFocus() {
    if (!svgRoot) return;
    focused = null;
    svgRoot.querySelectorAll('.node-group').forEach(function(g) {
      g.classList.remove('dim', 'focus');
    });
    svgRoot.querySelectorAll('.edge-line').forEach(function(l) {
      l.classList.remove('dim', 'focus');
    });
  }

  // --- Drawer ---
  var drawer = document.getElementById('drawer');
  var drawerBackdrop = document.getElementById('drawer-backdrop');
  var drawerTitle = document.getElementById('drawer-title');
  var drawerBody = document.getElementById('drawer-body');

  function openDrawer(nid) {
    var n = nodeMap[nid];
    if (!n) return;
    drawerTitle.textContent = nid.split(':')[1] || nid;
    drawerBody.innerHTML = buildDrawerContent(n);
    drawer.classList.add('open');
    drawerBackdrop.classList.remove('hidden');
  }

  function closeDrawer() {
    drawer.classList.remove('open');
    drawerBackdrop.classList.add('hidden');
  }

  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  drawerBackdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeDrawer();
  });

  // Delegated expand/collapse + connection-item navigation
  drawerBody.addEventListener('click', function(e) {
    // Spawned agent row: toggle response/reasoning panel (no navigation)
    var childRow = e.target.closest('.child-row-collapsible');
    if (childRow) {
      var responsesPanel = childRow.nextElementSibling;
      if (responsesPanel && responsesPanel.classList.contains('child-responses')) {
        var isOpen = childRow.classList.toggle('open');
        responsesPanel.style.display = isOpen ? 'block' : 'none';
      }
      return;
    }

    // Connection item: navigate to target node's drawer
    var conn = e.target.closest('.connection-item');
    if (conn) {
      var targetId = conn.getAttribute('data-node-id');
      if (targetId && nodeMap[targetId]) {
        setFocus(targetId);
        openDrawer(targetId);
      }
      return;
    }

    // Collapsible tool row: toggle per-call detail cards
    var toolRow = e.target.closest('.tool-row-collapsible');
    if (toolRow) {
      var invocations = toolRow.nextElementSibling;
      if (invocations && invocations.classList.contains('tool-invocations')) {
        var isOpen = toolRow.classList.toggle('open');
        invocations.style.display = isOpen ? 'block' : 'none';
      }
      return;
    }

    // Expand/collapse for tool detail cards and text blocks
    var expand = e.target.closest('.tdc-expand');
    if (!expand) return;
    var content = expand.previousElementSibling;
    if (!content) return;
    var full = content.getAttribute('data-full');
    if (!full) return;
    var isExpanded = content.classList.toggle('expanded');
    if (isExpanded) {
      content.textContent = full;
      expand.textContent = 'Collapse';
    } else {
      // Show preview (first 500 chars)
      var preview = full.length > 500 ? full.substring(0, 500) + '...[truncated]' : full;
      content.textContent = preview;
      expand.textContent = 'Expand';
    }
  });

  function escapeHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function escapeAttr(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Render a tool row (clickable header) + collapsible per-call detail cards.
  // Reuses the same tool-detail-card CSS as the tool-node drawer, so the
  // expand/collapse for arguments/result works via the shared .tdc-expand
  // delegated handler.
  function renderToolWithInvocations(t) {
    var errBadge = t.errors > 0 ? '<span class="err-badge">' + t.errors + ' err</span>' : '';
    var html = '<div class="tool-row tool-row-collapsible">' +
      '<span class="tool-name">' + escapeHtml(t.name) + '</span>' +
      '<span class="tool-count">\u00d7' + t.count + '</span>' + errBadge +
      '<span class="tool-row-chevron">\u203A</span>' +
      '</div>';
    // Collapsible container (hidden by default)
    html += '<div class="tool-invocations" style="display:none;">';
    if (t.invocations && t.invocations.length > 0) {
      t.invocations.forEach(function(d, i) {
        var cardClass = d.status === 'error' ? ' error' : '';
        html += '<div class="tool-detail-card' + cardClass + '">';
        html += '<div class="tdc-header">';
        html += '<span class="tdc-status ' + (d.status === 'error' ? 'err' : 'ok') + '">' + (d.status || 'OK').toUpperCase() + '</span>';
        if (d.call_id) {
          html += '<span style="font-size:10px;color:var(--text-dim);font-family:monospace">#' + (i + 1) + ' \u00b7 ' + escapeHtml(d.call_id.substring(0, 16)) + '</span>';
        } else {
          html += '<span style="font-size:10px;color:var(--text-dim);font-family:monospace">#' + (i + 1) + '</span>';
        }
        html += '<span class="tdc-duration">' + d.duration_ms + 'ms</span>';
        html += '</div>';

        // Arguments
        if (d.arguments) {
          var argStr = JSON.stringify(d.arguments, null, 2);
          var argPreview = argStr.length > 500 ? argStr.substring(0, 500) + '...[truncated]' : argStr;
          html += '<div class="tdc-section"><div class="tdc-label">Arguments</div>';
          html += '<div class="tdc-content" data-full="' + escapeAttr(argStr) + '">' + escapeHtml(argPreview) + '</div>';
          if (argStr.length > 500) {
            html += '<span class="tdc-expand">Expand</span>';
          }
          html += '</div>';
        }

        // Result
        if (d.result_preview) {
          html += '<div class="tdc-section"><div class="tdc-label">Result</div>';
          var resultFull = d.result_full || d.result_preview;
          var resultPreview = d.result_preview.length > 500 ? d.result_preview.substring(0, 500) + '...[truncated]' : d.result_preview;
          html += '<div class="tdc-content" data-full="' + escapeAttr(resultFull) + '">' + escapeHtml(resultPreview) + '</div>';
          if (resultFull.length > 500) {
            html += '<span class="tdc-expand">Expand</span>';
          }
          html += '</div>';
        }

        if (d.status === 'error' && d.result_preview) {
          html += '<div class="tdc-section"><div class="tdc-label">Error</div>';
          html += '<div class="tdc-content" style="color:var(--node-error)">' + escapeHtml(d.result_preview) + '</div></div>';
        }
        html += '</div>';
      });
    } else {
      html += '<div class="drawer-empty"><p>No per-call details available.</p></div>';
    }
    html += '</div>';
    return html;
  }

  function buildDrawerContent(n) {
    var html = '';

    // Stats row
    html += '<div class="drawer-stats">';
    html += '<div class="drawer-stat"><span class="stat-label">Invocations</span><span class="stat-value">' + n.invocation_count + '</span></div>';
    if (n.error_count > 0) {
      html += '<div class="drawer-stat"><span class="stat-label">Errors</span><span class="stat-value error">' + n.error_count + '</span></div>';
    } else {
      html += '<div class="drawer-stat"><span class="stat-label">Errors</span><span class="stat-value">0</span></div>';
    }
    html += '<div class="drawer-stat"><span class="stat-label">Avg Duration</span><span class="stat-value">' + n.avg_duration_ms + 'ms</span></div>';
    html += '<div class="drawer-stat"><span class="stat-label">Type</span><span class="stat-value" style="text-transform:capitalize">' + n.type + '</span></div>';
    html += '</div>';

    // Agent detail
    if (n.type === 'agent' && n.agent_detail) {
      var ad = n.agent_detail;
      html += '<div class="drawer-section"><h4>Agent Info</h4>';
      html += '<div class="drawer-stats">';
      html += '<div class="drawer-stat"><span class="stat-label">Model</span><span class="stat-value" style="font-size:12px">' + escapeHtml(ad.model_id || '?') + '</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Message</span><span class="stat-value" style="font-size:11px;font-family:monospace">' + escapeHtml((ad.message_id || '').substring(0, 16)) + '...</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Tokens</span><span class="stat-value" style="font-size:12px">' + (ad.tokens_input||0) + '+' + (ad.tokens_output||0) + '</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Cost</span><span class="stat-value" style="font-size:12px">$' + (ad.cost||0).toFixed(4) + '</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Duration</span><span class="stat-value" style="font-size:12px">' + (ad.agent_duration_ms||0) + 'ms</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Finish</span><span class="stat-value" style="font-size:12px">' + escapeHtml(ad.finish_reason || '?') + '</span></div>';
      if (ad.is_subagent) {
        html += '<div class="drawer-stat"><span class="stat-label">Subagent</span><span class="stat-value" style="font-size:12px">depth ' + ad.subagent_depth + '</span></div>';
      }
      html += '</div>';

      // Source session for subagents
      if (ad.is_subagent && ad.source_session_id) {
        var sidShort = ad.source_session_id.length > 20 ? ad.source_session_id.substring(0, 20) + '...' : ad.source_session_id;
        html += '<div style="font-size:11px;color:var(--text-dim);font-family:ui-monospace,SFMono-Regular,monospace;margin-bottom:12px">Source Session: ' + escapeHtml(sidShort) + '</div>';
      }

      // Reasoning text
      if (ad.reasoning) {
        html += '<div class="drawer-section"><h4>Reasoning</h4>';
        html += '<div class="agent-text-block"><div class="atb-content">' + escapeHtml(ad.reasoning) + '</div></div></div>';
      }

      // Output text
      if (ad.text_output) {
        html += '<div class="drawer-section"><h4>Response</h4>';
        html += '<div class="agent-text-block"><div class="atb-content">' + escapeHtml(ad.text_output) + '</div></div></div>';
      }
    }

    // Tool details
    if (n.type === 'tool' && n.details && n.details.length > 0) {
      html += '<div class="drawer-section"><h4>Invocation Details (' + n.details.length + ' calls)</h4>';
      n.details.forEach(function(d, i) {
        var cardClass = d.status === 'error' ? ' error' : '';
        html += '<div class="tool-detail-card' + cardClass + '">';
        html += '<div class="tdc-header">';
        html += '<span class="tdc-status ' + (d.status === 'error' ? 'err' : 'ok') + '">' + d.status.toUpperCase() + '</span>';
        if (d.call_id) {
          html += '<span style="font-size:10px;color:var(--text-dim);font-family:monospace">' + escapeHtml(d.call_id.substring(0, 20)) + '</span>';
        }
        html += '<span class="tdc-duration">' + d.duration_ms + 'ms</span>';
        html += '</div>';

        // Arguments
        if (d.arguments) {
          var argStr = JSON.stringify(d.arguments, null, 2);
          var argPreview = argStr.length > 500 ? argStr.substring(0, 500) + '...[truncated]' : argStr;
          html += '<div class="tdc-section"><div class="tdc-label">Arguments</div>';
          html += '<div class="tdc-content" data-full="' + escapeAttr(argStr) + '">' + escapeHtml(argPreview) + '</div>';
          if (argStr.length > 500) {
            html += '<span class="tdc-expand">Expand</span>';
          }
          html += '</div>';
        }

        // Result
        if (d.result_preview) {
          html += '<div class="tdc-section"><div class="tdc-label">Result</div>';
          var resultFull = d.result_full || d.result_preview;
          var resultPreview = d.result_preview.length > 500 ? d.result_preview.substring(0, 500) + '...[truncated]' : d.result_preview;
          html += '<div class="tdc-content" data-full="' + escapeAttr(resultFull) + '">' + escapeHtml(resultPreview) + '</div>';
          if (resultFull.length > 500) {
            html += '<span class="tdc-expand">Expand</span>';
          }
          html += '</div>';
        }

        if (d.status === 'error' && d.result_preview) {
          html += '<div class="tdc-section"><div class="tdc-label">Error</div>';
          html += '<div class="tdc-content" style="color:var(--node-error)">' + escapeHtml(d.result_preview) + '</div></div>';
        }
        html += '</div>';
      });
      html += '</div>';
    }

    // Chain (SpawnGroup) detail — parent message + spawned agents + tools
    if (n.type === 'chain' && n.chain_detail) {
      var cd = n.chain_detail;

      // Hero section
      html += '<div class="drawer-section drawer-hero">';
      html += '<div class="hero-row">';
      var roleIcon = cd.parent_role === 'user' ? '\u2709' : '\u2699';
      html += '<span class="hero-icon hero-icon-chain">' + roleIcon + '</span>';
      html += '<div class="hero-text">';
      html += '<div class="hero-kind">Spawn Group</div>';
      html += '<div class="hero-name">' + (cd.parent_role === 'user' ? 'User Message' : 'Agent Message') + '</div>';
      html += '</div></div>';
      html += '<div class="drawer-stats">';
      html += '<div class="drawer-stat"><span class="stat-label">Invocations</span><span class="stat-value">' + cd.total_invocations + '</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Total Tools</span><span class="stat-value">' + cd.total_tools + '</span></div>';
      if (cd.total_errors > 0) {
        html += '<div class="drawer-stat"><span class="stat-label">Errors</span><span class="stat-value error">' + cd.total_errors + '</span></div>';
      } else {
        html += '<div class="drawer-stat"><span class="stat-label">Errors</span><span class="stat-value">0</span></div>';
      }
      html += '<div class="drawer-stat"><span class="stat-label">Tokens</span><span class="stat-value" style="font-size:11px">\u2191' + cd.total_tokens_input + ' \u2193' + cd.total_tokens_output + '</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Cost</span><span class="stat-value" style="font-size:12px">$' + (cd.total_cost||0).toFixed(4) + '</span></div>';
      html += '<div class="drawer-stat"><span class="stat-label">Unique Agents</span><span class="stat-value">' + cd.unique_agents.length + '</span></div>';
      html += '</div></div>';

      // Parent message text (always expandable for consistent UX)
      if (cd.parent_text) {
        html += '<div class="drawer-section"><h4>Parent Message</h4>';
        var fullText = cd.parent_text;
        var textPreview = fullText.length > 500 ? fullText.substring(0, 500) + '...[truncated]' : fullText;
        html += '<div class="agent-text-block"><div class="atb-content" data-full="' + escapeAttr(fullText) + '">' + escapeHtml(textPreview) + '</div>';
        html += '<span class="tdc-expand">Expand</span>';
        html += '</div></div>';
      }

      // Spawned agents (aggregated by agent_name+model, clickable connection items)
      if (cd.children && cd.children.length > 0) {
        html += '<div class="drawer-section"><h4>Spawned Agents <span class="count-pill">' + cd.total_invocations + ' invocations</span></h4>';
        cd.children.forEach(function(c, i) {
          var childNodeId = 'agent:' + c.agent_name;
          var invTag = c.invocations > 1 ? ' \u00d7' + c.invocations : '';
          var errTag = c.error_count > 0 ? ' \u00b7 ' + c.error_count + ' err' : '';
          var hasTexts = c.invocation_texts && c.invocation_texts.length > 0;

          // Single collapsible row: click toggles response panel (no navigation)
          html += '<div class="child-row' + (hasTexts ? ' child-row-collapsible' : '') + '" data-has-texts="' + (hasTexts ? '1' : '0') + '">';
          html += '<span class="conn-icon conn-icon-agent">\u2B21</span>';
          html += '<span class="conn-text">';
          html += '<span class="conn-name">' + escapeHtml(c.agent_name) + '</span>';
          html += '<span class="conn-type">' + escapeHtml(c.model_id || '?') + ' \u00b7 ' + c.tool_count + ' tools' + invTag + errTag + '</span>';
          html += '</span>';
          if (hasTexts) {
            html += '<span class="child-chevron">\u203A</span>';
          }
          html += '</div>';

          // Collapsible: per-invocation response/reasoning texts
          if (hasTexts) {
            html += '<div class="child-responses" style="display:none;">';
            c.invocation_texts.forEach(function(t, j) {
              var msgShort = t.message_id.length > 20 ? t.message_id.substring(0, 20) + '...' : t.message_id;
              html += '<div class="response-entry">';
              html += '<div class="response-meta">#' + (j + 1) + ' \u00b7 ' + escapeHtml(msgShort) + '</div>';
              if (t.text_output) {
                html += '<div class="response-label">Response</div>';
                var fullText = t.text_output;
                var textPreview = fullText.length > 500 ? fullText.substring(0, 500) + '...[truncated]' : fullText;
                html += '<div class="response-content" data-full="' + escapeAttr(fullText) + '">' + escapeHtml(textPreview) + '</div>';
                if (fullText.length > 500) {
                  html += '<span class="tdc-expand">Expand</span>';
                }
              }
              if (t.reasoning) {
                html += '<div class="response-label">Reasoning</div>';
                var fullReasoning = t.reasoning;
                var reasoningPreview = fullReasoning.length > 500 ? fullReasoning.substring(0, 500) + '...[truncated]' : fullReasoning;
                html += '<div class="response-content" data-full="' + escapeAttr(fullReasoning) + '">' + escapeHtml(reasoningPreview) + '</div>';
                if (fullReasoning.length > 500) {
                  html += '<span class="tdc-expand">Expand</span>';
                }
              }
              if (!t.text_output && !t.reasoning) {
                html += '<div class="response-content" style="color:var(--text-dim);font-style:italic">(no response text)</div>';
              }
              html += '</div>';
            });
            html += '</div>';
          }
        });
        html += '</div>';
      }

      // MCP tools grouped by server
      if (cd.mcp_tools && cd.mcp_tools.length > 0) {
        html += '<div class="drawer-section"><h4>MCP Tools <span class="count-pill">' + cd.mcp_tools.length + '</span></h4>';
        // Group by server
        var byServer = {};
        cd.mcp_tools.forEach(function(t) {
          if (!byServer[t.server]) byServer[t.server] = [];
          byServer[t.server].push(t);
        });
        Object.keys(byServer).sort().forEach(function(server) {
          html += '<div class="mcp-server-group">';
          html += '<div class="mcp-server-label">' + escapeHtml(server) + '</div>';
          byServer[server].forEach(function(t) {
            html += renderToolWithInvocations(t);
          });
          html += '</div>';
        });
        html += '</div>';
      }

      // In-code (local) tools
      if (cd.local_tools && cd.local_tools.length > 0) {
        html += '<div class="drawer-section"><h4>In-Code Tools <span class="count-pill">' + cd.local_tools.length + '</span></h4>';
        cd.local_tools.forEach(function(t) {
          html += renderToolWithInvocations(t);
        });
        html += '</div>';
      }
    }

    if (!n.agent_detail && (!n.details || n.details.length === 0) && !n.chain_detail) {
      html += '<div class="drawer-empty"><p>No detailed data available for this node.</p></div>';
    }

    return html;
  }

  function render() {
    var container = document.getElementById('graph-container');
    var svg = el('svg', { xmlns: SVG_NS });
    container.appendChild(svg);
    svgRoot = svg;
    var dims = layout();
    build(svg, dims);
    wireInteractions(svg, dims);
  }

  render();
})();
</script>
</body>
</html>"""


def render_html(graph: SessionGraph, output_path: str) -> None:
    """Generate a self-contained session_flow.html file.

    The HTML includes embedded CSS and JavaScript for a dark-themed
    SVG topology visualization with pan, zoom, click-to-focus, and
    hover tooltips. No external dependencies.
    """
    topology = graph_to_topology(graph)
    topology_json = json.dumps(topology, ensure_ascii=False)

    # Build session info line
    n_agents = len(graph.steps)
    n_tools = graph.total_tools
    total_cost = sum(s.cost for s in graph.steps)
    cost_str = f"${total_cost:.4f}" if total_cost else "$0"
    session_info = (
        f"{graph.session_id} &middot; "
        f"{graph.title} &middot; "
        f"{n_agents} agents &middot; "
        f"{n_tools} tools &middot; "
        f"{cost_str}"
    )
    if graph.child_sessions:
        session_info += f" &middot; Subagents: {len(graph.child_sessions)}"

    # Replace placeholders — avoid f-string since JS has curly braces
    # Escape </ that would prematurely close the <script> tag (e.g. from tool results)
    safe_json = topology_json.replace("</", "<\\/")
    html = _HTML_TEMPLATE
    html = html.replace("__TITLE__", graph.title)
    html = html.replace("__SESSION_INFO__", session_info)
    html = html.replace("__TOPOLOGY_JSON__", safe_json)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
