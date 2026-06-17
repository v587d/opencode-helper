"""
graph.py - Build agent call topology from opencode.db.

Reads the part and message tables to construct a directed graph
of agent invocations and their tool calls for a single session.

Data model:
    SessionGraph
        -> AgentStep (one assistant message = one agent invocation)
            -> ToolCall (one tool-type part within that message)

Parallel detection: messages sharing the same parentID ran concurrently.

Zero external dependencies - stdlib only.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

from utilities import get_db_connection, setup_logging

log = setup_logging("drilldown")


# ============================================================================
#  Data classes
# ============================================================================

@dataclass
class ToolCall:
    """A single tool invocation within an agent step.

    Parsed from a ``part`` row where ``data.type == "tool"``.
    """

    name: str            # tool name: read, write, bash, skill, etc.
    call_id: str         # unique call identifier from data.callID
    status: str          # "completed" | "error" | "running"
    duration_ms: int     # end - start in seconds (0 if timing data unavailable)
    time_start: int      # epoch ms from state.time.start
    arguments: dict | None = None   # state.input (raw JSON)
    result: str | None = None       # state.output (may be truncated)

    @property
    def is_error(self) -> bool:
        return self.status == "error"


@dataclass
class AgentStep:
    """One agent invocation - one message with role="assistant".

    Groups all parts (tool calls, reasoning, text output) belonging
    to this message.
    """

    message_id: str
    agent_name: str           # e.g. "Sisyphus - Ultraworker"
    model_id: str             # e.g. "deepseek-v4-pro"
    provider_id: str          # e.g. "opencode-go"
    time_start: int           # message created time (epoch ms)
    time_end: int             # message completed time (epoch ms, 0 if unknown)
    tokens_input: int
    tokens_output: int
    cost: float
    parent_message_id: Optional[str]   # which message spawned this agent
    tools: list = field(default_factory=list)    # list[ToolCall]
    reasoning: list = field(default_factory=list)    # list[str] - thinking text
    text_output: list = field(default_factory=list)  # list[str] - response text
    finish_reason: str = ""   # e.g. "stop", "length", "tool_calls"

    @property
    def agent_duration_ms(self) -> int:
        """Agent wall-clock time in seconds (time_end - time_start)."""
        return max(0, (self.time_end - self.time_start) // 1000) if self.time_end else 0

    @property
    def total_duration_ms(self) -> int:
        """Sum of all tool call durations within this step, in seconds."""
        return sum(t.duration_ms for t in self.tools)

    @property
    def error_count(self) -> int:
        return sum(1 for t in self.tools if t.is_error)

    @property
    def tools_by_status(self) -> dict[str, int]:
        """Count tools by status: {"completed": N, "error": N, ...}."""
        counts: dict[str, int] = {}
        for t in self.tools:
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts


@dataclass
class SpawnGroup:
    """A group of agent steps spawned concurrently from the same parent.

    Tracks the originating message so background tasks remain traceable
    across conversation turns — even when results arrive after the user
    has moved on to the next turn.
    """

    parent_message_id: str         # the message that spawned these children
    parent_is_user: bool           # True if parent is a user message
    child_indices: list[int]       # indices into SessionGraph.steps


@dataclass
class SessionGraph:
    """Complete agent call topology for one session.

    ``steps`` are in chronological order (by time_start).
    ``spawn_groups`` track background-task concurrency with parent
    attribution for traceability across conversation turns.
    """

    session_id: str
    title: str
    user_messages: list = field(default_factory=list)       # list[dict]
    steps: list = field(default_factory=list)                # list[AgentStep]
    spawn_groups: list = field(default_factory=list)         # list[SpawnGroup]

    @property
    def total_tools(self) -> int:
        return sum(len(s.tools) for s in self.steps)

    @property
    def total_errors(self) -> int:
        return sum(s.error_count for s in self.steps)

    @property
    def unique_agents(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for s in self.steps:
            if s.agent_name not in seen:
                seen.add(s.agent_name)
                result.append(s.agent_name)
        return result

    @property
    def total_reasoning_chars(self) -> int:
        return sum(len(r) for s in self.steps for r in s.reasoning)

    @property
    def total_output_chars(self) -> int:
        return sum(len(t) for s in self.steps for t in s.text_output)


# ============================================================================
#  Database queries
# ============================================================================

def _get_messages(session_id: str) -> list[dict]:
    """Get all messages for a session with parsed JSON data.

    Same as analysis/common.py's get_messages(), duplicated here to keep
    drilldown self-contained.

    Returns dicts with ``id``, ``msg_time`` (DB column, always int),
    plus all JSON fields from message.data (role, agent, modelID, etc.).

    Note: uses ``msg_time`` instead of ``time`` to avoid collision with
    message.data's own ``time`` field (which is a dict for user messages).
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, time_created, data FROM message "
            "WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ).fetchall()
        result: list[dict] = []
        for r in rows:
            msg = {"id": r[0], "msg_time": r[1]}
            msg.update(json.loads(r[2]))
            result.append(msg)
        return result
    finally:
        conn.close()


def _get_parts_with_message(session_id: str) -> list[dict]:
    """Get all parts for a session, including message_id for linking.

    Unlike analysis/common.py's get_parts(), this includes
    ``message_id`` so parts can be grouped by their parent message.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, message_id, time_created, data FROM part "
            "WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ).fetchall()
        results: list[dict] = []
        for r in rows:
            data = json.loads(r[3])
            results.append({
                "id": r[0],
                "message_id": r[1],
                "time": r[2],
                "type": data.get("type", "?"),
                **data,
            })
        return results
    finally:
        conn.close()


def _get_session_info(session_id: str) -> dict | None:
    """Get session title and basic metadata."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id, title, time_created FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row:
            return {"id": row[0], "title": row[1] or "(untitled)", "time_created": row[2]}
        return None
    finally:
        conn.close()


# ============================================================================
#  Parsing helpers
# ============================================================================

def _parse_tool_call(part: dict) -> ToolCall:
    """Parse a tool-type part into a ToolCall.

    Extracts timing from ``state.time.start`` / ``state.time.end``
    (both epoch ms), plus input arguments and output result from
    ``state.input`` / ``state.output``.

    ``result`` is stringified (tool output can be a nested dict).
    """
    state = part.get("state", {})
    timing = state.get("time", {}) if isinstance(state, dict) else {}
    start = timing.get("start", 0) if isinstance(timing, dict) else 0
    end = timing.get("end", 0) if isinstance(timing, dict) else 0
    duration = max(0, (end - start) // 1000) if (start and end) else 0

    # Tool input / output (may be dict, list, or string)
    raw_input = state.get("input") if isinstance(state, dict) else None
    raw_output = state.get("output") if isinstance(state, dict) else None

    # Keep dict/list as-is; stringify only for non-JSON-safe values
    arguments = raw_input if isinstance(raw_input, (dict, list)) else None
    if raw_output is None:
        result = None
    elif isinstance(raw_output, str):
        result = raw_output
    else:
        result = json.dumps(raw_output, ensure_ascii=False)

    return ToolCall(
        name=part.get("tool", "unknown"),
        call_id=part.get("callID", part.get("id", "")),
        status=state.get("status", "unknown") if isinstance(state, dict) else "unknown",
        duration_ms=duration,
        time_start=start or part.get("time", 0),
        arguments=arguments,
        result=result,
    )


def _parse_agent_step(msg: dict, all_parts: list[dict]) -> AgentStep:
    """Build an AgentStep from a message and all its parts.

    Parses tool calls from ``type=tool`` parts, reasoning text from
    ``type=reasoning`` parts, and response text from ``type=text`` parts.

    Also extracts timing (created/completed), finish reason, and tokens.
    """
    tokens = msg.get("tokens", {}) or {}
    model_id = msg.get("modelID", "")
    provider_id = msg.get("providerID", "")
    if not model_id:
        model_info = msg.get("model", {}) or {}
        model_id = model_info.get("modelID", "unknown")
        provider_id = model_info.get("providerID", "")

    # Time: assistant messages store time as {"created": ms, "completed": ms}
    time_val = msg.get("time", {}) or {}
    time_start = msg.get("msg_time", 0)
    time_end = 0
    if isinstance(time_val, dict):
        time_start = time_start or time_val.get("created", 0) or 0
        time_end = time_val.get("completed", 0) or 0

    # Finish reason: "stop", "length", "tool_calls", etc.
    finish = msg.get("finish", "")
    if isinstance(finish, str):
        finish_reason = finish
    elif isinstance(finish, dict):
        finish_reason = finish.get("reason", "")
    else:
        finish_reason = str(finish) if finish else ""

    # Separate parts by type
    tool_parts: list[dict] = []
    reasoning_texts: list[str] = []
    output_texts: list[str] = []

    for p in all_parts:
        ptype = p.get("type", "")
        if ptype == "tool":
            tool_parts.append(p)
        elif ptype == "reasoning":
            text = p.get("text", "")
            if text:
                reasoning_texts.append(str(text))
        elif ptype == "text":
            text = p.get("text", "")
            if text:
                output_texts.append(str(text))

    return AgentStep(
        message_id=msg["id"],
        agent_name=msg.get("agent", "unknown"),
        model_id=model_id,
        provider_id=provider_id,
        time_start=time_start,
        time_end=time_end,
        tokens_input=tokens.get("input", 0) or 0,
        tokens_output=tokens.get("output", 0) or 0,
        cost=msg.get("cost", 0.0) or 0.0,
        parent_message_id=msg.get("parentID"),
        tools=[_parse_tool_call(p) for p in tool_parts],
        reasoning=reasoning_texts,
        text_output=output_texts,
        finish_reason=finish_reason,
    )


def _detect_spawn_groups(steps: list, messages: list[dict]) -> list[SpawnGroup]:
    """Find groups of steps spawned concurrently from the same parent.

    Builds a parentID -> child-indices map, then enriches each group
    with the parent message's role (user vs assistant) so downstream
    rendering can show *who* spawned the background tasks.

    Groups of size 1 are excluded — a single child is sequential, not a
    concurrent spawn.
    """
    # Build a quick role lookup from messages
    msg_roles: dict[str, str] = {}
    for m in messages:
        msg_roles[m["id"]] = m.get("role", "")

    parent_map: dict[str, list[int]] = {}
    for i, step in enumerate(steps):
        pid = step.parent_message_id
        if pid:
            parent_map.setdefault(pid, []).append(i)

    groups: list[SpawnGroup] = []
    for parent_id, indices in parent_map.items():
        if len(indices) >= 2:
            role = msg_roles.get(parent_id, "")
            groups.append(SpawnGroup(
                parent_message_id=parent_id,
                parent_is_user=(role == "user"),
                child_indices=indices,
            ))
    return groups


# ============================================================================
#  Graph builder (public API)
# ============================================================================

def build_graph(session_id: str) -> SessionGraph | None:
    """Build the agent call topology for a single session.

    Queries the session, message, and part tables, then assembles:
    - User messages (role="user")
    - Agent steps (role="assistant") with their tool calls
    - Parallel groups (concurrent agent invocations)

    Returns None if the session_id does not exist.
    """
    info = _get_session_info(session_id)
    if info is None:
        log.warning("Session not found: %s", session_id)
        return None

    messages = _get_messages(session_id)
    parts = _get_parts_with_message(session_id)

    # Index parts by message_id for fast lookup
    parts_by_msg: dict[str, list[dict]] = {}
    for p in parts:
        mid = p["message_id"]
        parts_by_msg.setdefault(mid, []).append(p)

    # Separate user messages from assistant messages
    user_msgs: list[dict] = []
    agent_steps: list[AgentStep] = []

    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            # Extract prompt text from user message's parts
            msg_parts = parts_by_msg.get(msg["id"], [])
            text_parts = [p.get("text", "") for p in msg_parts if p.get("type") == "text"]
            user_msg = dict(msg)  # shallow copy to avoid mutating original
            user_msg["text"] = "\n".join(text_parts) if text_parts else ""
            user_msgs.append(user_msg)
        elif role == "assistant":
            msg_parts = parts_by_msg.get(msg["id"], [])
            agent_steps.append(_parse_agent_step(msg, msg_parts))

    spawn_groups = _detect_spawn_groups(agent_steps, messages)

    graph = SessionGraph(
        session_id=session_id,
        title=info["title"],
        user_messages=user_msgs,
        steps=agent_steps,
        spawn_groups=spawn_groups,
    )

    log.info(
        "Session %s: %d users, %d agents, %d tools (%d err), "
        "%d spawn groups, %d reasoning chars, %d output chars",
        session_id, len(user_msgs), len(agent_steps),
        graph.total_tools, graph.total_errors, len(spawn_groups),
        graph.total_reasoning_chars, graph.total_output_chars,
    )
    return graph


# ============================================================================
#  Convenience: session listing
# ============================================================================

def get_latest_session_id() -> str | None:
    """Return the ID of the most recent session that has parts."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT s.id FROM session s "
            "JOIN part p ON p.session_id = s.id "
            "GROUP BY s.id ORDER BY s.time_created DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def list_sessions(limit: int = 20) -> list[dict]:
    """List recent sessions with part count, for ``--list``."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT s.id, s.title, s.time_created, COUNT(p.id) as part_count "
            "FROM session s "
            "LEFT JOIN part p ON p.session_id = s.id "
            "GROUP BY s.id ORDER BY s.time_created DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1] or "(untitled)",
                "time_created": r[2],
                "part_count": r[3],
            }
            for r in rows
        ]
    finally:
        conn.close()
