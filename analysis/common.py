"""
Shared utilities for the analysis module.

Database helpers, AI invocation, and prompt rendering.
Zero external dependencies — stdlib only.
"""

import json
import subprocess
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


# ═══════════════════════════════════════════════════════════════════
#  AI invocation
# ═══════════════════════════════════════════════════════════════════

def invoke(prompt: str, model: str | None = None, timeout: int = 300) -> str:
    """Invoke OpenCode agent for AI analysis.  Uses free model by default.

    Launches ``opencode run --format json`` as a subprocess and collects
    all ``text`` events from the JSON Lines output.

    Args:
        prompt: The analysis prompt to send.
        model: Model in ``provider/model`` format.  ``None`` = auto-select free.
        timeout: Maximum wait time in seconds.

    Returns:
        The concatenated text response, or empty string on failure.
    """
    if model is None:
        model = pick_free_model()
        log.info("Auto-selected model: %s", model)

    cmd = [
        "opencode", "run", "--format", "json",
        "--model", model,
        "--dangerously-skip-permissions",
        prompt,
    ]

    print(f"  Waiting for AI response (model: {model})...", flush=True)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.error("opencode timed out after %ds", timeout)
        return ""
    except FileNotFoundError:
        log.error("opencode not found — is OpenCode installed?")
        return ""

    if result.returncode != 0:
        log.error("opencode exit %d: %s", result.returncode, result.stderr[:300])
        return ""

    if result.stdout is None:
        log.error("opencode returned no stdout (encoding error?)")
        return ""

    stdout = result.stdout.strip()
    if not stdout:
        log.error("opencode stdout empty. stderr: %s", result.stderr[:200] if result.stderr else "(none)")
        return ""

    # Collect all event types for debugging
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
        # Fallback: try step-finish or other events that might carry content
        for line in stdout.splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "step-finish":
                reason = event.get("part", {}).get("reason", "?")
                log.info("step-finish reason: %s", reason)
    else:
        log.info("AI response: %d chars, events: %s", len(response), event_types)

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


def render_prompt(name: str, **kwargs) -> str:
    """Load a prompt template from ``prompts/`` and inject data.

    Templates use Python's ``str.format()`` syntax: ``{{variable}}``.
    ``{{lang_instruction}}`` is auto-injected from the ``analysis_language``
    setting — no need to pass it explicitly.

    Args:
        name: Template filename without extension (e.g. ``"tool_efficiency"``).
        **kwargs: Values to inject into the template.

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

    return template.format(**kwargs)
