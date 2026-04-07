# NanoHarness Project Summary

**NanoHarness** is a lightweight AI coding agent designed to run locally using **Ollama**.

It acts as an autonomous loop that can interact with your local system to perform coding tasks, run commands, and manage files. It is built to be versatile, offering three different interfaces: a **Textual TUI** (Terminal User Interface), a **Web UI** (via WebSockets), and a simple **REPL**.

### Key Capabilities:
* **Tool Use:** The agent can execute `bash` commands, `read_file`, `write_file`, `list_dir`, execute `python_exec` code, and manage a `todo` list.
* **Local & Private:** It relies on Ollram, meaning all LLM processing stays on your local machine.
* **Safety-Focused:** It features a sandboxed "workspace" mode, restricting file and shell operations to a specific directory to prevent accidental damage to your system.
* **Streaming & Thinking:** It supports streaming responses and can handle "thinking" modes (for models that support extended reasoning).
* **Configuration:** It is highly configurable via a `config.toml` file (stored in `~/.nanoharness/`), allowing you to tune model parameters, agent behavior, and safety levels.

### Core Tech Stack:
* **Language:** Python 3.12+
* **Dependency Management:** `uv`
* **LLM Backend:** Ollama
* **Interfaces:** Textual (TUI), Web (Web UI), and standard Python REPL.
