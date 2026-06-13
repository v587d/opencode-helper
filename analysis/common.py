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
