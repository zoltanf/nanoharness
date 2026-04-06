# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

`StreamEvent` types: `content`, `thinking`, `tool_call`, `tool_result`, `status`, `error`, `done`. Serialized via `.to_dict()` for WebSocket/SSE transport. All UI modes consume the same event stream.

### Config Layering (`config.py`)

Precedence (highest to lowest): CLI args → env vars (`NANO_*`) → TOML (`~/.nanoharness/config.toml`) → dataclass defaults. Key config: `model.name`, `model.thinking`, `agent.max_steps`, `safety.level` ("workspace"/"unrestricted").

### Tool Execution (`tools.py`)

`ToolExecutor` dispatches to: `bash`, `read_file`, `write_file`, `list_dir`, `python_exec`, `todo`. All file operations go through `_safe_path()` which enforces workspace containment unless `safety="unrestricted"`. Output clipped to `max_output_chars`.

### Web UI (`web.py`)

Inline HTML/JS/CSS template (no build toolchain). WebSocket primary transport with SSE fallback. Config values baked into HTML at server start via string replacement (`__MODEL_NAME__`, etc.). JS mirrors Python logic for hints (`getHint`), tab completion (`getCompletions`), and incomplete command blocking (`isIncompleteCommand`).

### TUI (`tui.py`)

Textual app using `VerticalScroll` with dynamically mounted `Static` widgets. Streaming content updates a widget in-place via `widget.update()`, then swaps to rendered Markdown on completion. `CompletingInput` provides tab completion, `HintLine` shows inline command hints.

### Completion System (`completion.py`)

Shared by REPL and TUI: `complete_line()` for context-aware tab completion, `hint_for_input()` for inline hints, `is_incomplete_command()` to block sending partial command prefixes. Web UI reimplements these in JS.

## Test Infrastructure

- `tests/conftest.py`: `workspace` fixture (tmp_path with test files), `config`, `mock_client` (AsyncMock of OllamaClient), `pw_server_url` (session-scoped live server for Playwright)
- `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio` on tests
- Playwright tests use module-scoped server fixtures with `make_mock_agent()` for deterministic events
- Markers: `playwright` (browser tests, included by default), `e2e` (real Ollama, excluded by default)

## Key Patterns

- Commands (`/think`, `/workspace`, etc.) are handled by `CommandHandler` before reaching the agent loop
- `/think once` is stateful: sets thinking on for one turn, then `consume_think_once()` resets it at all exit points in `process_input()`
- Ollama communication uses raw `httpx` (no SDK) against `/api/chat` with NDJSON streaming
- The agent maintains `history` (list of message dicts) with token-budget truncation (~200k tokens)
