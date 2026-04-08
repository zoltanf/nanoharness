# NanoHarness Project Summary

**NanoHarness** is a lightweight AI coding agent designed to run locally using **Ollama**.

It acts as an autonomous loop that can interact with your local system to perform coding tasks, run commands, and manage files. It is built to be versatile, offering three different interfaces: a **Textual TUI** (Terminal User Interface), a **Web UI** (via WebSockets), and a simple **REPL**.

### Key Capabilities

* **Tool Use:** The agent can execute `bash` commands, `read_file`, `write_file`, `list_dir`, execute `python_exec` code, and manage a `todo` list.
* **Local & Private:** Relies on Ollama — all LLM processing stays on your local machine.
* **Safety-Focused:** Features a sandboxed "workspace" mode, restricting file and shell operations to a specific directory. A `confirm` level adds per-tool approval prompts.
* **Streaming & Thinking:** Supports streaming responses and extended "thinking" mode for compatible models.
* **Model Management:** `/pull [model|all]` pulls models from Ollama; `/update ollama` updates the Ollama binary (detects brew vs manual install); `/update models` refreshes all locally installed models at once.
* **Ollama Lifecycle:** If Ollama is not running at startup, NanoHarness detects whether it was installed via Homebrew (`brew services start ollama`) or directly (`ollama serve`) and offers to start it automatically. If Ollama drops mid-session, it reconnects automatically with exponential back-off.
* **Configuration:** Highly configurable via `config.toml` (`~/.nanoharness/config.toml`), with overrides from CLI flags and `NANO_*` environment variables.

### Slash Commands

| Command | Description |
|---|---|
| `/think [on\|off\|once]` | Toggle extended thinking mode |
| `/workspace <dir>` | Switch working directory |
| `/pull [model\|all]` | Pull a model; `all` updates every locally installed model |
| `/update ollama` | Update the Ollama binary |
| `/update models` | Alias for `/pull all` |
| `/info` | Show model details, Ollama server version, and URL |
| `/clear` | Clear conversation history |
| `/config [set KEY VAL]` | Show or edit configuration |
| `/safety <level>` | Set safety level for this session |
| `/help` | List all commands |
| `/quit` | Exit |

### Core Tech Stack

* **Language:** Python 3.12+
* **Dependency Management:** `uv`
* **LLM Backend:** Ollama (via raw `httpx` against `/api/chat`)
* **Interfaces:** Textual (TUI), FastAPI + WebSocket (Web UI), plain stdin/stdout (REPL)
* **Testing:** pytest with `asyncio_mode = "auto"`, Playwright for browser tests
