"""FastAPI web UI for NanoHarness. WebSocket + SSE fallback for chat streaming."""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import uuid
import webbrowser
from contextlib import asynccontextmanager
from typing import AsyncIterator, TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import logging as log, BANNER as _BANNER, __version__
from .config import WARN_SAFETY_NONE, WARN_DEBUG_ON, TOOL_NAMES, write_config_toml
from .tools import format_confirm_preview

if TYPE_CHECKING:
    from .agent import Agent, StreamEvent



def create_app(
    agent: Agent,
    open_browser: bool = True,
    host: str = "127.0.0.1",
    port: int = 8321,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ollama_version = await agent.client.get_version()
        if open_browser:
            webbrowser.open(f"http://{host}:{port}")
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.agent = agent
    app.state.processing = False

    _origin_re = re.compile(
        rf"^https?://(localhost|127\.0\.0\.1|{re.escape(host)}):{port}$"
    )

    def _origin_allowed(origin: str) -> bool:
        """Return True for local origins or absent Origin (non-browser clients)."""
        return not origin or bool(_origin_re.match(origin))

    # Pre-render HTML template (config values are static for the server lifetime)
    cfg = agent.config
    cached_html = (
        HTML_TEMPLATE
        .replace("__MODEL_NAME_JSON__", json.dumps(cfg.model.name))
        .replace("__WORKSPACE_JSON__", json.dumps(str(cfg.workspace)))
        .replace("__MODEL_NAME__", _html.escape(cfg.model.name))
        .replace("__THINKING__", "on" if cfg.model.thinking else "off")
        .replace("__THINKING_ENABLED__", "true" if cfg.model.thinking else "false")
        .replace("__SAFETY__", cfg.safety.level)
        .replace("__WS_PORT__", str(port))
        .replace("__WORKSPACE__", _html.escape(str(cfg.workspace)))
        .replace("__BANNER__", json.dumps(_BANNER))
        .replace("__VERSION__", json.dumps(__version__))
        .replace("__OLLAMA_URL__", json.dumps(str(cfg.ollama.base_url)))
        .replace("__TOOL_NAMES_JSON__", json.dumps(TOOL_NAMES))
        .replace("__SAFETY_WARNING_JSON__", json.dumps(WARN_SAFETY_NONE if cfg.safety.level == "none" else ""))
        .replace("__DEBUG_WARNING_JSON__", json.dumps(WARN_DEBUG_ON if cfg.debug else ""))
    )

    _html_by_theme = {
        "auto":  cached_html.replace("__THEME_ATTR__", ""),
        "light": cached_html.replace("__THEME_ATTR__", 'data-theme="light"'),
        "dark":  cached_html.replace("__THEME_ATTR__", 'data-theme="dark"'),
    }

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _html_by_theme.get(agent.config.ui.theme, _html_by_theme["auto"])

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        origin = websocket.headers.get("origin", "")
        if not _origin_allowed(origin):
            log.log_event("ws_rejected", f"bad origin: {origin!r}")
            await websocket.accept()
            await websocket.close(code=1008)
            return
        await websocket.accept()
        log.log_event("ws_connect", "client connected")

        # Per-connection confirm futures: id → Future[bool]
        _pending_confirms: dict[str, asyncio.Future] = {}

        async def web_confirm(tool_name: str, args: dict) -> bool:
            req_id = str(uuid.uuid4())
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            _pending_confirms[req_id] = future
            await websocket.send_json({
                "type": "confirm_request",
                "id": req_id,
                "tool": tool_name,
                "preview": format_confirm_preview(tool_name, args),
            })
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=120)
            except asyncio.TimeoutError:
                _pending_confirms.pop(req_id, None)
                return False

        agent.tools.confirm_fn = web_confirm

        _agent_task: asyncio.Task | None = None

        async def _run_agent(text: str) -> None:
            app.state.processing = True
            log.log_event("ws_input", text)
            try:
                async for ev in agent.process_input(text):
                    await websocket.send_json(ev.to_dict())
            except Exception as e:
                log.log_error("ws_process", e)
                await websocket.send_json({"type": "error", "text": f"{type(e).__name__}: {e}"})
            finally:
                app.state.processing = False

        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 1_000_000:
                    await websocket.close(code=1009)
                    return
                data = json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "confirm_response":
                    req_id = data.get("id", "")
                    fut = _pending_confirms.pop(req_id, None)
                    if fut and not fut.done():
                        fut.set_result(bool(data.get("allowed", False)))
                    continue

                if msg_type == "interrupt":
                    if _agent_task and not _agent_task.done():
                        _agent_task.cancel()
                    await websocket.send_json({"type": "done", "text": ""})
                    continue

                if msg_type != "input":
                    continue
                text = data.get("text", "").strip()
                if not text:
                    continue

                if app.state.processing:
                    await websocket.send_json({"type": "error", "text": "Still processing..."})
                    continue

                _agent_task = asyncio.create_task(_run_agent(text))

        except WebSocketDisconnect:
            log.log_event("ws_disconnect", "client disconnected")
            if _agent_task and not _agent_task.done():
                _agent_task.cancel()
            # Auto-deny all future confirms so in-flight tools don't execute
            async def _auto_deny(tool_name: str, args: dict) -> bool:
                return False
            agent.tools.confirm_fn = _auto_deny
            # Resolve any pending confirms so the agent task doesn't hang
            for fut in _pending_confirms.values():
                if not fut.done():
                    fut.set_result(False)

    @app.post("/api/chat")
    async def chat_sse(request: Request):
        """SSE fallback for environments that don't support WebSocket (e.g. proxies)."""
        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            return StreamingResponse(
                iter(["data: " + json.dumps({"type": "error", "text": "empty input"}) + "\n\n"]),
                media_type="text/event-stream",
            )

        if app.state.processing:
            return StreamingResponse(
                iter(["data: " + json.dumps({"type": "error", "text": "Still processing..."}) + "\n\n"]),
                media_type="text/event-stream",
            )

        async def event_stream() -> AsyncIterator[str]:
            app.state.processing = True
            log.log_event("sse_input", text)
            try:
                async for ev in agent.process_input(text):
                    yield f"data: {json.dumps(ev.to_dict())}\n\n"
            except Exception as e:
                log.log_error("sse_process", e)
                yield f"data: {json.dumps({'type': 'error', 'text': f'{type(e).__name__}: {e}'})}\n\n"
            finally:
                app.state.processing = False

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/shutdown")
    async def shutdown(request: Request):
        """Shut down the server (used by /quit in app mode)."""
        origin = request.headers.get("origin", "")
        if not _origin_allowed(origin):
            log.log_event("shutdown_rejected", f"bad origin: {origin!r}")
            raise HTTPException(status_code=403, detail="Forbidden")
        import os, signal
        asyncio.get_event_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        return {"ok": True}

    @app.get("/api/version")
    async def version_info():
        """Return Ollama version for the welcome banner (fetched once at startup)."""
        return {"version": app.state.ollama_version, "url": str(cfg.ollama.base_url)}

    @app.get("/api/status")
    async def status_info():
        """Return dynamic status bar data: context tokens and todo stats."""
        ag = app.state.agent
        next_task, progress = ag.tools.get_todo_parts()
        return {
            "ctx_used": ag.last_prompt_tokens,
            "ctx_max": ag.context_size,
            "next_task": next_task,
            "progress": progress,
        }

    @app.get("/api/config/tools")
    async def get_tools_config():
        """Return current global and workspace tool enable/disable state."""
        states = agent.tools.get_tool_states(agent.config.tools)
        return {"tools": {
            name: {"global": s["global"], "workspace": s["workspace"]}
            for name, s in states.items()
        }}

    @app.post("/api/config/tools")
    async def set_tools_config(request: Request):
        """Save global and workspace tool enable/disable state."""
        body = await request.json()
        tools_map = body.get("tools", {})
        tools_cfg = agent.config.tools
        ws_update: dict[str, bool | None] = {}
        global_changed = False
        for name in TOOL_NAMES:
            entry = tools_map.get(name)
            if entry is None:
                continue
            if isinstance(entry.get("global"), bool):
                setattr(tools_cfg, name, entry["global"])
                global_changed = True
            if "workspace" in entry:
                ws_update[name] = entry["workspace"]
        if global_changed:
            write_config_toml(agent.config)
        agent.tools.set_workspace_tools(ws_update)
        states = agent.tools.get_tool_states(agent.config.tools)
        return {"tools": {
            name: {"global": s["global"], "workspace": s["workspace"]}
            for name, s in states.items()
        }}

    return app


async def run_web(
    agent: Agent,
    host: str = "127.0.0.1",
    port: int = 8321,
    open_browser: bool = True,
) -> int:
    import uvicorn

    app = create_app(agent, open_browser=open_browser, host=host, port=port)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
    return 0


# ---------------------------------------------------------------------------
# Inline HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" __THEME_ATTR__>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NanoHarness</title>
<style>
  :root {
    --bg: #0d1117; --bg2: #161b22; --bg3: #1c2129;
    --fg: #c9d1d9; --fg-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --cyan: #39c5cf; --border: #30363d; --bubble: #1a3a4a; --bubble-border: #264a5a;
    --radius: 8px; --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --mono: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    --btn-fg: #ffffff;
  }
  /* Light theme — follows system preference when no manual override */
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg: #ffffff; --bg2: #f6f8fa; --bg3: #eef1f5;
      --fg: #1f2328; --fg-dim: #656d76; --accent: #0969da;
      --green: #1a7f37; --yellow: #9a6700; --red: #cf222e;
      --cyan: #0550ae; --border: #d0d7de; --bubble: #ddf4ff; --bubble-border: #b6e3ff;
      --btn-fg: #ffffff;
    }
  }
  /* Manual light override (dark is the :root default, no override needed) */
  [data-theme="light"] {
    --bg: #ffffff; --bg2: #f6f8fa; --bg3: #eef1f5;
    --fg: #1f2328; --fg-dim: #656d76; --accent: #0969da;
    --green: #1a7f37; --yellow: #9a6700; --red: #cf222e;
    --cyan: #0550ae; --border: #d0d7de; --bubble: #ddf4ff; --bubble-border: #b6e3ff;
    --btn-fg: #ffffff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--fg); font-family: var(--font); font-size: 14px; height: 100vh; overflow: clip; }

  /* Chat area — full viewport height so scrollbar spans the whole window */
  #chat { height: 100vh; overflow-y: auto; padding: 16px 48px 140px; display: flex; flex-direction: column; align-items: center; gap: 12px; }
  #chat > * { width: 100%; max-width: 796px; }
  /* Floor strip — blocks chat content from showing in the gap below the fixed panel */
  #chat-floor { position: fixed; bottom: 0; left: 0; right: 0; height: 16px; background: var(--bg); z-index: 99; }

  /* Bottom panel — fixed overlay at the bottom so it doesn't shrink the scroll track */
  #bottom-panel { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); max-width: 860px; width: calc(100% - 32px); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--bg); z-index: 100; }

  /* Status bar — flex row of column cells with right-border separators */
  #status { display: flex; align-items: stretch; font-size: 11px; border-top: 1px solid var(--border); background: var(--bg2); }
  .s-col { display: flex; flex-direction: column; gap: 2px; padding: 10px 14px; border-right: 1px solid var(--border); justify-content: center; }
  .s-col:last-child { border-right: none; }
  .s-col-fill { flex: 1; min-width: 0; }
  .s-col-right { text-align: right; }
  .s-fill { display: flex; gap: 12px; }
  #status .model { color: var(--accent); font-weight: 600; }
  .s-val { color: var(--fg); font-weight: 600; }
  .s-lbl { color: var(--fg-dim); font-size: 10px; }
  .think-on { color: var(--green); }
  .think-once { color: var(--yellow); }
  .think-off { color: var(--fg-dim); }
  .ctx-warn { color: var(--red); font-weight: 600; }

  /* Messages */
  .msg-user { display: flex; flex-direction: column; align-items: flex-end; margin-top: 12px; }
  .msg-user .bubble { max-width: 66%; background: var(--bubble); border: 1px solid var(--bubble-border); border-radius: var(--radius); padding: 8px 14px; }
  .msg-user .label { color: var(--cyan); font-size: 12px; font-weight: 600; margin-bottom: 4px; }

  .msg-assistant { padding: 4px 8px; }
  .msg-assistant .content { line-height: 1.6; }
  .msg-assistant .content p { margin: 0.4em 0; }
  .msg-assistant .content pre { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; overflow-x: auto; margin: 0.6em 0; }
  .msg-assistant .content code { font-family: var(--mono); font-size: 13px; }
  .msg-assistant .content :not(pre) > code { background: var(--bg2); padding: 2px 6px; border-radius: 4px; }
  .msg-assistant .content h1, .msg-assistant .content h2, .msg-assistant .content h3 { color: var(--accent); margin: 0.6em 0 0.3em; }
  .msg-assistant .content ul, .msg-assistant .content ol { padding-left: 1.5em; }
  .msg-assistant .content a { color: var(--accent); }
  .msg-assistant .content table { border-collapse: separate; border-spacing: 0; margin: 0.6em 0; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .msg-assistant .content th, .msg-assistant .content td { border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 6px 12px; }
  .msg-assistant .content th:last-child, .msg-assistant .content td:last-child { border-right: none; }
  .msg-assistant .content tr:last-child td { border-bottom: none; }
  .msg-assistant .content th { background: var(--bg3); font-weight: 600; color: var(--fg); }
  .msg-assistant .content blockquote { border-left: 3px solid var(--border); padding-left: 12px; color: var(--fg-dim); }

  /* Tool call/result */
  .tool-call { border-left: 3px solid var(--yellow); background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; padding: 8px 12px; font-family: var(--mono); font-size: 13px; }
  .tool-call .name { color: var(--yellow); font-weight: 600; }
  .tool-call .args { color: var(--fg-dim); }
  .tool-result { border-left: 3px solid var(--green); background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; padding: 8px 12px; font-family: var(--mono); font-size: 12px; color: var(--green); white-space: pre-wrap; word-break: break-all; }
  /* Paired call+result share a .tool-group container (single flex item → no gap between them) */
  .tool-group > .tool-call { border-radius: 0 var(--radius) 0 0; }
  .tool-group > .tool-result { border-radius: 0 0 var(--radius) 0; }

  /* Thinking */
  .thinking { color: var(--fg-dim); font-style: italic; font-size: 13px; }
  .thinking summary { cursor: pointer; user-select: none; }
  .thinking pre { white-space: pre-wrap; margin-top: 4px; font-family: var(--mono); font-size: 12px; }

  /* Status/error messages */
  .msg-status { color: var(--fg-dim); font-size: 13px; font-style: italic; white-space: pre-wrap; font-family: var(--mono); }
  .msg-error { color: var(--red); font-weight: 600; }
  .msg-progress { color: var(--fg-dim); font-size: 13px; font-family: var(--mono); white-space: pre; overflow: hidden; text-overflow: ellipsis; }
  .msg-warning { color: var(--yellow); font-weight: 600; font-size: 13px; font-family: var(--mono); white-space: pre-wrap; }

  /* Welcome banner */
  .banner { font-family: var(--mono); font-size: 12px; color: var(--green); line-height: 1.4; white-space: pre; }
  .banner-meta { font-family: var(--mono); font-size: 12px; color: var(--fg-dim); margin-top: 4px; }

  /* Command markdown output */
  .msg-markdown { line-height: 1.6; }
  .msg-markdown p { margin: 0.4em 0; }
  .msg-markdown pre { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; overflow-x: auto; margin: 0.6em 0; }
  .msg-markdown code { font-family: var(--mono); font-size: 13px; }
  .msg-markdown :not(pre) > code { background: var(--bg2); padding: 2px 6px; border-radius: 4px; }
  .msg-markdown h1, .msg-markdown h2, .msg-markdown h3 { color: var(--accent); margin: 0.6em 0 0.3em; }
  .msg-markdown ul, .msg-markdown ol { padding-left: 1.5em; margin: 0.3em 0; }
  .msg-markdown li { margin: 0.15em 0; }
  .msg-markdown table { border-collapse: separate; border-spacing: 0; margin: 0.6em 0; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .msg-markdown th, .msg-markdown td { border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 6px 12px; }
  .msg-markdown th:last-child, .msg-markdown td:last-child { border-right: none; }
  .msg-markdown tr:last-child td { border-bottom: none; }
  .msg-markdown th { background: var(--bg3); font-weight: 600; color: var(--fg); }

  /* Spinner */
  .spinner { display: flex; align-items: center; gap: 7px; color: var(--fg-dim); font-size: 12px; padding: 4px 0; }
  .spinner-ring { width: 12px; height: 12px; border: 1.5px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; flex-shrink: 0; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Hint line — border only when visible so it doesn't bleed through as a phantom line when collapsed */
  #hint-line { padding: 0 16px; font-size: 13px; color: var(--fg-dim); font-family: var(--mono); overflow: hidden; transition: max-height 0.1s, padding 0.1s; }
  #hint-line.visible { max-height: 24px; padding: 4px 16px; border-bottom: 1px solid var(--border); }
  #hint-line.hidden { max-height: 0; padding: 0 16px; }

  /* Input area — left padding 0 so textarea text aligns with status bar column text at 14px */
  #input-area { padding: 6px 12px 6px 0; display: flex; align-items: center; gap: 8px; }
  #input-area textarea { flex: 1; background: transparent; color: var(--fg); border: none; padding: 6px 14px; font-family: var(--font); font-size: 14px; outline: none; resize: none; min-height: 32px; max-height: 200px; overflow-y: hidden; line-height: 1.5; }
  #input-area textarea::placeholder { color: var(--fg-dim); }
  #input-area button { background: var(--accent); color: var(--btn-fg); border: none; border-radius: var(--radius); width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; font-weight: 700; cursor: pointer; font-size: 18px; line-height: 1; flex-shrink: 0; }
  #input-area button:hover { opacity: .9; }
  #input-area button:disabled { opacity: .4; cursor: default; }

  /* ── Confirm Modal ──────────────────────────────────── */
  #confirm-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: flex; align-items: center; justify-content: center;
    z-index: 9999;
  }
  .c-box {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 24px 28px;
    max-width: 600px; width: 90%;
    box-shadow: 0 8px 32px rgba(0,0,0,0.35);
  }
  .c-title { font-weight: 700; font-size: 15px; color: var(--accent); margin-bottom: 12px; }
  .c-preview {
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    padding: 10px 14px; white-space: pre-wrap; word-break: break-word;
    color: var(--fg); font-family: var(--mono); font-size: 13px;
    max-height: 200px; overflow-y: auto; margin-bottom: 16px;
  }
  .c-buttons { display: flex; gap: 12px; }
  .c-allow, .c-deny {
    padding: 8px 18px; border-radius: var(--radius); border: none;
    cursor: pointer; font-size: 13px; font-weight: 600; color: #fff;
  }
  .c-allow { background: var(--green); }
  .c-allow:hover { opacity: .85; }
  .c-deny  { background: var(--red); }
  .c-deny:hover  { opacity: .85; }

  /* ── Tools Config Modal ─────────────────────────────── */
  .t-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000;
  }
  .t-box {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px 24px;
    min-width: 340px; max-width: 500px; width: 90%;
    box-shadow: 0 8px 32px rgba(0,0,0,0.35);
  }
  .t-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 16px;
  }
  .t-title { font-weight: 700; font-size: 15px; color: var(--accent); }
  .t-close {
    background: none; border: none; color: var(--fg-dim); cursor: pointer;
    font-size: 16px; padding: 2px 6px; border-radius: 4px; line-height: 1;
  }
  .t-close:hover { color: var(--fg); background: var(--bg3); }
  .t-table { width: 100%; border-collapse: collapse; }
  .t-table th {
    text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .04em; color: var(--fg-dim); padding: 4px 8px 8px;
    border-bottom: 1px solid var(--border);
  }
  .t-col-tool { width: 55%; }
  .t-col-toggle { width: 22.5%; text-align: center; }
  .t-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  .t-table tr:last-child td { border-bottom: none; }
  .t-tool-name { font-family: var(--mono); font-size: 13px; }
  /* 2-state toggle (Global) */
  .t-btn {
    display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; border-radius: 20px; padding: 3px 12px;
    font-size: 12px; font-weight: 600; min-width: 44px;
    border: none; transition: background 0.15s, opacity 0.15s; outline: none;
  }
  .t-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .t-on  { background: var(--green); color: #fff; }
  .t-off { background: var(--red);   color: #fff; }
  /* 3-state toggle (Workspace) */
  .t-ws {
    display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; border-radius: 20px; padding: 3px 10px;
    font-size: 12px; font-weight: 600; min-width: 60px;
    border: 1px solid var(--border); transition: background 0.15s; outline: none;
  }
  .t-ws:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .t-ws-on      { background: var(--green); color: #fff; border-color: var(--green); }
  .t-ws-off     { background: var(--red);   color: #fff; border-color: var(--red); }
  .t-ws-inherit { background: var(--bg3);   color: var(--fg-dim); }
  .t-footer { margin-top: 14px; font-size: 11px; color: var(--fg-dim); line-height: 1.5; }
</style>
</head>
<body>

<div id="chat"></div>

<div id="bottom-panel">
  <div id="hint-line" class="hidden"></div>
  <div id="input-area">
    <textarea id="input" rows="1" placeholder="Tell me about this project" autocomplete="off" autofocus></textarea>
    <button id="send" onclick="sendMessage()" aria-label="Send"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M8 13V3M8 3L3 8M8 3L13 8" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
  </div>
  <div id="status">
    <div class="s-col">
      <span class="s-val model">__MODEL_NAME__</span>
      <span class="s-lbl">model</span>
    </div>
    <div class="s-col">
      <span class="s-val" id="status-ctx">– / –</span>
      <span class="s-lbl">context</span>
    </div>
    <div class="s-col">
      <span class="s-val think-__THINKING__" id="status-thinking">__THINKING__</span>
      <span class="s-lbl">think</span>
    </div>
    <div class="s-col s-col-fill">
      <div class="s-fill">
        <span id="status-workspace">__WORKSPACE__</span>
        <span id="status-todo-val" style="display:none"></span>
      </div>
      <div class="s-fill">
        <span id="status-safety" class="s-lbl">safety:__SAFETY__</span>
        <span id="status-todo-lbl" style="display:none"></span>
      </div>
    </div>
  </div>
</div>
<div id="chat-floor"></div>
<span id="status-ws" style="display:none">connecting...</span>

<!-- Tools config modal -->
<div id="tools-modal-overlay" class="t-overlay" style="display:none" onclick="handleToolsOverlayClick(event)">
  <div id="tools-modal" class="t-box" role="dialog" aria-modal="true" aria-label="Tool Configuration">
    <div class="t-header">
      <span class="t-title">Tool Configuration</span>
      <button class="t-close" onclick="closeToolsModal()" aria-label="Close">&#x2715;</button>
    </div>
    <table class="t-table">
      <thead><tr>
        <th class="t-col-tool">Tool</th>
        <th class="t-col-toggle">Global</th>
        <th class="t-col-toggle">Workspace</th>
      </tr></thead>
      <tbody id="tools-tbody"></tbody>
    </table>
    <div class="t-footer">
      Global: click to toggle on/off &nbsp;&middot;&nbsp; Workspace: click to cycle on / off / inherit
    </div>
  </div>
</div>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusThinking = document.getElementById('status-thinking');
const statusCtx = document.getElementById('status-ctx');

function fmtCtx(n) { return n >= 1000 ? Math.floor(n / 1000) + 'k' : String(n); }

async function updateStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (statusCtx) {
      const usedStr = d.ctx_used > 0 ? fmtCtx(d.ctx_used) : '–';
      const maxStr = d.ctx_max > 0 ? fmtCtx(d.ctx_max) : '?';
      statusCtx.textContent = usedStr + ' / ' + maxStr;
      statusCtx.className = (d.ctx_max > 0 && d.ctx_used / d.ctx_max > 0.70) ? 'ctx-warn' : '';
    }
    const valEl = document.getElementById('status-todo-val');
    const lblEl = document.getElementById('status-todo-lbl');
    if (d.progress) {
      if (valEl && d.next_task) { valEl.textContent = 'Next: ' + d.next_task; valEl.style.display = ''; }
      else if (valEl) valEl.style.display = 'none';
      if (lblEl) { lblEl.textContent = d.progress; lblEl.style.display = ''; }
    } else {
      if (valEl) { valEl.textContent = ''; valEl.style.display = 'none'; }
      if (lblEl) { lblEl.textContent = ''; lblEl.style.display = 'none'; }
    }
  } catch(e) {}
}
updateStatus();

let ws;
let processing = false;
let thinkingEnabled = __THINKING_ENABLED__;
const SAFETY_WARNING = __SAFETY_WARNING_JSON__;
const DEBUG_WARNING = __DEBUG_WARNING_JSON__;
let contentBuf = '';
let thinkingBuf = '';
let currentAssistantEl = null;
let currentContentEl = null;
let currentThinkingEl = null;
let renderPending = false;
let progressEl = null;
let currentToolGroup = null;
let history = [];
let historyIdx = -1;

// ── Inline Markdown renderer (no external deps) ──────────────────────────────
function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function inlineMd(text) {
  // Extract inline code first to protect its content
  const codes = [];
  text = text.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return '\x00' + (codes.length - 1) + '\x00'; });
  text = escHtml(text);
  text = text
    .replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return text.replace(/\x00(\d+)\x00/g, (_, i) => '<code>' + escHtml(codes[+i]) + '</code>');
}

function renderMd(src) {
  const out = [];
  const lines = src.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // Fenced code block
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      const lang = fence[1];
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].startsWith('```')) codeLines.push(lines[i++]);
      if (i < lines.length) i++; // skip closing ```
      out.push('<pre><code' + (lang ? ' class="language-' + lang + '"' : '') + '>' + escHtml(codeLines.join('\n')) + '</code></pre>');
      continue;
    }
    // Heading
    const hm = line.match(/^(#{1,6}) (.*)/);
    if (hm) { out.push('<h' + hm[1].length + '>' + inlineMd(hm[2]) + '</h' + hm[1].length + '>'); i++; continue; }
    // Horizontal rule
    if (/^(---+|\*\*\*+)\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
    // Blockquote
    if (line.startsWith('> ')) {
      const ql = [];
      while (i < lines.length && lines[i].startsWith('> ')) ql.push(lines[i++].slice(2));
      out.push('<blockquote>' + ql.map(inlineMd).join('<br>') + '</blockquote>');
      continue;
    }
    // Unordered list
    if (/^[-*] /.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*] /.test(lines[i])) items.push('<li>' + inlineMd(lines[i++].replace(/^[-*] /, '')) + '</li>');
      out.push('<ul>' + items.join('') + '</ul>');
      continue;
    }
    // Ordered list
    if (/^\d+\. /.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\. /.test(lines[i])) items.push('<li>' + inlineMd(lines[i++].replace(/^\d+\. /, '')) + '</li>');
      out.push('<ol>' + items.join('') + '</ol>');
      continue;
    }
    // Table (header row | separator row | body rows)
    if (/^\|/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|/.test(lines[i + 1])) {
      const tRows = [];
      while (i < lines.length && /^\|/.test(lines[i].trim())) tRows.push(lines[i++]);
      const parseRow = l => l.split('|').slice(1, -1).map(c => inlineMd(c.trim()));
      const head = parseRow(tRows[0]);
      const body = tRows.slice(2);
      let tHtml = '<table><thead><tr>' + head.map(c => '<th>' + c + '</th>').join('') + '</tr></thead>';
      if (body.length) tHtml += '<tbody>' + body.map(r => '<tr>' + parseRow(r).map(c => '<td>' + c + '</td>').join('') + '</tr>').join('') + '</tbody>';
      out.push(tHtml + '</table>');
      continue;
    }
    // Blank line
    if (!line.trim()) { i++; continue; }
    // Paragraph: collect consecutive non-block lines
    const para = [];
    while (i < lines.length && lines[i].trim() && !lines[i].match(/^(#{1,6} |```|> |[-*] |\d+\. |\|)/) && !/^(---+|\*\*\*+)\s*$/.test(lines[i])) {
      para.push(inlineMd(lines[i++]));
    }
    if (para.length) out.push('<p>' + para.join('<br>') + '</p>');
    else i++;  // safety: always advance past unrecognized lines
  }
  return out.join('\n');
}

function scrollBottom() {
  chat.scrollTop = chat.scrollHeight;
}
let scrollPending = false;
function scheduleScroll() {
  if (!scrollPending) {
    scrollPending = true;
    requestAnimationFrame(() => { scrollPending = false; scrollBottom(); });
  }
}

function countLines(s) {
  if (!s) return 0;
  return (s.match(/\n/g) || []).length + (s.endsWith('\n') ? 0 : 1);
}

function buildUiNotice(uiShown, uiClipped, modelShown, linesTotal) {
  if (modelShown === 0 && linesTotal === 0) return '';
  if (modelShown > 0 && linesTotal > 0 && modelShown < linesTotal)
    return uiClipped ? '[' + uiShown + ' lines shown \xb7 model: ' + modelShown + '/' + linesTotal + ']' : '';
  const n = linesTotal > 0 ? linesTotal : modelShown;
  return uiClipped ? '[' + uiShown + '/' + n + ' lines shown \xb7 model: all]' : '[' + n + ' lines \xb7 all]';
}

function autoResize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
  input.style.overflowY = input.scrollHeight > 200 ? 'auto' : 'hidden';
}

function setProcessing(v) {
  processing = v;
  sendBtn.disabled = v;
  input.disabled = v;
  if (!v) { input.placeholder = 'Tell me about this project'; input.focus(); }
}

// --- Transport: WebSocket with SSE fallback ---
let useSSE = false;
let wsConnectAttempts = 0;

function connect() {
  if (useSSE) { document.getElementById('status-ws').textContent = 'http'; return; }
  ws = new WebSocket('ws://' + location.host + '/ws');
  const timeout = setTimeout(() => {
    // WS stuck in CONNECTING — proxy doesn't support it
    if (ws.readyState === 0) { ws.close(); switchToSSE(); }
  }, 3000);
  ws.onopen = () => { clearTimeout(timeout); wsConnectAttempts = 0; document.getElementById('status-ws').textContent = 'ws'; };
  ws.onclose = () => {
    clearTimeout(timeout);
    wsConnectAttempts++;
    if (wsConnectAttempts >= 2) { switchToSSE(); }
    else { setTimeout(connect, 2000); }
  };
  ws.onerror = () => { clearTimeout(timeout); };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
}

function switchToSSE() {
  useSSE = true;
  document.getElementById('status-ws').textContent = 'http';
  console.log('WebSocket unavailable, using HTTP/SSE fallback');
}

async function sendViaSSE(text) {
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: text}),
    });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try { handleEvent(JSON.parse(line.slice(6))); } catch(e) {}
        }
      }
    }
    // Process remaining buffer
    if (buffer.startsWith('data: ')) {
      try { handleEvent(JSON.parse(buffer.slice(6))); } catch(e) {}
    }
  } catch(e) {
    handleEvent({type: 'error', text: 'Connection error: ' + e.message});
    handleEvent({type: 'done', text: ''});
  }
}

function handleEvent(ev) {
  switch (ev.type) {
    case 'content':
      hideSpinner();
      if (!currentAssistantEl) startAssistantMsg();
      contentBuf += ev.text;
      if (!renderPending) {
        renderPending = true;
        requestAnimationFrame(() => {
          if (currentContentEl && contentBuf) currentContentEl.innerHTML = renderMd(contentBuf);
          renderPending = false;
          scheduleScroll();
        });
      }
      break;

    case 'thinking':
      if (!currentAssistantEl) startAssistantMsg();
      thinkingBuf += ev.text;
      if (currentThinkingEl) {
        currentThinkingEl.querySelector('pre').textContent = thinkingBuf;
      }
      scheduleScroll();
      break;

    case 'tool_call': {
      hideSpinner();
      flushAssistant();
      const tc = document.createElement('div');
      tc.className = 'tool-call';
      const argsStr = ev.tool_args ? Object.entries(ev.tool_args).map(([k,v]) => {
        let vs = typeof v === 'string' ? v : JSON.stringify(v);
        if (vs.length > 100) vs = vs.slice(0, 100) + '...';
        return k + '=' + JSON.stringify(vs);
      }).join(', ') : '';
      tc.innerHTML = '<span class="name">&gt; ' + esc(ev.tool_name) + '</span>(<span class="args">' + esc(argsStr) + '</span>)';
      currentToolGroup = document.createElement('div');
      currentToolGroup.className = 'tool-group';
      currentToolGroup.appendChild(tc);
      chat.appendChild(currentToolGroup);
      showSpinner('Running ' + ev.tool_name);
      scrollBottom();
      break;
    }

    case 'tool_result': {
      hideSpinner();
      const tr = document.createElement('div');
      tr.className = 'tool-result';
      const UI_LIMIT = 500;
      const raw = ev.text || '';
      let display;
      if (raw.length <= UI_LIMIT) {
        const notice = buildUiNotice(countLines(raw), false, ev.lines_shown || 0, ev.lines_total || 0);
        display = raw + (notice ? '\n' + notice : '');
      } else {
        let cut = raw.lastIndexOf('\n', UI_LIMIT);
        if (cut === -1) cut = UI_LIMIT;
        const preview = raw.slice(0, cut);
        const notice = buildUiNotice(countLines(preview), true, ev.lines_shown || 0, ev.lines_total || 0);
        display = preview + (notice ? '\n' + notice : '');
      }
      tr.textContent = display;
      if (currentToolGroup) {
        currentToolGroup.appendChild(tr);
        currentToolGroup = null;
      } else {
        chat.appendChild(tr);
      }
      if (ev.tool_name === 'todo') updateStatus();
      showSpinner(thinkingEnabled ? 'Thinking' : 'Processing');
      scrollBottom();
      break;
    }

    case 'progress':
      if (!progressEl) {
        progressEl = document.createElement('div');
        progressEl.className = 'msg-progress';
        chat.appendChild(progressEl);
      }
      progressEl.textContent = ev.text;
      scrollBottom();
      break;

    case 'markdown':
      hideSpinner();
      flushAssistant();
      const mk = document.createElement('div');
      mk.className = 'msg-markdown';
      mk.innerHTML = renderMd(ev.text);
      chat.appendChild(mk);
      scrollBottom();
      break;

    case 'theme':
      if (ev.text === 'auto') document.documentElement.removeAttribute('data-theme');
      else document.documentElement.setAttribute('data-theme', ev.text);
      break;

    case 'status':
      hideSpinner();
      flushAssistant();
      if (ev.text === 'Conversation cleared.') {
        chat.innerHTML = '';
        initWelcome();
        break;
      }
      const st = document.createElement('div');
      st.className = 'msg-status';
      st.textContent = ev.text;
      chat.appendChild(st);
      // Update thinking status if it changed
      if (ev.text.startsWith('Thinking mode:')) {
        if (ev.text.includes('once')) { statusThinking.textContent = 'once'; statusThinking.className = 'think-once'; thinkingEnabled = true; }
        else if (ev.text.includes('ON')) { statusThinking.textContent = 'on'; statusThinking.className = 'think-on'; thinkingEnabled = true; }
        else { statusThinking.textContent = 'off'; statusThinking.className = 'think-off'; thinkingEnabled = false; }
      }
      // Update workspace if it changed
      if (ev.text.startsWith('Workspace changed to:')) {
        const newWs = ev.text.replace('Workspace changed to: ', '');
        const wsEl = document.getElementById('status-workspace');
        if (wsEl) wsEl.textContent = newWs;
      }
      // Update safety if it changed
      if (ev.text.startsWith('Safety:')) {
        const safetyEl = document.getElementById('status-safety');
        const level = ev.text.split(':')[1]?.trim().split(/\s/)[0];
        if (safetyEl && level) safetyEl.textContent = 'safety:' + level;
      }
      scrollBottom();
      break;

    case 'error':
      hideSpinner();
      flushAssistant();
      const er = document.createElement('div');
      er.className = 'msg-error';
      er.textContent = ev.text;
      chat.appendChild(er);
      scrollBottom();
      break;

    case 'done':
      hideSpinner();
      flushAssistant();
      progressEl = null;
      updateStatus();
      setProcessing(false);
      if (ev.text === 'quit') {
        fetch('/api/shutdown', {method: 'POST'}).finally(() => window.close());
      }
      break;

    case 'confirm_request':
      showConfirmModal(ev);
      break;
  }
}

// --- Confirm modal ---
function showConfirmModal(ev) {
  const overlay = document.createElement('div');
  overlay.id = 'confirm-overlay';

  const box = document.createElement('div');
  box.className = 'c-box';

  const title = document.createElement('div');
  title.className = 'c-title';
  title.textContent = 'Allow tool call?';

  const preview = document.createElement('pre');
  preview.className = 'c-preview';
  preview.textContent = ev.preview || ev.tool;

  const buttons = document.createElement('div');
  buttons.className = 'c-buttons';

  function respond(allowed) {
    document.getElementById('confirm-overlay')?.remove();
    document.removeEventListener('keydown', keyHandler);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type: 'confirm_response', id: ev.id, allowed}));
    }
  }

  const allowBtn = document.createElement('button');
  allowBtn.className = 'c-allow';
  allowBtn.textContent = 'Allow  [Enter]';
  allowBtn.onclick = () => respond(true);

  const denyBtn = document.createElement('button');
  denyBtn.className = 'c-deny';
  denyBtn.textContent = 'Deny  [Esc / n]';
  denyBtn.onclick = () => respond(false);

  buttons.appendChild(allowBtn);
  buttons.appendChild(denyBtn);
  box.appendChild(title);
  box.appendChild(preview);
  box.appendChild(buttons);
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  function keyHandler(e) {
    if (e.key === 'Enter') { e.preventDefault(); respond(true); }
    else if (e.key === 'Escape' || e.key === 'n') { e.preventDefault(); respond(false); }
  }
  document.addEventListener('keydown', keyHandler);
  allowBtn.focus();
}

function startAssistantMsg() {
  currentAssistantEl = document.createElement('div');
  currentAssistantEl.className = 'msg msg-assistant';

  // Thinking (collapsible)
  if (thinkingBuf) {
    currentThinkingEl = document.createElement('details');
    currentThinkingEl.className = 'thinking';
    currentThinkingEl.innerHTML = '<summary>Thinking...</summary><pre></pre>';
    currentThinkingEl.querySelector('pre').textContent = thinkingBuf;
    currentAssistantEl.appendChild(currentThinkingEl);
  }

  currentContentEl = document.createElement('div');
  currentContentEl.className = 'content';
  currentAssistantEl.appendChild(currentContentEl);
  chat.appendChild(currentAssistantEl);
}

function flushAssistant() {
  // If we have thinking but no assistant element yet, create one
  if (thinkingBuf && !currentAssistantEl) {
    startAssistantMsg();
  }
  // Finalize thinking
  if (thinkingBuf && currentAssistantEl && !currentThinkingEl) {
    const det = document.createElement('details');
    det.className = 'thinking';
    det.innerHTML = '<summary>Thinking...</summary><pre></pre>';
    det.querySelector('pre').textContent = thinkingBuf;
    currentAssistantEl.insertBefore(det, currentContentEl);
    currentThinkingEl = det;
  }
  // Finalize content with markdown
  if (contentBuf && currentContentEl) {
    currentContentEl.innerHTML = renderMd(contentBuf);
  }
  contentBuf = '';
  thinkingBuf = '';
  currentAssistantEl = null;
  currentContentEl = null;
  currentThinkingEl = null;
  renderPending = false;
}

// --- Spinner ---
let spinnerEl = null;
function showSpinner(label) {
  hideSpinner();
  spinnerEl = document.createElement('div');
  spinnerEl.className = 'spinner';
  spinnerEl.innerHTML = '<div class="spinner-ring"></div>' + esc(label || 'Thinking') + '...';
  chat.appendChild(spinnerEl);
  scrollBottom();
}
function hideSpinner() {
  if (spinnerEl) { spinnerEl.remove(); spinnerEl = null; }
}

// --- Input ---
function isIncompleteCommand(line) {
  const s = line.trim();
  if (!s.startsWith('/')) return false;
  const parts = s.split(/\s+/);
  const cmdPart = parts[0].toLowerCase();
  const firstArg = (parts[1] || '').toLowerCase();

  // Unknown or partial-prefix command
  if (!COMMANDS.includes(cmdPart)) return true;

  // Known command: validate first argument for those with a fixed set
  if (firstArg) {
    if (cmdPart === '/think') {
      return !['on','off','once','true','false','yes','no'].includes(firstArg);
    }
    if (cmdPart === '/safety') return !SAFETY_OPTIONS.includes(firstArg);
    if (cmdPart === '/update') return !UPDATE_OPTIONS.includes(firstArg);
  }
  return false;
}

function sendMessage() {
  const text = input.value.trim();
  if (!text || processing) return;
  // Intercept /config tools — open modal client-side instead of sending to server
  if (text.toLowerCase() === '/config tools') {
    input.value = '';
    autoResize();
    hintEl.className = 'hidden';
    hintEl.textContent = '';
    openToolsModal();
    return;
  }
  if (isIncompleteCommand(text)) return;

  // Add to history
  history.push(text);
  historyIdx = history.length;

  // Show user message
  const msg = document.createElement('div');
  msg.className = 'msg msg-user';
  msg.innerHTML = '<div class="bubble">' + esc(text) + '</div>';
  chat.appendChild(msg);
  scrollBottom();

  input.value = '';
  autoResize();
  setProcessing(true);
  showSpinner(thinkingEnabled ? 'Thinking' : 'Processing');

  if (useSSE) {
    sendViaSSE(text);
  } else {
    ws.send(JSON.stringify({ type: 'input', text: text }));
  }
}

// --- Inline hints & tab completion ---
const hintEl = document.getElementById('hint-line');
const COMMANDS = ['/safety', '/workspace', '/think', '/clear', '/todo', '/info', '/code', '/lazygit', '/config', '/pull', '/update', '/help', '/quit', '/exit'];
const COMMAND_HINTS = {
  '/safety':    ['confirm|workspace|none',    'Set session safety level'],
  '/workspace': ['<dir>',                     'Switch workspace directory'],
  '/think':     ['on|off|once',               'Toggle thinking mode'],
  '/clear':     ['',                          'Clear conversation history'],
  '/todo':      ['[list|clear|add|done|remove]','Manage task list'],
  '/info':      ['[prompt|context|tools]',      'Show model info, system prompt/context, or available tools'],
  '/code':      ['',                          'Open workspace in VS Code'],
  '/lazygit':   ['',                          'Open lazygit in a new terminal window'],
  '/config':    ['[tools | theme | set KEY VAL]', 'Show/edit config or tool enables'],
  '/pull':      ['[model|all]',               "Pull a model; 'all' pulls every local model"],
  '/update':    ['ollama|models',             'Update Ollama binary or pull all local models'],
  '/help':      ['',                          'Show available commands'],
  '/quit':      ['',                          'Exit NanoHarness'],
  '/exit':      ['',                          'Exit NanoHarness'],
};
const THINK_OPTIONS = ['on', 'off', 'once'];
const SAFETY_OPTIONS = ['confirm', 'workspace', 'none'];
const UPDATE_OPTIONS = ['ollama', 'models'];
const INFO_OPTIONS = ['prompt', 'context', 'tools'];
const TOOL_NAMES = ['bash', 'read_file', 'write_file', 'list_files', 'python_exec', 'todo', 'fetch_webpage'];
const THEME_OPTIONS = ['light', 'dark', 'auto'];

function getHint(line) {
  const s = line.trimStart();
  if (!s.startsWith('/')) {
    if (s.toLowerCase().includes(' /thi')) return '... /think once  Think for this message only';
    return '';
  }
  const parts = s.split(/\s+(.*)/);
  const cmdPart = parts[0].toLowerCase();
  const hasSpace = s.includes(' ');
  const argPart = (parts[1] || '').trimStart().toLowerCase();

  if (!hasSpace) {
    const matches = COMMANDS.filter(c => c.startsWith(cmdPart));
    if (!matches.length) return '';
    if (matches.length === 1) {
      const c = matches[0];
      const [argH, desc] = COMMAND_HINTS[c];
      let ghost = c;
      if (argH) ghost += ' ' + argH;
      if (desc) ghost += '  ' + desc;
      return ghost;
    }
    return matches.join('  ');
  }
  if (COMMAND_HINTS[cmdPart]) {
    const [argH, desc] = COMMAND_HINTS[cmdPart];
    if (!argH) return '';
    if (cmdPart === '/think' && argPart) {
      const opts = THINK_OPTIONS.filter(o => o.startsWith(argPart));
      return opts.length ? '/think ' + opts.join(' | ') : '';
    }
    if (cmdPart === '/safety' && argPart) {
      const opts = SAFETY_OPTIONS.filter(o => o.startsWith(argPart));
      return opts.length ? '/safety ' + opts.join(' | ') : '';
    }
    if (cmdPart === '/update' && argPart) {
      const opts = UPDATE_OPTIONS.filter(o => o.startsWith(argPart));
      return opts.length ? '/update ' + opts.join(' | ') : '';
    }
    if (cmdPart === '/info' && argPart) {
      const opts = INFO_OPTIONS.filter(o => o.startsWith(argPart));
      return opts.length ? '/info ' + opts.join(' | ') : '';
    }
    if (cmdPart === '/config') {
      const subParts = argPart.split(/\s+/).filter(Boolean);
      const firstSub = subParts[0] || '';
      if ('tools'.startsWith(firstSub) && firstSub !== 'set' && firstSub !== 'theme') {
        if (subParts.length <= 1) return '/config tools [<tool> [global] [workspace]]  Configure tool access';
        if (subParts.length === 2) return '/config tools <tool> on|off|_  (global; _ = keep)';
        if (subParts.length === 3) return `/config tools <tool> ${subParts[2]} on|off|inherit|_  (workspace)`;
        return '';
      }
      if ('theme'.startsWith(firstSub) && firstSub !== 'set' && firstSub !== 'tools') {
        if (subParts.length <= 1) return '/config theme light|dark|auto  Set UI color theme';
        return '';
      }
      return desc ? cmdPart + ' ' + argH + '  ' + desc : cmdPart + ' ' + argH;
    }
    if (cmdPart === '/workspace' && argPart) return '';
    return desc ? cmdPart + ' ' + argH + '  ' + desc : cmdPart + ' ' + argH;
  }
  return '';
}

function updateHint() {
  // Use the line where the cursor currently is for hints
  const pos = input.selectionStart ?? input.value.length;
  const textUpToCursor = input.value.slice(0, pos);
  const currentLine = textUpToCursor.split('\n').pop() ?? '';
  const hint = getHint(currentLine);
  if (hint) {
    hintEl.textContent = hint;
    hintEl.className = 'visible';
  } else {
    hintEl.textContent = '';
    hintEl.className = 'hidden';
  }
}

// Tab completion state
let tabMatches = [];
let tabIndex = -1;

function getCompletions(line) {
  const s = line.trimStart().toLowerCase();
  // /think <partial>
  if (s.startsWith('/think ')) {
    const partial = s.slice(7).trimStart();
    return THINK_OPTIONS.filter(o => o.startsWith(partial)).map(o => '/think ' + o);
  }
  // /safety <partial>
  if (s.startsWith('/safety ')) {
    const partial = s.slice(8).trimStart();
    return SAFETY_OPTIONS.filter(o => o.startsWith(partial)).map(o => '/safety ' + o);
  }
  // /update <partial>
  if (s.startsWith('/update ')) {
    const partial = s.slice(8).trimStart();
    return UPDATE_OPTIONS.filter(o => o.startsWith(partial)).map(o => '/update ' + o);
  }
  // /info <partial>
  if (s.startsWith('/info ')) {
    const partial = s.slice(6).trimStart();
    return INFO_OPTIONS.filter(o => o.startsWith(partial)).map(o => '/info ' + o);
  }
  // /config tools|theme|set ...
  if (s.startsWith('/config ')) {
    const rest = s.slice(8);
    const trailing = rest !== rest.trimEnd();
    const rp = rest.trim().split(/\s+/).filter(Boolean);
    const first = rp[0] || '';
    const subcmds = ['set', 'theme', 'tools'];
    // still typing the subcommand token
    if (!trailing && rp.length <= 1) return subcmds.filter(x => x.startsWith(first)).map(x => '/config ' + x);
    // /config tools ...
    if (first === 'tools') {
      if (rp.length === 1) return TOOL_NAMES.map(n => `/config tools ${n}`);
      if (rp.length === 2 && !trailing) return TOOL_NAMES.filter(n => n.startsWith(rp[1])).map(n => `/config tools ${n}`);
      if (rp.length === 2) return ['on', 'off', '_'].map(v => `/config tools ${rp[1]} ${v}`);
      if (rp.length === 3 && !trailing) return ['on', 'off', '_'].filter(v => v.startsWith(rp[2])).map(v => `/config tools ${rp[1]} ${v}`);
      if (rp.length === 3) return ['on', 'off', 'inherit'].map(v => `/config tools ${rp[1]} ${rp[2]} ${v}`);
      if (rp.length === 4) return ['on', 'off', 'inherit'].filter(v => v.startsWith(rp[3])).map(v => `/config tools ${rp[1]} ${rp[2]} ${v}`);
    }
    // /config theme ...
    if (first === 'theme') {
      if (rp.length === 1) return THEME_OPTIONS.map(v => `/config theme ${v}`);
      return THEME_OPTIONS.filter(v => v.startsWith(rp[1])).map(v => `/config theme ${v}`);
    }
    return [];
  }
  // bare /command prefix
  if (s.startsWith('/') && !s.includes(' ')) {
    return COMMANDS.filter(c => c.startsWith(s));
  }
  return [];
}

input.addEventListener('input', () => {
  tabMatches = [];
  tabIndex = -1;
  autoResize();
  updateHint();
});

input.addEventListener('keydown', (e) => {
  if (e.key === 'Tab') {
    e.preventDefault();
    if (tabMatches.length && tabIndex >= 0) {
      tabIndex = (tabIndex + 1) % tabMatches.length;
      input.value = tabMatches[tabIndex];
      updateHint();
      return;
    }
    const matches = getCompletions(input.value);
    if (matches.length) {
      tabMatches = matches;
      tabIndex = 0;
      input.value = matches[0];
      updateHint();
    }
    return;
  }
  if (e.key === 'Enter' && (e.shiftKey || e.altKey)) {
    // Shift+Enter, Alt+Enter → insert newline
    e.preventDefault();
    const pos = input.selectionStart;
    input.value = input.value.slice(0, pos) + '\n' + input.value.slice(input.selectionEnd);
    input.selectionStart = input.selectionEnd = pos + 1;
    autoResize();
    updateHint();
  } else if (e.key === 'j' && e.ctrlKey) {
    // Ctrl+J → insert newline
    e.preventDefault();
    const pos = input.selectionStart;
    input.value = input.value.slice(0, pos) + '\n' + input.value.slice(input.selectionEnd);
    input.selectionStart = input.selectionEnd = pos + 1;
    autoResize();
    updateHint();
  } else if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.ctrlKey) {
    e.preventDefault();
    sendMessage();
    hintEl.className = 'hidden';
    hintEl.textContent = '';
  } else if (e.key === 'ArrowUp') {
    if (historyIdx > 0) { historyIdx--; input.value = history[historyIdx]; updateHint(); }
  } else if (e.key === 'ArrowDown') {
    if (historyIdx < history.length - 1) { historyIdx++; input.value = history[historyIdx]; }
    else { historyIdx = history.length; input.value = ''; }
    updateHint();
  }
});

// Global ESC → interrupt streaming response
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !processing) return;
  const confirmOpen = !!document.getElementById('confirm-overlay');
  const toolsOpen = document.getElementById('tools-modal-overlay')?.style.display !== 'none';
  if (confirmOpen || toolsOpen) return;
  e.preventDefault();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'interrupt' }));
  }
});

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}


const APP_VERSION = __VERSION__;
const OLLAMA_URL = __OLLAMA_URL__;

async function initWelcome() {
  const frag = document.createDocumentFragment();
  const bannerEl = document.createElement('div');
  bannerEl.className = 'banner';
  bannerEl.textContent = __BANNER__;
  frag.appendChild(bannerEl);
  const metaEl = document.createElement('div');
  metaEl.className = 'banner-meta';
  metaEl.textContent = 'v' + APP_VERSION + ' \u2014 ' + __MODEL_NAME_JSON__ + ' \u2014 ' + __WORKSPACE_JSON__;
  frag.appendChild(metaEl);
  const ollamaMetaEl = document.createElement('div');
  ollamaMetaEl.className = 'banner-meta';
  ollamaMetaEl.id = 'ollama-version-line';
  frag.appendChild(ollamaMetaEl);
  const tipEl = document.createElement('div');
  tipEl.className = 'msg-status';
  tipEl.textContent = 'Type /help for commands';
  frag.appendChild(tipEl);
  if (SAFETY_WARNING) {
    const warnEl = document.createElement('div');
    warnEl.className = 'msg-warning';
    warnEl.textContent = SAFETY_WARNING;
    frag.appendChild(warnEl);
  }
  if (DEBUG_WARNING) {
    const dbgEl = document.createElement('div');
    dbgEl.className = 'msg-warning';
    dbgEl.textContent = DEBUG_WARNING;
    frag.appendChild(dbgEl);
  }
  chat.appendChild(frag);
  // Fetch Ollama version asynchronously after rendering banner
  try {
    const r = await fetch('/api/version');
    const d = await r.json();
    const el = document.getElementById('ollama-version-line');
    if (el && d.version) el.textContent = 'Ollama ' + d.version + ' \u2014 ' + d.url;
  } catch(e) {}
}

initWelcome();
connect();

// ── Tools Config Modal ────────────────────────────────────
const TOOL_NAMES_JS = __TOOL_NAMES_JSON__;
let toolsState = null;

async function openToolsModal() {
  try {
    const r = await fetch('/api/config/tools');
    const d = await r.json();
    toolsState = d.tools;
  } catch(e) {
    toolsState = {};
    for (const n of TOOL_NAMES_JS) toolsState[n] = {global: true, workspace: null};
  }
  renderToolsModal();
  document.getElementById('tools-modal-overlay').style.display = 'flex';
  const first = document.querySelector('#tools-tbody .t-btn');
  if (first) first.focus();
  document.removeEventListener('keydown', toolsModalKeyHandler);
  document.addEventListener('keydown', toolsModalKeyHandler);
}

function renderToolsModal() {
  const tbody = document.getElementById('tools-tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  for (const name of TOOL_NAMES_JS) {
    const entry = (toolsState && toolsState[name]) || {global: true, workspace: null};
    const tr = document.createElement('tr');

    const tdName = document.createElement('td');
    tdName.innerHTML = '<span class="t-tool-name">' + esc(name) + '</span>';
    tr.appendChild(tdName);

    const tdG = document.createElement('td');
    tdG.className = 't-col-toggle';
    const btnG = document.createElement('button');
    btnG.className = 't-btn ' + (entry.global ? 't-on' : 't-off');
    btnG.textContent = entry.global ? 'on' : 'off';
    btnG.dataset.tool = name;
    btnG.dataset.col = 'global';
    btnG.addEventListener('click', () => cycleTool(name, 'global'));
    tdG.appendChild(btnG);
    tr.appendChild(tdG);

    const tdW = document.createElement('td');
    tdW.className = 't-col-toggle';
    const btnW = document.createElement('button');
    const wsClass = entry.workspace === null ? 't-ws-inherit' : (entry.workspace ? 't-ws-on' : 't-ws-off');
    btnW.className = 't-ws ' + wsClass;
    btnW.textContent = entry.workspace === null ? 'inherit' : (entry.workspace ? 'on' : 'off');
    btnW.dataset.tool = name;
    btnW.dataset.col = 'workspace';
    btnW.addEventListener('click', () => cycleTool(name, 'workspace'));
    tdW.appendChild(btnW);
    tr.appendChild(tdW);

    tbody.appendChild(tr);
  }
}

function cycleTool(name, col) {
  if (!toolsState || !toolsState[name]) return;
  if (col === 'global') {
    toolsState[name].global = !toolsState[name].global;
  } else {
    const cur = toolsState[name].workspace;
    toolsState[name].workspace = cur === null ? true : cur === true ? false : null;
  }
  renderToolsModal();
  document.querySelector(`[data-tool="${name}"][data-col="${col}"]`)?.focus();
}

async function closeToolsModal() {
  document.removeEventListener('keydown', toolsModalKeyHandler);
  document.getElementById('tools-modal-overlay').style.display = 'none';
  if (!toolsState) return;
  const snapshot = toolsState;
  toolsState = null;
  try {
    await fetch('/api/config/tools', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tools: snapshot}),
    });
  } catch(e) {}
}

function handleToolsOverlayClick(e) {
  if (e.target === e.currentTarget) closeToolsModal();
}

function toolsModalKeyHandler(e) {
  if (e.key === 'Escape') { e.preventDefault(); closeToolsModal(); return; }
  const focused = document.activeElement;
  if (!focused || !focused.dataset || !focused.dataset.tool) return;
  const idx = TOOL_NAMES_JS.indexOf(focused.dataset.tool);
  const col = focused.dataset.col;
  if (e.key === 'ArrowDown' && idx < TOOL_NAMES_JS.length - 1) {
    e.preventDefault();
    document.querySelector(`[data-tool="${TOOL_NAMES_JS[idx+1]}"][data-col="${col}"]`)?.focus();
  } else if (e.key === 'ArrowUp' && idx > 0) {
    e.preventDefault();
    document.querySelector(`[data-tool="${TOOL_NAMES_JS[idx-1]}"][data-col="${col}"]`)?.focus();
  } else if (e.key === 'ArrowRight' && col === 'global') {
    e.preventDefault();
    document.querySelector(`[data-tool="${focused.dataset.tool}"][data-col="workspace"]`)?.focus();
  } else if (e.key === 'ArrowLeft' && col === 'workspace') {
    e.preventDefault();
    document.querySelector(`[data-tool="${focused.dataset.tool}"][data-col="global"]`)?.focus();
  } else if (e.key === ' ' || e.key === 'Enter') {
    e.preventDefault();
    focused.click();
  }
}
</script>
</body>
</html>
"""
