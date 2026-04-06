# NanoHarness

Lightweight AI coding agent for local LLMs via [Ollama](https://ollama.com). Runs in a Textual TUI, a web UI, or a plain REPL — all driven by the same streaming agent loop.

## Features

- Streaming responses with thinking mode support
- Tool use: `bash`, `read_file`, `write_file`, `list_dir`, `python_exec`, `todo`
- Workspace safety: file/shell ops sandboxed to a working directory
- Tab completion and inline command hints
- Web UI with WebSocket streaming (optional)

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com) running locally with at least one model

## Install

```bash
git clone https://github.com/zoltanf/nanoharness.git
cd nanoharness
uv sync
```

## Run

```bash
# TUI (default)
uv run python -m nanoharness

# Web UI
uv run --extra web python -m nanoharness --web

# Basic REPL
uv run python -m nanoharness --repl
```

## Options

```
--model MODEL       Ollama model name (default: from config or gemma3)
--think / --no-think  Enable extended thinking mode
--workspace DIR     Working directory for file/shell tools
--web               Launch web UI instead of TUI
--port PORT         Web UI port (default: 8000)
--repl              Use plain stdin/stdout REPL
--debug             Enable debug logging to ~/.nanoharness/
```

## Slash commands

| Command | Description |
|---|---|
| `/think [on\|off\|once]` | Toggle thinking mode |
| `/workspace <dir>` | Switch working directory |
| `/clear` | Clear conversation history |
| `/config [set KEY VAL]` | Show or edit configuration |
| `/info` | Show model details from Ollama |
| `/help` | List all commands |
| `/quit` | Exit |
| `!<cmd>` | Run a shell command directly |

## Configuration

Persisted at `~/.nanoharness/config.toml`. Edit live with `/config set <key> <value>`:

```toml
[model]
name = "gemma3"
thinking = false
num_ctx = 0       # 0 = model default

[agent]
max_steps = 20
timeout_seconds = 120
max_output_chars = 8000

[safety]
level = "workspace"   # workspace | unrestricted

[ollama]
base_url = "http://localhost:11434"
```

## Tests

```bash
# Unit tests
uv run --extra test pytest tests/ -q -m "not playwright"

# With browser (Playwright) tests
uv run --extra test --extra playwright --extra web pytest tests/ -v

# Install Playwright browsers (one-time)
uv run --extra playwright playwright install chromium
```

## License

MIT
