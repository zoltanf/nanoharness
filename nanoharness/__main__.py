"""Entry point for NanoHarness: python -m nanoharness"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

try:
    import readline
except ImportError:
    readline = None  # type: ignore[assignment]

from .config import load_config
from .ollama import OllamaClient
from .startup import check_ollama, check_model, print_install_instructions
from .agent import Agent, StreamEvent
from .completion import is_incomplete_command, hint_for_input
from . import logging as log


def _setup_readline_completion(agent: Agent) -> None:
    """Set up tab completion for file/folder names in workspace + commands."""
    if readline is None:
        return
    from .completion import complete_line

    def completer(text: str, state: int) -> str | None:
        if state == 0:
            line = readline.get_line_buffer()
            completer._matches = complete_line(agent.config.workspace, line)
        matches = completer._matches
        return matches[state] if state < len(matches) else None

    completer._matches = []  # type: ignore[attr-defined]
    readline.set_completer(completer)
    readline.set_completer_delims("")  # complete full line, not tokens
    # macOS uses libedit, which needs a different binding
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


async def run_repl(agent: Agent) -> int:
    """Basic REPL for testing without TUI."""
    _setup_readline_completion(agent)
    BANNER = (
        "  _  _                  _  _\n"
        " | \\| |__ _ _ _  ___   | || |__ _ _ _ _  ___ _______\n"
        " | .` / _` | ' \\/ _ \\  | __ / _` | '_| ' \\/ -_|_-<_-<\n"
        " |_|\\_\\__,_|_||_\\___/  |_||_\\__,_|_| |_||_\\___|/__/__/"
    )
    print(BANNER)
    print()
    print(f"v0.1.0 — {agent.config.model.name} — {agent.config.workspace}")
    print(f"Thinking: {'on' if agent.config.model.thinking else 'off'} | Safety: {agent.config.safety.level}")
    if agent.config.debug:
        print("Debug logging: ON")
    print("Type /help for commands, /quit to exit.\n")

    while True:
        try:
            user_input = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0

        if not user_input:
            continue

        if is_incomplete_command(user_input):
            hint = hint_for_input(user_input)
            if hint:
                print(f"  {hint}")
            continue

        try:
            async for event in agent.process_input(user_input):
                match event.type:
                    case "content":
                        print(event.text, end="", flush=True)
                    case "thinking":
                        pass  # hide thinking in REPL
                    case "tool_call":
                        print(f"\n[tool] {event.tool_name}({event.tool_args})", flush=True)
                    case "tool_result":
                        result_preview = event.text[:200]
                        if len(event.text) > 200:
                            result_preview += "..."
                        print(f"[result] {result_preview}", flush=True)
                    case "markup":
                        from rich.console import Console
                        from rich.text import Text as RichText
                        Console().print(RichText.from_markup(event.text))
                    case "status":
                        print(event.text)
                    case "error":
                        print(f"[error] {event.text}", file=sys.stderr)
                    case "done":
                        if event.text == "quit":
                            return 0
                        print()  # newline after streamed content
        except Exception as e:
            log.log_error("repl_process_input", e)
            print(f"[error] {type(e).__name__}: {e}", file=sys.stderr)

    return 0


async def async_main() -> int:
    config, args = load_config()

    # Initialize logging early
    session_id = log.init_logging(enabled=config.debug)
    log.log_config(config)
    log.log_startup("init", f"session={session_id} debug={config.debug}")

    client = OllamaClient(base_url=config.ollama.base_url)

    try:
        # Startup checks
        if not await check_ollama(config, client):
            print_install_instructions(config)
            return 1

        if not await check_model(config, client):
            print(f"Cannot proceed without model '{config.model.name}'.")
            return 1

        # Log Ollama server diagnostics (version, running models, version mismatch)
        if config.debug:
            await client.start_log_monitor()

        log.log_startup("ready", "startup checks passed, launching UI")
        agent = Agent(config, client)

        if args.web:
            try:
                from .web import run_web
                return await run_web(
                    agent,
                    host=config.web.host,
                    port=config.web.port,
                    open_browser=config.web.open_browser,
                )
            except ImportError:
                log.log_event("web_missing_deps", "fastapi/uvicorn not installed")
                print(
                    "FastAPI or uvicorn not installed. Install with:\n"
                    "  uv sync --extra web",
                    file=sys.stderr,
                )
                return 1
        elif args.repl:
            return await run_repl(agent)
        else:
            # Try TUI, fall back to REPL
            try:
                from .tui import run_tui
                return await run_tui(agent)
            except ImportError:
                log.log_event("tui_fallback", "Textual not available")
                print("Textual not available, falling back to basic REPL.", file=sys.stderr)
                return await run_repl(agent)

    except Exception as e:
        log.log_error("async_main", e)
        print(f"Fatal error: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        await client.close()
        log.log_event("shutdown", "client closed")


def main() -> None:
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
