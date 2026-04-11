# NanoHarness

Lightweight AI coding agent for local LLMs via [Ollama](https://ollama.com). Runs in a Textual TUI, a web UI, or a plain REPL — all driven by the same streaming agent loop.

## Features

- Streaming responses with thinking mode support
- Tool use: `bash`, `read_file`, `write_file`, `list_dir`, `python_exec`, `todo`, `fetch_webpage`
- Workspace safety: file/shell ops sandboxed to a working directory
- Tab completion and inline command hints
- Web UI with WebSocket streaming (optional)
- Auto-start Ollama at launch (brew services or background `ollama serve`)
- Automatic reconnect if Ollama goes away mid-session

## macOS install and release

Install the packaged app with Homebrew:

```bash
brew tap zoltanf/nanoharness
brew install --cask nanoharness
```

Install just the terminal command:

```bash
brew tap zoltanf/nanoharness
brew install nanoh
```

If macOS blocks the app on first launch because the build is not notarized yet, unblock it in one of these ways:

Option A: Terminal

```bash
sudo xattr -r -d com.apple.quarantine "/Applications/NanoHarness.app"
sudo xattr -r -d com.apple.quarantine "/opt/homebrew/bin/nanoh"
```

Option B: System Settings

Open `System Settings` -> `Privacy & Security`, scroll to the bottom, and click `Open Anyway` for `NanoHarness.app`.

The macOS build chain now defaults to `x86_64` then `arm64` on Apple Silicon, and `x86_64` only on Intel Macs. A local build writes the app bundle, CLI, and installer into architecture-specific folders:

- `dist/macos/arm64/`
- `dist/macos/x86_64/`

It also writes shared Homebrew assets and checksums at the top level of `dist/macos/`.

Run a local build with:

```bash
./scripts/build-macos.sh
```

The generated build version uses local time and looks like `2026.04.11.1229`.

On Apple Silicon, `build-macos.sh` automatically prefers `/usr/local/bin/uv` for the `x86_64` pass when that binary exists. If it does not find one, it will try to install an Intel-capable `uv` into your user bin with Rosetta Python on the first run. The Intel build then uses a uv-managed `x86_64` Python in `build/macos/managed-python/x86_64` so it does not accidentally pick up your arm64 Homebrew interpreter. That first Intel pass may need network access, and Rosetta must be installed. If your Intel `uv` lives somewhere else, point the script at it:

```bash
export NANOHARNESS_UV_BIN_X86_64="/usr/local/bin/uv"
./scripts/build-macos.sh
```

If Rosetta is missing:

```bash
softwareupdate --install-rosetta
```

If you only want one architecture for a quick build:

```bash
export NANOHARNESS_TARGET_ARCHES="arm64"
./scripts/build-macos.sh
```

To publish a release after building:

```bash
gh auth login -h github.com
./scripts/publish-github-release.sh
./scripts/publish-homebrew-tap.sh
```

For signing, notarization, and the full release flow, see [docs/releasing-macos.md](docs/releasing-macos.md).

## Development

### Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com) running locally with at least one model

### Setup

```bash
git clone https://github.com/zoltanf/nanoharness.git
cd nanoharness
uv sync
```

If you install the package as a command-line tool, both `nanoharness` and `nanoh` point to the terminal entrypoint.

### Run

```bash
# TUI (default)
uv run python -m nanoharness

# Web UI
uv run --extra web python -m nanoharness --web

# Basic REPL
uv run python -m nanoharness --repl
```

### Options

```
--model MODEL       Ollama model name (default: from config or gemma3)
--think / --no-think  Enable extended thinking mode
--workspace DIR     Working directory for file/shell tools
--web               Launch web UI instead of TUI
--port PORT         Web UI port (default: 8000)
--repl              Use plain stdin/stdout REPL
--debug             Enable debug logging to ~/.nanoharness/
```

### Slash commands

| Command | Description |
|---|---|
| `/think [on\|off\|once]` | Toggle thinking mode |
| `/workspace <dir>` | Switch working directory |
| `/pull [model\|all]` | Pull a model; `all` updates every locally installed model |
| `/update ollama` | Update the Ollama binary (detects brew vs manual install) |
| `/update models` | Alias for `/pull all` |
| `/info [prompt\|tools]` | Show model details, current system prompt, or available tools |
| `/clear` | Clear conversation history |
| `/config [set KEY VAL]` | Show or edit configuration |
| `/safety <level>` | Set safety level for this session |
| `/help` | List all commands |
| `/quit` | Exit |
| `!<cmd>` | Run a shell command directly |

### Configuration

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
level = "workspace"   # workspace | confirm | none

[ollama]
base_url = "http://localhost:11434"
```

### Optional extras

| Extra | Purpose |
|-------|---------|
| `web` | FastAPI web UI |
| `web-reader` | `fetch_webpage` tool (installs `trafilatura`) |
| `test` | pytest suite |
| `playwright` | browser tests |

```bash
# Enable web page fetching
uv run --extra web-reader python -m nanoharness
```

### Tests

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
