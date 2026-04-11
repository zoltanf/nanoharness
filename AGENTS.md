# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Build & Run Commands

```bash
# Package manager: uv (no Node.js)
# Run the TUI (default mode)
uv run python -m nanoharness

# Run in REPL mode
uv run python -m nanoharness --repl

# Run web UI (requires web extra)
uv run --extra web python -m nanoharness --web --no-open

# Run all tests (unit + playwright, excludes e2e by default)
uv run --extra test --extra playwright --extra web pytest tests/ -v

# Run just unit tests (fast, no browser)
uv run --extra test pytest tests/ -q -m "not playwright"

# Run a single test file or test
uv run --extra test pytest tests/test_commands.py -v
uv run --extra test pytest tests/test_commands.py::TestThinkCommand::test_once -v

# Run playwright browser tests only
uv run --extra test --extra playwright --extra web pytest -m playwright -v

# Run e2e tests (requires running Ollama with a model)
uv run --extra test --extra playwright --extra web pytest -m e2e -v

# Install playwright browsers (one-time setup)
uv run --extra playwright playwright install chromium

# Coverage report
uv run --extra test pytest tests/ --cov=nanoharness --cov-report=term-missing
```

## Architecture

### Three UI Modes

`__main__.py` selects the mode: `--web` → FastAPI server, `--repl` → stdin loop, default → Textual TUI (falls back to REPL if Textual unavailable). All three consume the same `AsyncIterator[StreamEvent]` from `Agent.process_input()`.

### Agent Loop (`agent.py`)

The central async loop: user input → build messages (system + truncated history) → stream from Ollama → if tool calls, execute in parallel via `asyncio.gather` → append results to history → loop until no more tool calls or max steps reached. Yields `StreamEvent` objects for UI consumption.

**Fallback mechanism**: When Ollama's tool parser fails (detected by eval_count > 10 but empty response), retries without tools and parses bash/python code blocks from the response directly.

### Streaming Events

`StreamEvent` types: `content`, `thinking`, `tool_call`, `tool_result`, `status`, `error`, `done`, `progress`. The `progress` type carries incremental text lines (e.g. from `ollama pull` or `ollama serve` output) and is rendered in-place rather than appended as a new message. Serialized via `.to_dict()` for WebSocket/SSE transport. All UI modes consume the same event stream.

### Config Layering (`config.py`)

Precedence (highest to lowest): CLI args → env vars (`NANO_*`) → TOML (`~/.nanoharness/config.toml`) → dataclass defaults. Key config: `model.name`, `model.thinking`, `agent.max_steps`, `safety.level` (`"workspace"` / `"confirm"` / `"none"`).

**Tool enable/disable**: `ToolsConfig` dataclass (one `bool` field per tool, all default `True`) is stored under `config.tools` and serialized as a `[tools]` TOML section. `TOOL_NAMES` lists the canonical names. Workspace overrides live in `<workspace>/.nanoharness/tools.json` (only explicitly-set values; absent = inherit global).

### Tool Execution (`tools.py`)

`ToolExecutor` dispatches to: `bash`, `read_file`, `write_file`, `list_files`, `python_exec`, `todo`, `fetch_webpage`. All file operations go through `_safe_path()` which enforces workspace containment unless `safety="unrestricted"`. `fetch_webpage` uses `httpx` for fetching and `trafilatura` (lazy import, requires `web-reader` extra) for content extraction. Output clipped to `max_output_chars`.

**Tool filtering**: `enabled_schemas(tools_config)` merges global (`ToolsConfig`) and workspace (`tools.json`) state — workspace wins, absent = inherit — and returns the filtered subset of `TOOL_SCHEMAS`. Disabled tools are excluded from every LLM call (saves tokens; model can't attempt them). The workspace override dict is cached in `_ws_tools_cache` and invalidated on `_save_workspace_tools()`. The `todo` tool filtered out here does not affect `/todo` slash commands or the status bar (those bypass the LLM tool schema).

### Web UI (`web.py`)

Inline HTML/JS/CSS template (no build toolchain). WebSocket primary transport with SSE fallback. Config values baked into HTML at server start via string replacement (`__MODEL_NAME__`, etc.). JS mirrors Python logic for hints (`getHint`), tab completion (`getCompletions`), and incomplete command blocking (`isIncompleteCommand`).

### TUI (`tui.py`)

Textual app using `VerticalScroll` with dynamically mounted `Static` widgets. Streaming content updates a widget in-place via `widget.update()`, then swaps to rendered Markdown on completion. `CompletingInput` provides tab completion, `HintLine` shows inline command hints.

**`ToolsModal`**: `ModalScreen` launched when user types `/config tools` (bare). Keyboard: ↑↓ navigate rows (tools), ←→ switch columns (global / workspace), Space cycles states (workspace: `None→True→False→None`; global: `True↔False`). Escape saves and closes — writes `ToolsConfig` fields back to `config.toml` via `write_config_toml` and workspace overrides to `tools.json` via `_save_workspace_tools`.

### Completion System (`completion.py`)

Shared by REPL and TUI: `complete_line()` for context-aware tab completion, `hint_for_input()` for inline hints, `is_incomplete_command()` to block sending partial command prefixes. Web UI reimplements these in JS.

`complete_line` handles full-line replacements for `/workspace`, `/think`, `/update`, `/info`, and `/config` (subcommand/arg completion including `/config tools <tool> [global] [workspace]`). A `trailing` flag (whether `rest != rest.rstrip()`) disambiguates "still typing a token" from "committed and need next level". The fallback token path returns `[]` immediately when the input ends with a trailing space to avoid spurious path completions. `_do_tab_complete` in `tui.py` maintains a matching list of "full-line" command prefixes so that the prefix calculation does not double-prepend when cycling through suggestions.

### Startup Checks (`startup.py`)

`check_ollama` → `check_model` → `check_version` run in order before the UI launches. If `check_ollama` fails, `try_start_ollama` detects how Ollama is installed:
- **brew-managed** (found in `brew services list`): prompts to run `brew services start ollama`
- **PATH-only**: prompts to launch `ollama serve` as a detached background process (`subprocess.Popen(..., start_new_session=True)`)
- **Not installed**: prints install instructions and exits

After starting, polls `check_health()` with exponential back-off for up to 15 s.

## Test Infrastructure

- `tests/conftest.py`: `workspace` fixture (tmp_path with test files), `config`, `mock_client` (AsyncMock of OllamaClient), `pw_server_url` (session-scoped live server for Playwright)
- `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio` on tests
- Playwright tests use module-scoped server fixtures with `make_mock_agent()` for deterministic events
- Markers: `playwright` (browser tests, included by default), `e2e` (real Ollama, excluded by default)

## Key Patterns

- Commands (`/think`, `/workspace`, `/config tools`, etc.) are handled by `CommandHandler` before reaching the agent loop
- **`/config tools`**: bare form → TUI opens `ToolsModal`; in REPL/web it lists current state as text. `/config tools <tool> [g] [w]` sets global and/or workspace value (`_` skips a column; workspace also accepts `inherit` to remove an override). Global changes are written to `~/.nanoharness/config.toml`; workspace changes to `<workspace>/.nanoharness/tools.json`
- `/think once` is stateful: sets thinking on for one turn, then `consume_think_once()` resets it at all exit points in `process_input()`
- Ollama communication uses raw `httpx` (no SDK) against `/api/chat` with NDJSON streaming
- The agent maintains `history` (list of message dicts) with token-budget truncation (~200k tokens)
- **Reconnect**: Before each LLM turn, `check_health()` is called; on failure `_poll_reconnect(timeout=30)` retries with exponential back-off (check first, then sleep: 0.5 s → 5 s cap)
- **`_ask_confirm(action_id, params, *, default)`**: single gating point for optional user confirmation (delegates to `tools.confirm_fn` if set, else returns `default`)
- **`_stream_subprocess_output(proc)`**: shared async helper that reads stdout line-by-line and yields `progress` events; used by `/pull` and `/update ollama` to avoid duplicated streaming loops
- **`/pull [model|all]`**: bridges Ollama's sync pull callback to an async generator via `asyncio.Queue`; `/pull all` lists models then iterates, collecting failures for a summary line
- **`/update ollama`**: uses `shutil.which` to find the Ollama binary, detects brew vs manual install, runs the appropriate upgrade command through `_stream_subprocess_output`, then optionally restarts the server
- **`/info`**: uses `asyncio.gather` to fetch server version, running models, and model show-data in parallel before rendering
- **Startup version display**: `on_mount` in `tui.py` is `async` so it can `await client.get_version()` before rendering the welcome banner; REPL does the same in its async startup sequence
