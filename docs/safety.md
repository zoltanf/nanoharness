# NanoHarness Safety System

NanoHarness is a local AI coding agent that runs real shell commands and file operations on your machine. This document explains how the safety system works, what each level protects, and what it does not protect.

---

## Safety Levels

There are three safety levels, configured with `--safety`, `NANO_SAFETY`, or `/safety` at runtime:

| Level | Default | Purpose |
|-------|---------|---------|
| `workspace` | Yes | Workspace path containment + environment scrubbing |
| `confirm` | No | Everything in `workspace`, plus user approval before destructive tools run |
| `none` | No | No restrictions — full trust mode |

### `workspace` (default)

All AI-initiated file and directory operations are restricted to the configured workspace directory. Shell commands (`bash`, `python_exec`) run in the workspace as `cwd`, but the shell itself is unrestricted — see [Limitations](#limitations) below.

Protections active in `workspace` mode:

**Path containment (`_safe_path` in `tools.py`)**
- `read_file`, `write_file`, and `list_dir` resolve the requested path and verify it is inside `config.workspace`.
- Symlinks are resolved before the check, so a symlink pointing outside the workspace is rejected.
- `.git` directories are fully blocked from writes (case-insensitive check, macOS-safe).
- Relative paths are anchored to the workspace root.

**Environment scrubbing (`_scrubbed_env` in `tools.py`)**
All `bash` and `python_exec` subprocesses receive a cleaned copy of the environment with the following removed:

- Variables matching prefixes: `AWS_`, `AZURE_`, `GOOGLE_`, `GCP_`, `GITHUB_`
- Variables containing substrings: `_API_KEY`, `_SECRET`, `_TOKEN`, `_PASSWORD`, `_CREDENTIAL`, `_DSN`
- Exact names: `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `DATABASE_URL`, `PGPASSWORD`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- `HOME` is overridden to the workspace directory

This prevents the agent from accidentally (or intentionally) exfiltrating credentials that happen to be in your shell environment.

---

### `confirm`

Everything from `workspace` mode, plus explicit user approval before these tools execute:

| Tool | What it does |
|------|-------------|
| `bash` | Run a shell command |
| `python_exec` | Execute arbitrary Python code |
| `write_file` | Write content to a file |

When the agent requests one of these tools, execution is paused and the user sees a preview of the exact command or content. The turn only continues if the user approves.

**Confirmation flow by UI mode:**

- **TUI**: An inline prompt appears in the chat area with `[y/N]`.
- **REPL**: A `[y/N]` prompt is printed to stdout and waits for stdin.
- **Web UI**: A modal dialog appears with Allow / Deny buttons (keyboard shortcuts: Enter to allow, Escape or `n` to deny). The confirmation times out and auto-denies after 120 seconds.

If the WebSocket disconnects while the agent is mid-turn, all pending confirmations are auto-denied and subsequent tool calls in that turn are also auto-denied. The agent does not execute tools silently after a disconnect.

**`todo` is not in the confirm list** — it only modifies a local JSON file inside the workspace and is considered low-risk.

---

### `none`

No restrictions. The agent can:

- Read and write any file anywhere on the filesystem
- Run commands that interact with files outside the workspace
- See the full unmodified environment (including secrets)

This mode is intended for trusted local development where you have already inspected the workspace and model, or for testing the agent in a fully controlled environment.

**A warning is printed to stderr at startup whenever `safety=none` is active.**

---

## Shell Escape (`!cmd`)

Commands prefixed with `!` (e.g. `!git status`, `!ls -la`) are **user-initiated**, not AI-initiated. They bypass the confirm system because the user typed them directly — this is equivalent to running the command in your own terminal. These commands run in the workspace directory and with the scrubbed environment (in `workspace` or `confirm` modes).

---

## Limitations

The safety system operates at the **NanoHarness tool API layer**. It does not sandbox the underlying shell or Python interpreter.

**What `workspace` and `confirm` modes do NOT prevent:**

- A `bash` command using `cd /` or accessing absolute paths outside the workspace.
- A `python_exec` script using `open('/etc/passwd')`, `os.system(...)`, `subprocess.run(...)`, or any Python stdlib operation — the containment guards only apply to the `read_file` / `write_file` / `list_dir` tools, not to a live Python interpreter.
- A `bash` or `python_exec` subprocess that exfiltrates data via the network.

If you need true sandboxing, run NanoHarness inside a container, VM, or OS-level sandbox (e.g. Docker, `firejail`, macOS sandbox-exec).

---

## Web UI Security

The web UI binds to `127.0.0.1` by default (configurable via `--host` / `NANO_WEB_HOST`). It is not intended to be exposed on a public network interface.

**WebSocket CSRF protection:** The WebSocket endpoint (`/ws`) and the shutdown endpoint (`/api/shutdown`) check the `Origin` header. Connections from origins other than `localhost` or `127.0.0.1` on the configured port are rejected with HTTP 403 / WebSocket close code 1008. This prevents malicious web pages from connecting to your local NanoHarness instance.

**No authentication between browser tabs:** All browser tabs that connect share the same agent state. Only one tab can send messages at a time (enforced by the `processing` lock), but any tab can connect and interact with the agent.

---

## Environment Variables

| Variable | Effect |
|----------|--------|
| `NANO_SAFETY` | Set safety level (`confirm` / `workspace` / `none`) |
| `NANO_MODEL` | Model name |
| `NANO_THINKING` | Enable thinking mode (`1` / `true`) |
| `NANO_NUM_CTX` | Context window size |
| `NANO_MAX_STEPS` | Max agent steps per turn |
| `NANO_TIMEOUT` | Tool execution timeout (seconds) |
| `NANO_OLLAMA_URL` | Ollama API base URL |
| `NANO_DEBUG` | Enable debug logging |
| `NANO_WEB_PORT` | Web UI port |

Setting `NANO_SAFETY=none` is equivalent to `--safety none` and triggers the startup warning.

---

## Debug Logging

When `--debug` is passed (or `NANO_DEBUG=1`), NanoHarness writes detailed logs to:

```
~/.nanoharness/debug/<session_id>.log
```

**What is logged:**
- Full Ollama API request payloads — including all conversation messages
- All tool call arguments (bash commands, file paths, Python code)
- Full tool outputs (file contents, command output)
- Agent step counts and timing

**Security implications:** Debug logs contain everything the agent sees and does, including any file contents it reads and any commands it runs. If you paste secrets or sensitive code into the chat, they will be in the log file. Do not enable debug mode in shared or multi-user environments.

A warning is printed to stderr at startup when debug mode is active.

---

## Changing Safety at Runtime

**Session-only (not saved):**
```
/safety confirm
/safety workspace
/safety none
```

**Saved to config (persists across restarts):**
```
/config set safety.level confirm
```
This writes to `~/.nanoharness/config.toml`. Restart NanoHarness for it to take effect.

**Precedence (highest to lowest):**
1. CLI flag (`--safety`)
2. Environment variable (`NANO_SAFETY`)
3. Config file (`~/.nanoharness/config.toml`)
4. Default (`workspace`)
