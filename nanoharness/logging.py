"""Debug logging for NanoHarness. Logs to ~/.nanoharness/debug/<session_id>.log"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEBUG_DIR = Path.home() / ".nanoharness" / "debug"
_logger: logging.Logger | None = None
_session_id: str = ""
_session_start: float = 0.0


def _elapsed() -> str:
    """Time since session start, formatted as seconds."""
    return f"+{time.monotonic() - _session_start:.3f}s"


def init_logging(enabled: bool = False) -> str:
    """Initialize debug logging. Returns the session ID."""
    global _logger, _session_id, _session_start

    _session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    _session_start = time.monotonic()

    _logger = logging.getLogger("nanoharness")
    _logger.handlers.clear()
    _logger.propagate = False

    if enabled:
        _logger.setLevel(logging.DEBUG)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = DEBUG_DIR / f"{_session_id}.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        _logger.addHandler(handler)
        _logger.info(f"Session started: {_session_id}")
        _logger.info(f"Log file: {log_path}")
    else:
        _logger.setLevel(logging.CRITICAL + 1)  # effectively disabled

    return _session_id


def get_logger() -> logging.Logger:
    """Get the nanoharness logger. Must call init_logging first."""
    if _logger is None:
        init_logging(enabled=False)
    return _logger  # type: ignore


def log_config(config: object) -> None:
    """Log the full resolved configuration."""
    lg = get_logger()
    from dataclasses import asdict
    try:
        d = asdict(config)  # type: ignore
        # Convert Path objects to strings for JSON serialization
        def _serialize(obj: object) -> object:
            if isinstance(obj, Path):
                return str(obj)
            return obj

        def _walk(d: dict) -> dict:
            return {k: _walk(v) if isinstance(v, dict) else _serialize(v) for k, v in d.items()}

        lg.info(f"CONFIG | {json.dumps(_walk(d), indent=2)}")
    except Exception as e:
        lg.info(f"CONFIG | (serialization failed: {e}) {config}")


def log_startup(event: str, detail: str = "") -> None:
    get_logger().info(f"STARTUP | {_elapsed()} | {event} | {detail}")


def log_user_input(text: str) -> None:
    get_logger().info(f"USER_INPUT | {_elapsed()} | {text!r}")


def log_command(cmd: str, result: str) -> None:
    get_logger().info(f"COMMAND | {_elapsed()} | cmd={cmd!r} | result={result!r}")


def log_api_request(model: str, message_count: int, has_tools: bool, think: bool) -> None:
    get_logger().info(
        f"API_REQUEST | {_elapsed()} | model={model} | messages={message_count} "
        f"| tools={has_tools} | think={think}"
    )


def log_api_request_messages(messages: list[dict]) -> None:
    """Log full message payload sent to Ollama (for deep debugging)."""
    lg = get_logger()
    if not lg.isEnabledFor(logging.DEBUG):
        return
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls", [])
        tool_call_id = msg.get("tool_call_id", "")

        preview = content[:500] + ("..." if len(content) > 500 else "")
        extra = ""
        if tool_calls:
            extra = f" | tool_calls={json.dumps(tool_calls)}"
        if tool_call_id:
            extra += f" | tool_call_id={tool_call_id}"

        lg.debug(f"  MSG[{i}] | role={role} | len={len(content)}{extra} | {preview!r}")


def log_api_chunk(chunk_num: int, content: str, thinking: str, tool_calls: list, done: bool) -> None:
    """Log individual streaming chunks (only non-empty fields)."""
    lg = get_logger()
    if not lg.isEnabledFor(logging.DEBUG):
        return
    parts = [f"CHUNK[{chunk_num}]"]
    if content:
        parts.append(f"content={content!r}")
    if thinking:
        parts.append(f"thinking={thinking!r}")
    if tool_calls:
        parts.append(f"tool_calls={json.dumps(tool_calls)}")
    if done:
        parts.append("DONE")
    lg.debug(" | ".join(parts))


def log_api_response_complete(
    content: str, thinking: str, tool_calls: list,
    chunk_count: int, duration_s: float,
    eval_count: int = 0, prompt_eval_count: int = 0,
) -> None:
    """Log summary of a completed API response."""
    get_logger().info(
        f"API_RESPONSE | {_elapsed()} | duration={duration_s:.3f}s | chunks={chunk_count} "
        f"| content_len={len(content)} | thinking_len={len(thinking)} "
        f"| tool_calls={len(tool_calls)} | eval_count={eval_count} "
        f"| prompt_eval_count={prompt_eval_count}"
    )
    if tool_calls:
        for tc in tool_calls:
            func = tc.get("function", {})
            get_logger().info(
                f"  TOOL_CALL | id={tc.get('id','')} | name={func.get('name','')} "
                f"| args={json.dumps(func.get('arguments', {}))}"
            )


def log_tool_exec_start(name: str, args: dict, call_id: str) -> None:
    logged_args = args.copy()
    if name == "write_file" and "content" in logged_args:
        logged_args["content"] = f"<{len(logged_args['content'])} chars>"
    get_logger().info(f"TOOL_EXEC_START | {_elapsed()} | id={call_id} | {name} | args={json.dumps(logged_args)}")


def log_tool_exec_end(name: str, call_id: str, result: str, duration_s: float) -> None:
    preview = result[:500] + ("..." if len(result) > 500 else "")
    get_logger().info(
        f"TOOL_EXEC_END | {_elapsed()} | id={call_id} | {name} "
        f"| duration={duration_s:.3f}s | result_len={len(result)} | {preview!r}"
    )


def log_history_state(history: list[dict]) -> None:
    """Log current conversation history summary."""
    lg = get_logger()
    if not lg.isEnabledFor(logging.DEBUG):
        return
    roles = [m.get("role", "?") for m in history]
    total_chars = sum(len(m.get("content", "")) for m in history)
    lg.debug(f"HISTORY | {_elapsed()} | messages={len(history)} | total_chars={total_chars} | roles={roles}")


def log_agent_step(step: int, max_steps: int) -> None:
    get_logger().info(f"AGENT_STEP | {_elapsed()} | step={step}/{max_steps}")


def log_error(context: str, error: Exception) -> None:
    get_logger().error(f"ERROR | {_elapsed()} | {context} | {type(error).__name__}: {error}", exc_info=True)


def log_event(event_type: str, detail: str = "") -> None:
    get_logger().info(f"EVENT | {_elapsed()} | {event_type} | {detail}")
