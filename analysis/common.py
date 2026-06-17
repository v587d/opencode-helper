"""
Shared utilities for the analysis module.

Database helpers, AI invocation, and prompt rendering.
Zero external dependencies — stdlib only.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from utilities import get_db_connection, get_settings, setup_logging

log = setup_logging("analysis")
PROMPT_DIR = Path(__file__).parent / "prompts"


# ═══════════════════════════════════════════════════════════════════
#  Database helpers
# ═══════════════════════════════════════════════════════════════════

def get_messages(session_id: str) -> list[dict]:
    """Get all messages for a session with parsed JSON data.

    Each returned dict has: id, time (epoch ms), plus all JSON fields
    from message.data (role, agent, modelID, tokens, cost, finish, etc.)
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, time_created, data FROM message "
            "WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ).fetchall()
        return [{"id": r[0], "time": r[1], **json.loads(r[2])} for r in rows]
    finally:
        conn.close()


def get_parts(session_id: str) -> list[dict]:
    """Get all parts for a session with parsed JSON data.

    Each returned dict has: id, time (epoch ms), type, plus all JSON
    fields from part.data (text, tool, state, etc.)
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, time_created, data FROM part "
            "WHERE session_id = ? ORDER BY time_created",
            (session_id,),
        ).fetchall()
        results = []
        for r in rows:
            data = json.loads(r[2])
            results.append({"id": r[0], "time": r[1], "type": data.get("type", "?"), **data})
        return results
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list:
    """Run a read-only query against opencode.db and return all rows."""
    conn = get_db_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════
#  Free model discovery
# ═══════════════════════════════════════════════════════════════════

def get_free_models(providers: list[str] | None = None) -> list[dict]:
    """Dynamically query available free models from OpenCode.

    Parses ``opencode models <provider> --verbose`` output, which emits
    one provider/model_id line followed by a JSON block per model.
    Filters to models where cost.input == 0 AND cost.output == 0
    AND toolcall capability is available.

    Args:
        providers: Provider IDs to query (default: opencode, opencode-go).

    Returns:
        List of dicts with keys: key, name, context.
        Sorted by context window size descending (bigger = preferred).
    """
    providers = providers or ["opencode", "opencode-go"]
    free_models = []
    seen = set()

    for provider in providers:
        try:
            result = subprocess.run(
                ["opencode", "models", provider, "--verbose"],
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.warning("Failed to query models for provider: %s", provider)
            continue
        except Exception as e:
            log.debug("opencode models error for %s: %s", provider, e)
            continue

        lines = result.stdout.strip().split("\n")
        i = 0
        while i < len(lines):
            model_key = lines[i].strip()
            i += 1
            # Collect JSON lines until the next model line or EOF
            json_lines = []
            while i < len(lines) and lines[i].strip().startswith("{"):
                json_lines.append(lines[i])
                i += 1
            if not json_lines:
                continue
            try:
                model_data = json.loads("".join(json_lines))
            except json.JSONDecodeError:
                continue

            cost = model_data.get("cost", {})
            caps = model_data.get("capabilities", {})
            if cost.get("input", 1) == 0 and cost.get("output", 1) == 0:
                if caps.get("toolcall", False):
                    if model_key not in seen:
                        seen.add(model_key)
                        free_models.append({
                            "key": model_key,
                            "name": model_data.get("name", model_key),
                            "context": model_data.get("limit", {}).get("context", 0),
                        })

    free_models.sort(key=lambda m: m["context"], reverse=True)
    return free_models


def pick_free_model() -> str:
    """Return the best available free model key, or a hardcoded fallback."""
    models = get_free_models()
    if models:
        return models[0]["key"]
    # Last-resort fallback — will fail gracefully if unavailable
    return "opencode/mimo-v2.5-free"


def pick_analysis_model() -> str:
    """Return the model to use for AI analysis.

    Priority:
      1. ``analysis_model`` from settings.jsonc if set.
      2. Best available free model discovered via ``opencode models``.
      3. Hardcoded fallback.
    """
    configured = get_settings().get("analysis_model")
    if configured:
        log.info("Using configured analysis model: %s", configured)
        return configured
    return pick_free_model()


# ═══════════════════════════════════════════════════════════════════
#  AI invocation
# ═══════════════════════════════════════════════════════════════════


def _run_opencode(prompt: str, model: str, timeout: int) -> tuple[int, str, str]:
    """Run opencode once and return (returncode, stdout, stderr).

    Writes the full prompt to a UTF-8 .md file and passes it via ``-f``
    (attachment).  The inline message is a short ASCII instruction.
    Uses ``shell=True`` with ``>`` file redirection to capture output,
    because ``capture_output=True`` (PIPE) triggers server errors in
    opencode's internal server on Windows.
    """
    pid = os.getpid()
    base = str(Path(tempfile.gettempdir()) / "opencode")
    stdout_path = f"{base}\\och_out_{pid}.jsonl"
    stderr_path = f"{base}\\och_err_{pid}.txt"
    prompt_f_path = f"{base}\\och_prompt_{pid}.md"

    # Write the full prompt as an attached file (UTF-8, no encoding issues)
    try:
        Path(prompt_f_path).write_text(prompt, encoding="utf-8")
    except OSError:
        log.error("Failed to write prompt temp file")
        return -2, "", ""

    # Build command list, then convert to safe shell string.
    # Short inline message avoids shell encoding issues with long Unicode prompts.
    short_msg = "Analyze the attached file according to the instructions within it."
    cmd_args = [
        "opencode", "run", "--format", "json",
        "--model", model,
        "-f", prompt_f_path,
        "--dangerously-skip-permissions",
    ]
    variant = get_settings().get("analysis_variant")
    if variant:
        cmd_args.extend(["--variant", variant])
    cmd_args.append(short_msg)

    cmd_str = subprocess.list2cmdline(cmd_args)
    # Append file redirections AFTER list2cmdline quoting
    full_cmd = f'{cmd_str} >"{stdout_path}" 2>"{stderr_path}"'

    print(f"  Waiting for AI response (model: {model})...", flush=True)

    try:
        proc = subprocess.Popen(full_cmd, shell=True)
        proc.wait(timeout=timeout)
        rc = proc.returncode or 0
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
        log.error("opencode timed out after %ds", timeout)
        return -1, "", ""
    except FileNotFoundError:
        log.error("opencode not found — is OpenCode installed?")
        return -2, "", ""

    # Read captured output from temp files
    try:
        stdout = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        stdout = ""
    try:
        stderr = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr = ""

    # Clean up temp files
    for path in (stdout_path, stderr_path, prompt_f_path):
        try:
            os.unlink(path)
        except OSError:
            pass

    return rc, stdout, stderr


def _extract_response(stdout: str) -> str:
    """Extract text response from opencode JSON Lines output."""
    text_parts = []
    event_types = {}
    for line in stdout.splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        evt_type = event.get("type", "?")
        event_types[evt_type] = event_types.get(evt_type, 0) + 1
        if evt_type == "text":
            text_parts.append(event["part"].get("text", ""))

    response = "\n".join(text_parts)
    if not response:
        log.warning("No text events found. Event types received: %s", event_types)
    return response


def invoke(prompt: str, model: str | None = None, timeout: int = 300) -> str:
    """Invoke OpenCode agent for AI analysis.  Uses configured model or free model by default.

    Launches ``opencode run --format json`` as a subprocess and collects
    all ``text`` events from the JSON Lines output.

    Args:
        prompt: The analysis prompt to send.
        model: Model in ``provider/model`` format.  ``None`` = use configured model or auto-select free.
        timeout: Maximum wait time in seconds.

    Returns:
        The concatenated text response, or empty string on failure.
    """
    configured_model = get_settings().get("analysis_model")
    if model is None:
        model = configured_model or pick_free_model()
        log.info("Using analysis model: %s", model)

    returncode, stdout, stderr = _run_opencode(prompt, model, timeout)

    # If the configured model failed, fall back to a free model.
    # Only do this when the caller did not explicitly pass a model.
    if (returncode != 0 or not stdout.strip()) and model == configured_model:
        fallback = pick_free_model()
        if fallback != model:
            log.warning("Configured model failed (%s), trying free fallback: %s", model, fallback)
            returncode, stdout, stderr = _run_opencode(prompt, fallback, timeout)
            model = fallback

    if returncode != 0:
        log.error("opencode exit %d: %s", returncode, stderr[:300])
        return ""

    if not stdout.strip():
        log.error("opencode stdout empty. stderr: %s", stderr[:200] if stderr else "(none)")
        return ""

    response = _extract_response(stdout)
    if response:
        log.info("AI response: %d chars", len(response))
    return response


# ═══════════════════════════════════════════════════════════════════
#  Prompt rendering
# ═══════════════════════════════════════════════════════════════════

# Maps `analysis_language` config values to prompt instructions.
_LANG_INSTRUCTIONS: dict[str, str] = {
    "en":    "Respond in English.",
    "zh-CN": "用简体中文回复。",
    "zh-TW": "用繁體中文回覆。",
    "ja":    "日本語で回答してください。",
    "ko":    "한국어로 답변하세요.",
    "fr":    "Répondez en français.",
    "de":    "Antworten Sie auf Deutsch.",
    "es":    "Responde en español.",
    "pt":    "Responda em português.",
    "ru":    "Отвечайте на русском языке.",
}


def lang_instruction() -> str:
    """Generate the language instruction line for prompts.

    Reads ``analysis_language`` from settings.jsonc (default ``"en"``).
    If the value is a known short code, maps to a natural instruction;
    otherwise uses the value directly as a plain instruction.
    """
    lang = get_settings().get("analysis_language", "en")
    return _LANG_INSTRUCTIONS.get(lang, lang)


# ═══════════════════════════════════════════════════════════════════
#  Compression helpers (opt-in, controlled by settings.jsonc)
# ═══════════════════════════════════════════════════════════════════


def truncate(text: str, max_chars: int = 0) -> str:
    """Hard-cap a string at ``max_chars`` characters.

    Appends a ``…[+N chars]`` marker when truncation occurs.  When
    ``max_chars <= 0`` the text is returned unchanged (compression off).

    Args:
        text: Input string (may be None / non-str).
        max_chars: Maximum allowed length.  0 = passthrough.

    Returns:
        Truncated string, or the original if it fits.
    """
    if not text or max_chars <= 0:
        return text or ""
    s = str(text)
    if len(s) <= max_chars:
        return s
    # Reserve room for marker: '…[+XXXX chars]' = ~14 chars worst case
    keep = max(1, max_chars - 14)
    return s[:keep] + f"…[+{len(s) - keep} chars]"


def head_tail(text: str, head: int = 80, tail: int = 40) -> str:
    """Keep first ``head`` and last ``tail`` characters; collapse the middle.

    Designed for diagnostic error messages: the beginning (error type) and
    end (root cause / stack tail) usually matter most; the middle is often
    repetitive stack frames.  Returns the text unchanged if it's already
    short enough that sampling wouldn't help.

    Args:
        text: Input string.
        head: Number of leading characters to preserve.
        tail: Number of trailing characters to preserve.

    Returns:
        Sampled string with ``…[N chars omitted]…`` marker in the middle.
    """
    if not text:
        return text if text is None else ""
    s = str(text)
    # If text fits comfortably, just return as-is.
    if len(s) <= head + tail + 20:
        return s
    omitted = len(s) - head - tail
    return f"{s[:head]}…[{omitted} chars omitted]…{s[-tail:]}"


def dedup_errors(rows: list, prefix_len: int = 0, top_k: int = 0) -> list:
    """Collapse error rows with matching prefixes, keep top-K by total count.

    Errors that differ only in a UUID, timestamp, or path are visually
    distinct but semantically identical.  This function groups rows whose
    first ``prefix_len`` characters of the error message match (per tool)
    and sums their counts.

    Args:
        rows: List of ``(tool, error_msg, count)`` tuples.
        prefix_len: How many leading chars of error_msg to use as bucket key.
                    0 = disabled (return rows unchanged).
        top_k: Maximum number of unique patterns to keep.  0 = no limit.
               Patterns beyond top_k are folded into a single ``(other)`` row.

    Returns:
        List of ``(tool, error_msg, total_count)`` tuples.  When
        collapsing happens, a synthetic ``("(other)", "N more error
        patterns", total)`` row is appended at the end.
    """
    if prefix_len <= 0 or not rows:
        return rows
    # Bucket by (tool, prefix)
    buckets: dict[tuple, list] = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) < 3:
            continue
        tool, msg, cnt = row[0], row[1], row[2]
        prefix = (str(msg) if msg else "")[:prefix_len]
        key = (str(tool) if tool else "", prefix)
        buckets.setdefault(key, []).append((tool, msg, cnt))

    # Sort buckets by total count descending
    sorted_buckets = sorted(
        buckets.items(),
        key=lambda kv: -sum(int(r[2] or 0) for r in kv[1]),
    )

    out: list = []
    for key, group in sorted_buckets:
        if top_k > 0 and len(out) >= top_k:
            # Fold into the "other" bucket
            continue
        total = sum(int(r[2] or 0) for r in group)
        representative = group[0][1]  # first occurrence's full msg
        out.append((key[0], representative, total))

    if top_k > 0 and len(buckets) > top_k:
        other_total = sum(
            sum(int(r[2] or 0) for r in group)
            for key, group in sorted_buckets[top_k:]
        )
        out.append((
            "(other)",
            f"{len(buckets) - top_k} more error patterns",
            other_total,
        ))

    return out


def _compression_settings() -> dict:
    """Read compression settings, with safe defaults (all 0 = off)."""
    s = get_settings()
    return {
        "max_error_chars": int(s.get("analysis_max_error_chars", 0) or 0),
        "max_total_chars": int(s.get("analysis_max_total_chars", 0) or 0),
        "max_rows": int(s.get("analysis_max_rows_per_section", 0) or 0),
        "dedup_prefix": int(s.get("analysis_error_dedup_prefix", 0) or 0),
        "dedup_top_k": int(s.get("analysis_error_dedup_top_k", 0) or 0),
    }


# Section priority: lower = dropped first when prompt is over budget.
# Each entry is a (section_header_substring, command_name) tuple.
# command_name is used to pick the right priority list per subcommand.
_SECTION_PRIORITY: dict[str, list[str]] = {
    "harness": [
        "Session Lifecycle",        # large list, droppable
        "Agent/Model Switching",    # secondary
        "Session Status",           # archive stats
        "Efficiency Snapshot",      # tool/MCP/skill counts
        "Session Overview",         # KEEP — top-level numbers
    ],
    "tools": [
        "Retry Chains",             # often empty
        "Error Details",            # largest, droppable
        "Tool Distribution",        # secondary
        "Read:Edit Ratio",          # KEEP — key metric
    ],
    "mcp": [
        "Error Details",            # largest
        "MCP Tool Call Overview",   # secondary
        "Per-Server Summary",       # KEEP — rollup
    ],
    "models": [
        "Model Switching Events",   # secondary
        "Agent-Model Cross Analysis",
        "Model Usage Distribution", # KEEP — primary signal
    ],
    "skills": [
        "Skills Referenced in Prompts",
        "Shell / Platform Usage",
        "Skill Invocations",        # KEEP — primary signal
    ],
}


def enforce_budget(prompt: str, command: str = "") -> str:
    """Cap total prompt length by dropping lowest-priority sections first.

    Section boundaries are detected by lines starting with ``## `` (Markdown
    H2 headers).  When the prompt is over budget, sections are removed
    from lowest priority first.  The first section (typically the
    intro/header) is never dropped.  If still over budget after all
    droppable sections are removed, hard-truncates the remaining text.

    Disabled when ``analysis_max_total_chars <= 0`` (default).

    Args:
        prompt: The fully-rendered prompt string.
        command: Subcommand name to pick the priority list
                 (one of: harness, tools, mcp, models, skills).

    Returns:
        Prompt string, possibly with sections removed or truncated.
    """
    max_chars = int(get_settings().get("analysis_max_total_chars", 0) or 0)
    if max_chars <= 0 or len(prompt) <= max_chars:
        return prompt

    import re
    # Split on lines that start with '## ' (Markdown H2)
    parts = re.split(r"(?m)^(?=## )", prompt)
    if len(parts) <= 1:
        # No section headers found — just hard-truncate
        return truncate(prompt, max_chars)

    # First part is the intro / header — always keep
    intro = parts[0]
    sections = parts[1:]

    priority = _SECTION_PRIORITY.get(command, [])
    if not priority:
        # Unknown command — drop from the end
        return intro + "".join(sections) if len(intro + "".join(sections)) <= max_chars \
            else truncate(intro + "".join(sections), max_chars)

    # Build map: section_header_first_line -> section_text
    def section_header(sec: str) -> str:
        # sec starts with '## ', first line is the header
        first_line = sec.split("\n", 1)[0]
        return first_line.lstrip("# ").strip()

    # Try removing sections from lowest priority first
    for low_prio_name in priority:
        if len(intro) + sum(len(s) for s in sections) <= max_chars:
            break
        for i, sec in enumerate(sections):
            hdr = section_header(sec)
            # Match if priority name appears in header (fuzzy)
            if low_prio_name.lower() in hdr.lower():
                sections.pop(i)
                break

    combined = intro + "".join(sections)
    if len(combined) <= max_chars:
        return combined
    # Still over budget — hard truncate
    return truncate(combined, max_chars)


def render_prompt(name: str, **kwargs) -> str:
    """Load a prompt template from ``prompts/`` and inject data.

    Templates use Python's ``str.format()`` syntax: ``{{variable}}``.
    ``{{lang_instruction}}`` is auto-injected from the ``analysis_language``
    setting — no need to pass it explicitly.

    If ``analysis_max_total_chars`` is set in settings.jsonc, the rendered
    prompt is passed through ``enforce_budget()`` to drop lowest-priority
    sections when over budget.  Pass ``command="<name>"`` to enable
    per-subcommand priority lookup (otherwise no section dropping happens).

    Args:
        name: Template filename without extension (e.g. ``"tool_efficiency"``).
        **kwargs: Values to inject into the template.  May include
                  ``command`` (str) — subcommand name for budget priority.

    Returns:
        Rendered prompt string, or empty string if template not found.
    """
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        log.warning("Prompt template not found: %s", path)
        return ""

    template = path.read_text(encoding="utf-8")

    # Auto-inject language instruction if template uses it
    kwargs.setdefault("lang_instruction", lang_instruction())

    rendered = template.format(**kwargs)

    # Optional: enforce total prompt budget
    command = kwargs.get("command", "") or ""
    if command:
        rendered = enforce_budget(rendered, command=command)

    return rendered
