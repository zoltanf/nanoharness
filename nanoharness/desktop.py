"""Desktop app mode: runs FastAPI in a background thread, opens pywebview on the main thread."""

from __future__ import annotations

import asyncio
import sys
import threading
import time


def _wait_for_server(url: str, timeout: float = 10.0) -> bool:
    """Poll until the HTTP server responds or timeout expires."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=0.3)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _run_uvicorn(agent, host: str, port: int) -> None:
    """Run uvicorn with its own event loop in a daemon thread."""
    import uvicorn
    from .web import create_app

    app = create_app(agent, open_browser=False, host=host, port=port)
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    asyncio.run(server.serve())


def main_desktop() -> int:
    """
    Desktop entry point.

    Flow:
      1. Load config + run async startup checks in a worker thread
         (main thread must stay free for pywebview / WKWebView on macOS)
      2. Start uvicorn in a daemon thread
      3. Wait for the server to accept connections
      4. Open pywebview window on the main thread (blocking until closed)
    """
    from .config import load_config
    from .ollama import OllamaClient
    from .startup import check_ollama, check_model, print_install_instructions
    from .agent import Agent
    from . import logging as log

    config, _args = load_config()
    log.init_logging(enabled=config.debug)
    log.log_config(config)

    client = OllamaClient(base_url=config.ollama.base_url)

    # ── Startup checks (async, run in worker thread) ─────────────────────────
    result: dict = {}

    async def _startup() -> None:
        try:
            if not await check_ollama(config, client):
                result["error"] = "ollama"
                return
            if not await check_model(config, client):
                result["error"] = "model"
                return
            result["agent"] = Agent(config, client)
        except Exception as e:
            result["error"] = f"exception:{e}"

    t = threading.Thread(target=lambda: asyncio.run(_startup()))
    t.start()
    t.join()

    if "error" in result:
        err = result["error"]
        if err == "ollama":
            print_install_instructions(config)
        elif err == "model":
            print(f"Cannot proceed without model '{config.model.name}'.", file=sys.stderr)
        else:
            print(f"Startup failed: {err}", file=sys.stderr)
        return 1

    agent = result["agent"]

    # ── Start FastAPI server in daemon thread ─────────────────────────────────
    try:
        import uvicorn  # noqa: F401  — check dep before spawning thread
    except ImportError:
        print(
            "FastAPI/uvicorn not installed. Install with:\n  uv sync --extra app",
            file=sys.stderr,
        )
        return 1

    server_thread = threading.Thread(
        target=_run_uvicorn,
        args=(agent, config.web.host, config.web.port),
        daemon=True,
    )
    server_thread.start()

    # ── Wait for server ready ─────────────────────────────────────────────────
    url = f"http://{config.web.host}:{config.web.port}"
    if not _wait_for_server(url):
        print("Web server failed to start within 10 seconds.", file=sys.stderr)
        return 1

    # ── Open pywebview window on main thread ──────────────────────────────────
    try:
        import webview
    except ImportError:
        print(
            "pywebview not installed. Install with:\n  uv sync --extra app",
            file=sys.stderr,
        )
        return 1

    webview.create_window(
        title=f"NanoHarness — {config.model.name}",
        url=url,
        width=1200,
        height=800,
        resizable=True,
        min_size=(800, 600),
    )
    webview.start(debug=config.debug)
    # Server thread is daemon — terminates with the process
    return 0
