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

from . import logging as log, BANNER as _BANNER
from .config import WARN_SAFETY_NONE, WARN_DEBUG_ON
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
        .replace("__SAFETY_WARNING_JSON__", json.dumps(WARN_SAFETY_NONE if cfg.safety.level == "none" else ""))
        .replace("__DEBUG_WARNING_JSON__", json.dumps(WARN_DEBUG_ON if cfg.debug else ""))
    )

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return cached_html

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

                if msg_type != "input":
                    continue
                text = data.get("text", "").strip()
                if not text:
                    continue

                if app.state.processing:
                    await websocket.send_json({"type": "error", "text": "Still processing..."})
                    continue

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
        except WebSocketDisconnect:
            log.log_event("ws_disconnect", "client disconnected")
            # Auto-deny all future confirms so in-flight agent tasks don't execute
            # unconfirmed tools for the remainder of the turn.
            async def _auto_deny(tool_name: str, args: dict) -> bool:
                return False
            agent.tools.confirm_fn = _auto_deny
            # Reject any pending confirms so agent tasks don't hang
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NanoHarness</title>
<script src="https://cdn.jsdelivr.net/npm/marked@18.0.0/marked.min.js" integrity="sha384-tkjnnf9Tzhv5ZFrDroGvUExw9C3EVFo0RFRkzKR8ZX4b5Psoec4yb1PlD8Jh4j4H" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.3.3/dist/purify.min.js" integrity="sha384-pcBjnGbkyKeOXaoFkmJiuR9E08/6gkmus6/Strimnxtl3uk0Hx23v345pWyC/MMr" crossorigin="anonymous"></script>
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
  body { background: var(--bg); color: var(--fg); font-family: var(--font); font-size: 14px; height: 100vh; display: flex; flex-direction: column; }

  /* Status bar */
  #status { background: var(--bg2); border-top: 1px solid var(--border); padding: 6px 16px; font-size: 13px; flex-shrink: 0; display: flex; gap: 12px; align-items: center; }
  #status .model { color: var(--accent); font-weight: 600; }
  #status .dim { color: var(--fg-dim); }

  /* Chat area */
  #chat { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }

  /* Messages */
  .msg { max-width: 100%; }
  .msg-user { align-self: flex-end; margin-top: 12px; }
  .msg-user .bubble { background: var(--bubble); border: 1px solid var(--bubble-border); border-radius: var(--radius); padding: 8px 14px; }
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
  .msg-assistant .content table { border-collapse: collapse; margin: 0.6em 0; }
  .msg-assistant .content th, .msg-assistant .content td { border: 1px solid var(--border); padding: 4px 8px; }
  .msg-assistant .content blockquote { border-left: 3px solid var(--border); padding-left: 12px; color: var(--fg-dim); }

  /* Tool call/result */
  .tool-call { border-left: 3px solid var(--yellow); background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; padding: 8px 12px; margin: 2px 0; font-family: var(--mono); font-size: 13px; }
  .tool-call .name { color: var(--yellow); font-weight: 600; }
  .tool-call .args { color: var(--fg-dim); }
  .tool-result { border-left: 3px solid var(--green); background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; padding: 8px 12px; margin: 2px 0; font-family: var(--mono); font-size: 12px; color: var(--green); white-space: pre-wrap; word-break: break-all; }

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

  /* /info markup output */
  .msg-markup { font-family: var(--mono); font-size: 13px; line-height: 1.7; }
  .msg-markup .dim { color: var(--fg-dim); }

  /* Spinner */
  .spinner { display: flex; align-items: center; gap: 8px; color: var(--fg-dim); font-size: 13px; padding: 4px 0; }
  .spinner-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: .3; transform: scale(.8); } 50% { opacity: 1; transform: scale(1.2); } }
  .spinner-dot:nth-child(2) { animation-delay: .15s; }
  .spinner-dot:nth-child(3) { animation-delay: .3s; }

  /* Hint line */
  #hint-line { background: var(--bg2); padding: 2px 16px; font-size: 13px; color: var(--fg-dim); font-family: var(--mono); min-height: 0; overflow: hidden; transition: max-height 0.1s, padding 0.1s; }
  #hint-line.visible { max-height: 24px; padding: 4px 16px; }
  #hint-line.hidden { max-height: 0; padding: 0 16px; }

  /* Input area */
  #input-area { background: var(--bg2); border-top: 1px solid var(--border); padding: 12px 16px; flex-shrink: 0; display: flex; gap: 8px; }
  #input-area textarea { flex: 1; background: var(--bg3); color: var(--fg); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 14px; font-family: var(--font); font-size: 14px; outline: none; resize: none; min-height: 42px; max-height: 200px; overflow-y: hidden; line-height: 1.5; }
  #input-area textarea:focus { border-color: var(--accent); }
  #input-area textarea::placeholder { color: var(--fg-dim); }
  #input-area button { background: var(--accent); color: var(--btn-fg); border: none; border-radius: var(--radius); padding: 8px 14px; font-weight: 700; cursor: pointer; font-size: 18px; line-height: 1; flex-shrink: 0; align-self: flex-end; margin-bottom: 3px; }
  #input-area button:hover { opacity: .9; }
  #input-area button:disabled { opacity: .4; cursor: default; }
</style>
</head>
<body>

<div id="chat"></div>

<div id="hint-line" class="hidden"></div>
<div id="input-area">
  <textarea id="input" rows="1" placeholder="Type a message or /help… Enter to send, Shift+Enter / Alt+Enter / Ctrl+J for newline" autocomplete="off" autofocus></textarea>
  <button id="send" onclick="sendMessage()" aria-label="Send">&#x2191;</button>
</div>

<div id="status">
  <span class="model">__MODEL_NAME__</span>
  <span class="dim" id="status-thinking">think:__THINKING__</span>
  <span class="dim" id="status-safety">safety:__SAFETY__</span>
  <span class="dim" id="status-workspace">__WORKSPACE__</span>
  <span class="dim" id="status-ws">connecting...</span>
  <span class="dim" id="theme-toggle" style="cursor:pointer;margin-left:auto;" onclick="toggleTheme()">theme:auto</span>
</div>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusWs = document.getElementById('status-ws');
const statusThinking = document.getElementById('status-thinking');

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
let progressEl = null;
let history = [];
let historyIdx = -1;

// Markdown renderer
function renderMd(text) {
  if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
    const html = marked.parse(text, { breaks: true });
    return DOMPurify.sanitize(html);
  }
  // Fallback: escape HTML and render as plain text (used when CDN libs fail to load)
  const d = document.createElement('div');
  d.textContent = text;
  return '<pre style="white-space:pre-wrap">' + d.innerHTML + '</pre>';
}

function scrollBottom() {
  chat.scrollTop = chat.scrollHeight;
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
  if (!v) input.focus();
}

// --- Transport: WebSocket with SSE fallback ---
let useSSE = false;
let wsConnectAttempts = 0;

function connect() {
  if (useSSE) { statusWs.textContent = 'http'; return; }
  ws = new WebSocket('ws://' + location.host + '/ws');
  const timeout = setTimeout(() => {
    // WS stuck in CONNECTING — proxy doesn't support it
    if (ws.readyState === 0) { ws.close(); switchToSSE(); }
  }, 3000);
  ws.onopen = () => { clearTimeout(timeout); wsConnectAttempts = 0; statusWs.textContent = 'ws'; };
  ws.onclose = () => {
    clearTimeout(timeout);
    wsConnectAttempts++;
    if (wsConnectAttempts >= 2) { switchToSSE(); }
    else { statusWs.textContent = 'reconnecting...'; setTimeout(connect, 2000); }
  };
  ws.onerror = () => { clearTimeout(timeout); };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
}

function switchToSSE() {
  useSSE = true;
  statusWs.textContent = 'http';
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
      currentContentEl.innerHTML = renderMd(contentBuf);
      scrollBottom();
      break;

    case 'thinking':
      if (!currentAssistantEl) startAssistantMsg();
      thinkingBuf += ev.text;
      if (currentThinkingEl) {
        currentThinkingEl.querySelector('pre').textContent = thinkingBuf;
      }
      scrollBottom();
      break;

    case 'tool_call':
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
      chat.appendChild(tc);
      showSpinner('Running ' + ev.tool_name);
      scrollBottom();
      break;

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
      chat.appendChild(tr);
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

    case 'markup':
      hideSpinner();
      flushAssistant();
      const mk = document.createElement('div');
      mk.className = 'msg-markup';
      mk.innerHTML = richToHtml(ev.text);
      chat.appendChild(mk);
      scrollBottom();
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
      // Update thinking status if it changed
      if (ev.text.startsWith('Thinking mode:')) {
        if (ev.text.includes('once')) { statusThinking.textContent = 'think:once'; thinkingEnabled = true; }
        else if (ev.text.includes('ON')) { statusThinking.textContent = 'think:on'; thinkingEnabled = true; }
        else { statusThinking.textContent = 'think:off'; thinkingEnabled = false; }
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
  overlay.style.cssText = `
    position:fixed;inset:0;background:rgba(0,0,0,0.6);display:flex;
    align-items:center;justify-content:center;z-index:9999;
  `;

  const box = document.createElement('div');
  box.style.cssText = `
    background:#1a1a2e;border:2px solid #4a9eff;border-radius:8px;
    padding:24px 28px;max-width:600px;width:90%;font-family:monospace;
  `;

  const title = document.createElement('div');
  title.style.cssText = 'color:#f0a500;font-weight:bold;font-size:1.1em;margin-bottom:12px;';
  title.textContent = 'Allow tool call?';

  const preview = document.createElement('pre');
  preview.style.cssText = `
    background:#0d0d1a;border-radius:4px;padding:10px 14px;
    white-space:pre-wrap;word-break:break-word;color:#ccc;
    font-size:0.88em;max-height:200px;overflow-y:auto;margin-bottom:16px;
  `;
  preview.textContent = ev.preview || ev.tool;

  const buttons = document.createElement('div');
  buttons.style.cssText = 'display:flex;gap:12px;';

  function respond(allowed) {
    document.getElementById('confirm-overlay')?.remove();
    document.removeEventListener('keydown', keyHandler);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type: 'confirm_response', id: ev.id, allowed}));
    }
  }

  const allowBtn = document.createElement('button');
  allowBtn.textContent = 'Allow  [Enter]';
  allowBtn.style.cssText = `
    background:#1a6634;color:#fff;border:1px solid #2a8844;
    padding:8px 18px;border-radius:4px;cursor:pointer;font-family:monospace;
  `;
  allowBtn.onclick = () => respond(true);

  const denyBtn = document.createElement('button');
  denyBtn.textContent = 'Deny  [Esc / n]';
  denyBtn.style.cssText = `
    background:#6b1a1a;color:#fff;border:1px solid #8b2a2a;
    padding:8px 18px;border-radius:4px;cursor:pointer;font-family:monospace;
  `;
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
}

// --- Spinner ---
let spinnerEl = null;
function showSpinner(label) {
  hideSpinner();
  spinnerEl = document.createElement('div');
  spinnerEl.className = 'spinner';
  spinnerEl.innerHTML = '<div class="spinner-dot"></div><div class="spinner-dot"></div><div class="spinner-dot"></div> ' + esc(label || 'Thinking') + '...';
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
const COMMANDS = ['/think', '/workspace', '/code', '/lazygit', '/clear', '/config', '/info', '/pull', '/update', '/todo', '/safety', '/help', '/quit', '/exit'];
const COMMAND_HINTS = {
  '/think':     ['on|off|once',               'Toggle thinking mode'],
  '/workspace': ['<dir>',                     'Switch workspace directory'],
  '/code':      ['',                          'Open workspace in VS Code'],
  '/lazygit':   ['',                          'Open lazygit in a new terminal window'],
  '/clear':     ['',                          'Clear conversation history'],
  '/config':    ['[set KEY VAL]',             'Show or edit configuration'],
  '/info':      ['[prompt|tools]',             'Show model info, system prompt, or available tools'],
  '/pull':      ['[model|all]',               "Pull a model; 'all' pulls every local model"],
  '/update':    ['ollama|models',             'Update Ollama binary or pull all local models'],
  '/todo':      ['[list|clear|add|done|remove]','Manage task list'],
  '/safety':    ['confirm|workspace|none',    'Set session safety level'],
  '/help':      ['',                          'Show available commands'],
  '/quit':      ['',                          'Exit NanoHarness'],
  '/exit':      ['',                          'Exit NanoHarness'],
};
const THINK_OPTIONS = ['on', 'off', 'once'];
const SAFETY_OPTIONS = ['confirm', 'workspace', 'none'];
const UPDATE_OPTIONS = ['ollama', 'models'];
const INFO_OPTIONS = ['prompt', 'tools'];

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

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// Convert Rich markup ([bold cyan]text[/]) to HTML.
// Only handles the subset emitted by _info_command.
function richToHtml(text) {
  const COLOR = { cyan: 'var(--cyan)', green: 'var(--green)', red: 'var(--red)', yellow: 'var(--yellow)' };
  const parts = text.split(/(\[[^\]]*\])/);
  let html = '';
  const stack = [];
  for (const part of parts) {
    if (!part.startsWith('[')) {
      html += part.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
      continue;
    }
    const tag = part.slice(1, -1).trim().toLowerCase();
    if (tag === '/' || tag.startsWith('/')) {
      const open = stack.pop();
      if (open) html += '</' + open + '>';
      continue;
    }
    const words = tag.split(/\s+/);
    let elem = 'span', style = '', hasDim = false;
    for (const w of words) {
      if (w === 'bold')        elem = 'strong';
      else if (w === 'italic') style += 'font-style:italic;';
      else if (w === 'dim')    hasDim = true;
      else if (COLOR[w])       style += 'color:' + COLOR[w] + ';';
    }
    let attrs = '';
    if (hasDim) attrs += ' class="dim"';
    if (style)  attrs += ' style="' + style + '"';
    stack.push(elem);
    html += '<' + elem + attrs + '>';
  }
  while (stack.length) html += '</' + stack.pop() + '>';
  return html;
}

function initWelcome() {
  const frag = document.createDocumentFragment();
  const bannerEl = document.createElement('div');
  bannerEl.className = 'banner';
  bannerEl.textContent = __BANNER__;
  frag.appendChild(bannerEl);
  const metaEl = document.createElement('div');
  metaEl.className = 'banner-meta';
  metaEl.textContent = 'v0.1.0 \u2014 ' + __MODEL_NAME_JSON__ + ' \u2014 ' + __WORKSPACE_JSON__;
  frag.appendChild(metaEl);
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
}

// --- Theme toggle ---
function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute('data-theme');
  let next;
  if (!current) next = 'light';
  else if (current === 'light') next = 'dark';
  else next = null;
  if (next) html.setAttribute('data-theme', next);
  else html.removeAttribute('data-theme');
  document.getElementById('theme-toggle').textContent = 'theme:' + (next || 'auto');
}

initWelcome();
connect();
</script>
</body>
</html>
"""
