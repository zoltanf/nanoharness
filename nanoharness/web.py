"""FastAPI web UI for NanoHarness. WebSocket + SSE fallback for chat streaming."""

from __future__ import annotations

import asyncio
import json
import webbrowser
from contextlib import asynccontextmanager
from typing import AsyncIterator, TYPE_CHECKING

import json as _json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import logging as log, BANNER as _BANNER

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

    # Pre-render HTML template (config values are static for the server lifetime)
    cfg = agent.config
    cached_html = (
        HTML_TEMPLATE
        .replace("__MODEL_NAME__", cfg.model.name)
        .replace("__THINKING__", "on" if cfg.model.thinking else "off")
        .replace("__THINKING_ENABLED__", "true" if cfg.model.thinking else "false")
        .replace("__SAFETY__", cfg.safety.level)
        .replace("__WS_PORT__", str(port))
        .replace("__WORKSPACE__", str(cfg.workspace))
        .replace("__BANNER__", _json.dumps(_BANNER))
    )

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return cached_html

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        log.log_event("ws_connect", f"client connected")
        try:
            while True:
                data = await websocket.receive_json()
                if data.get("type") != "input":
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
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    --bg: #0d1117; --bg2: #161b22; --bg3: #1c2129;
    --fg: #c9d1d9; --fg-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --cyan: #39c5cf; --border: #30363d;
    --radius: 8px; --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --mono: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
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
  .msg-user { align-self: flex-end; }
  .msg-user .bubble { background: #1a3a4a; border: 1px solid #264a5a; border-radius: var(--radius); padding: 8px 14px; }
  .msg-user .label { color: var(--cyan); font-size: 12px; font-weight: 600; margin-bottom: 4px; }

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
  .tool-call { border-left: 3px solid var(--yellow); background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; padding: 6px 12px; font-family: var(--mono); font-size: 13px; }
  .tool-call .name { color: var(--yellow); font-weight: 600; }
  .tool-call .args { color: var(--fg-dim); }
  .tool-result { border-left: 3px solid var(--green); background: var(--bg2); border-radius: 0 var(--radius) var(--radius) 0; padding: 6px 12px; font-family: var(--mono); font-size: 12px; color: var(--green); white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; }

  /* Thinking */
  .thinking { color: var(--fg-dim); font-style: italic; font-size: 13px; }
  .thinking summary { cursor: pointer; user-select: none; }
  .thinking pre { white-space: pre-wrap; margin-top: 4px; font-family: var(--mono); font-size: 12px; }

  /* Status/error messages */
  .msg-status { color: var(--fg-dim); font-size: 13px; text-align: center; font-style: italic; }
  .msg-error { color: var(--red); font-weight: 600; }

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
  #input-area textarea { flex: 1; background: var(--bg3); color: var(--fg); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 14px; font-family: var(--font); font-size: 14px; outline: none; resize: none; min-height: 42px; max-height: 200px; overflow-y: auto; line-height: 1.5; }
  #input-area textarea:focus { border-color: var(--accent); }
  #input-area textarea::placeholder { color: var(--fg-dim); }
  #input-area button { background: var(--accent); color: var(--bg); border: none; border-radius: var(--radius); padding: 10px 20px; font-weight: 600; cursor: pointer; font-size: 14px; }
  #input-area button:hover { opacity: .9; }
  #input-area button:disabled { opacity: .4; cursor: default; }
</style>
</head>
<body>

<div id="chat"></div>

<div id="hint-line" class="hidden"></div>
<div id="input-area">
  <textarea id="input" rows="1" placeholder="Type a message or /help… Enter to send, Shift+Enter / Alt+Enter / Ctrl+J for newline" autocomplete="off" autofocus></textarea>
  <button id="send" onclick="sendMessage()">Send</button>
</div>

<div id="status">
  <span class="model">__MODEL_NAME__</span>
  <span class="dim" id="status-thinking">think:__THINKING__</span>
  <span class="dim">safety:__SAFETY__</span>
  <span class="dim" id="status-workspace">__WORKSPACE__</span>
  <span class="dim" id="status-ws">connecting...</span>
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
let contentBuf = '';
let thinkingBuf = '';
let currentAssistantEl = null;
let currentContentEl = null;
let currentThinkingEl = null;
let history = [];
let historyIdx = -1;

// Markdown renderer
function renderMd(text) {
  if (typeof marked !== 'undefined') {
    return marked.parse(text, { breaks: true });
  }
  // Fallback: escape HTML and wrap in pre
  const esc = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return '<pre>' + esc + '</pre>';
}

function scrollBottom() {
  chat.scrollTop = chat.scrollHeight;
}

function autoResize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
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

    case 'tool_result':
      hideSpinner();
      const tr = document.createElement('div');
      tr.className = 'tool-result';
      let preview = ev.text || '';
      if (preview.length > 500) preview = preview.slice(0, 500) + '...';
      tr.textContent = preview;
      chat.appendChild(tr);
      showSpinner(thinkingEnabled ? 'Thinking' : 'Processing');
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
      flushAssistant();
      const st = document.createElement('div');
      st.className = 'msg-status';
      st.textContent = ev.text;
      chat.appendChild(st);
      // Update thinking status if it changed
      if (ev.text.startsWith('Thinking mode:')) {
        if (ev.text.includes('once')) { statusThinking.textContent = 'think:once'; thinkingEnabled = true; }
        else if (ev.text.includes('ON')) { statusThinking.textContent = 'think:on'; thinkingEnabled = true; }
        else { statusThinking.textContent = 'think:off'; thinkingEnabled = false; }
      }
      // Update workspace if it changed
      if (ev.text.startsWith('Workspace changed to:')) {
        const ws = ev.text.replace('Workspace changed to: ', '');
        const wsEl = document.getElementById('status-workspace');
        if (wsEl) wsEl.textContent = ws;
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
      setProcessing(false);
      if (ev.text === 'quit') {
        const q = document.createElement('div');
        q.className = 'msg-status';
        q.textContent = 'Session ended.';
        chat.appendChild(q);
      }
      break;
  }
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
  const s = line.trim().toLowerCase();
  if (!s.startsWith('/')) return false;
  const cmdPart = s.split(/\s/)[0];
  if (COMMANDS.includes(cmdPart)) return false;
  return COMMANDS.some(c => c.startsWith(cmdPart));
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
  msg.innerHTML = '<div class="label">&gt;&gt;&gt;</div><div class="bubble">' + esc(text) + '</div>';
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
const COMMANDS = ['/think', '/workspace', '/clear', '/config', '/info', '/help', '/quit'];
const COMMAND_HINTS = {
  '/think':     ['on|off|once',  'Toggle thinking mode'],
  '/workspace': ['<dir>',        'Switch workspace directory'],
  '/clear':     ['',             'Clear conversation history'],
  '/config':    ['',             'Show current configuration'],
  '/info':      ['',             'Show model details from Ollama'],
  '/help':      ['',             'Show available commands'],
  '/quit':      ['',             'Exit NanoHarness'],
};
const THINK_OPTIONS = ['on', 'off', 'once'];

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
  metaEl.textContent = 'v0.1.0 \u2014 __MODEL_NAME__ \u2014 __WORKSPACE__';
  frag.appendChild(metaEl);
  const tipEl = document.createElement('div');
  tipEl.className = 'msg-status';
  tipEl.textContent = 'Type /help for commands';
  frag.appendChild(tipEl);
  chat.appendChild(frag);
}

initWelcome();
connect();
</script>
</body>
</html>
"""
