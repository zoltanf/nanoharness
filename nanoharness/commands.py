"""Slash command handler shared between TUI and web UI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import CONFIG_KEYS, THEME_OPTIONS, TOOL_NAMES

if TYPE_CHECKING:
    from .config import Config
    from .tools import ToolExecutor


@dataclass
class CommandResult:
    """Result of a slash command execution."""
    output: str
    is_markdown: bool = False   # True → emit as 'markdown' event; False → 'status'
    should_quit: bool = False
    clear_history: bool = False
    shell_command: str | None = None
    workspace_changed: bool = False
    refresh_status: bool = False
    theme_changed: bool = False


HELP_TEXT = """\
## Commands

- `/safety [confirm|workspace|none]` — Show or set safety level for this session
- `/workspace [DIR]` — Show or switch workspace directory
- `/think [on|off|once]` — Toggle thinking mode; append to a message for one turn
- `!<cmd>` — Run a shell command directly (e.g. `!ls -la`)
- `/clear` — Clear conversation history

- `/todo add TEXT` — Add a task
- `/todo done ID` | `/todo remove ID` — Complete or remove a task by ID
- `/todo [list|clear]` — Show or clear the task list

- `/info [prompt|context|tools]` — Show model details, system prompt/context breakdown, or available tools
- `/code` — Open workspace in VS Code
- `/lazygit` — Open lazygit in a new terminal window

- `/config` — Show current configuration
- `/config set KEY VAL` — Edit a config value (saved to `~/.nanoharness/config.toml`)
- `/config tools` — Show tool enable/disable state *(TUI: opens interactive editor)*
- `/config tools TOOL [G] [W]` — Set global (G) and/or workspace (W) enable for a tool — values: `on` | `off` | `_` skip; workspace also accepts `inherit`

- `/pull [model|all]` — Pull a model (defaults to current); `all` pulls every local model
- `/update ollama` — Update Ollama to the latest version
- `/update models` — Pull all local models (alias for `/pull all`)

- `/quit` | `/exit` — Exit NanoHarness

## Key bindings — TUI

- `Enter` — Send message
- `Ctrl+J` — Insert newline
- `Tab` — Autocomplete command or path
- `PageUp` / `PageDown` — Scroll chat history
- `Home` / `End` — Jump to top / bottom of chat
- `Ctrl+C` — Copy selected text
- `Escape` — Interrupt running agent
- `Ctrl+Q` — Quit

## Key bindings — Web / Desktop App

- `Enter` — Send message
- `Shift+Enter` / `Alt+Enter` / `Ctrl+J` — Insert newline
- `Tab` — Autocomplete command or path
- `Up` / `Down` — Navigate input history
- `Escape` — Interrupt running agent
- `Cmd+C` / `Ctrl+C` — Copy selected text (standard browser/OS behavior)
"""

_LINUX_TERMINALS = [
    ("gnome-terminal", ["--"]),
    ("xterm", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("wezterm", ["start", "--"]),
]


class CommandHandler:
    def __init__(self, config: Config):
        self.config = config
        self._think_once = False
        self.tools: ToolExecutor | None = None  # set by Agent after construction

    @property
    def think_once_pending(self) -> bool:
        return self._think_once

    def consume_think_once(self) -> None:
        """Call after a turn completes to disable think-once."""
        if self._think_once:
            self._think_once = False
            self.config.model.thinking = False

    def is_command(self, text: str) -> bool:
        t = text.strip()
        return t.startswith("/") or t.startswith("!")

    def is_shell(self, text: str) -> bool:
        return text.strip().startswith("!")

    def handle(self, text: str) -> CommandResult:
        t = text.strip()

        # Shell escape: !<command>
        if t.startswith("!"):
            shell_cmd = t[1:].strip()
            if not shell_cmd:
                return CommandResult(output="Usage: `!<command>` (e.g. `!ls -la`)", is_markdown=True)
            return CommandResult(output="", shell_command=shell_cmd)

        parts = t.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        match cmd:
            case "/think":
                if arg.lower() == "once":
                    self._think_once = True
                    self.config.model.thinking = True
                    return CommandResult(output="Thinking mode: ON (once — will turn off after next response)")
                elif arg.lower() in ("on", "true", "yes"):
                    self._think_once = False
                    self.config.model.thinking = True
                    return CommandResult(output="Thinking mode: ON")
                elif arg.lower() in ("off", "false", "no"):
                    self._think_once = False
                    self.config.model.thinking = False
                    return CommandResult(output="Thinking mode: OFF")
                else:
                    # Toggle
                    self._think_once = False
                    self.config.model.thinking = not self.config.model.thinking
                    state = "ON" if self.config.model.thinking else "OFF"
                    return CommandResult(output=f"Thinking mode: {state}")

            case "/workspace":
                if not arg:
                    return CommandResult(
                        output=f"**Current workspace:** `{self.config.workspace}`\n\nUse `/workspace <path>` to switch directories.",
                        is_markdown=True,
                    )
                new_path = Path(arg).expanduser()
                if not new_path.is_absolute():
                    new_path = self.config.workspace / new_path
                new_path = new_path.resolve()
                if not new_path.is_dir():
                    return CommandResult(output=f"Error: not a directory: {new_path}")
                self.config.workspace = new_path
                return CommandResult(
                    output=f"Workspace changed to: {new_path}",
                    workspace_changed=True,
                )

            case "/clear":
                return CommandResult(output="Conversation cleared.", clear_history=True)

            case "/config":
                if not arg:
                    return CommandResult(output=self._config_show(), is_markdown=True)

                arg_parts = arg.split(maxsplit=3)
                first = arg_parts[0].lower()

                if first == "tools":
                    return self._config_tools_command(arg_parts[1:])

                if first == "theme":
                    return self._config_theme_command(arg_parts[1:])

                if first != "set" or len(arg_parts) < 3:
                    return CommandResult(
                        output=(
                            "**Usage:**\n\n"
                            "- `/config set <key> <value>`\n"
                            "- `/config tools [<tool> [global] [workspace]]`\n"
                            "- `/config theme light|dark|auto`\n\n"
                            "Type `/config` to see all keys and current values."
                        ),
                        is_markdown=True,
                    )

                key, value = arg_parts[1].lower(), arg_parts[2]
                err = self._config_set(key, value)
                if err:
                    return CommandResult(output=f"Error: {err}", is_markdown=True)

                from .config import write_config_toml, CONFIG_FILE
                write_config_toml(self.config)
                return CommandResult(
                    output=(
                        f"Set `{key}` = `{value}`\n\n"
                        f"Saved to `{CONFIG_FILE}`. Restart NanoHarness for changes to take effect."
                    ),
                    is_markdown=True,
                )

            case "/help":
                return CommandResult(output=HELP_TEXT, is_markdown=True)

            case "/code":
                import subprocess
                ws = str(self.config.workspace)
                try:
                    subprocess.Popen(["code", ws])
                    return CommandResult(output=f"Opening VS Code: {ws}")
                except FileNotFoundError:
                    return CommandResult(
                        output="Error: `code` not found. Install the VS Code CLI via *Shell Command: Install 'code' command in PATH*.",
                        is_markdown=True,
                    )

            case "/lazygit":
                import platform
                import shlex
                import shutil
                import subprocess
                if not shutil.which("lazygit"):
                    return CommandResult(
                        output="Error: `lazygit` not found. Install from [jesseduffield/lazygit](https://github.com/jesseduffield/lazygit).",
                        is_markdown=True,
                    )
                ws = shlex.quote(str(self.config.workspace))
                try:
                    if platform.system() == "Darwin":
                        script = f'tell application "Terminal" to do script "cd {ws} && lazygit"'
                        subprocess.Popen(["osascript", "-e", script])
                    elif platform.system() == "Linux":
                        for term, extra_args in _LINUX_TERMINALS:
                            if shutil.which(term):
                                cmd = [term] + extra_args + ["sh", "-c", f"cd {ws} && lazygit"]
                                subprocess.Popen(cmd)
                                break
                        else:
                            return CommandResult(
                                output="Error: No supported terminal emulator found (tried: gnome-terminal, xterm, kitty, alacritty, wezterm).",
                                is_markdown=True,
                            )
                    else:
                        return CommandResult(
                            output=f"Error: Unsupported platform `{platform.system()}`.",
                            is_markdown=True,
                        )
                    return CommandResult(output=f"Opening lazygit in new terminal: {self.config.workspace}")
                except Exception as e:
                    return CommandResult(output=f"Error launching lazygit: {e}", is_markdown=True)

            case "/todo":
                return self._todo_command(arg)

            case "/safety":
                if not arg:
                    level = self.config.safety.level
                    return CommandResult(
                        output=(
                            f"**Safety level:** `{level}`\n\n"
                            "| Level | Description |\n"
                            "|-------|-------------|\n"
                            "| `confirm` | Workspace restrictions + confirmation for bash/python/write |\n"
                            "| `workspace` | Workspace containment + env scrubbing *(default)* |\n"
                            "| `none` | No restrictions |\n\n"
                            "Use `/config set safety.level <value>` to save as startup default."
                        ),
                        is_markdown=True,
                        refresh_status=True,
                    )
                if arg not in ("confirm", "workspace", "none"):
                    return CommandResult(
                        output="Usage: `/safety [confirm|workspace|none]`",
                        is_markdown=True,
                    )
                self.config.safety.level = arg
                if self.tools:
                    self.tools.safety = arg
                return CommandResult(output=f"Safety: {arg}", refresh_status=True)

            case "/quit" | "/exit" | "/q":
                return CommandResult(output="Goodbye.", should_quit=True)

            case _:
                return CommandResult(
                    output=f"Unknown command: `{cmd}`. Type `/help` for available commands.",
                    is_markdown=True,
                )

    def _todo_command(self, arg: str) -> CommandResult:
        if self.tools is None:
            return CommandResult(output="Todo not available.")
        sub_parts = arg.split(maxsplit=1)
        sub = sub_parts[0].lower() if sub_parts else ""
        rest = sub_parts[1] if len(sub_parts) > 1 else ""

        match sub:
            case "" | "list":
                output = self._todo_list_to_md(self.tools._todo("list"))
                return CommandResult(output=output, is_markdown=True, refresh_status=True)
            case "clear":
                output = self.tools._todo("clear")
            case "add":
                if not rest:
                    return CommandResult(output="Usage: `/todo add <task text>`", is_markdown=True)
                output = self.tools._todo("add", task=rest)
            case "done":
                try:
                    output = self.tools._todo("complete", task_id=int(rest))
                except ValueError:
                    return CommandResult(output="Usage: `/todo done <id>`", is_markdown=True)
            case "remove":
                try:
                    output = self.tools._todo("remove", task_id=int(rest))
                except ValueError:
                    return CommandResult(output="Usage: `/todo remove <id>`", is_markdown=True)
            case _:
                return CommandResult(
                    output="Usage: `/todo [list|clear|add TEXT|done ID|remove ID]`",
                    is_markdown=True,
                )
        return CommandResult(output=output, refresh_status=True)

    @staticmethod
    def _todo_list_to_md(raw: str) -> str:
        """Convert plain todo list output to markdown."""
        if raw == "No tasks":
            return "*No tasks.*"
        lines = ["**Tasks**", ""]
        for line in raw.splitlines():
            m = re.match(r"#(\d+) \[(\w+)\] (.*)", line)
            if m:
                id_, status, task = m.groups()
                if status == "done":
                    lines.append(f"- `#{id_}` *{task}* *(done)*")
                else:
                    lines.append(f"- `#{id_}` {task}")
            else:
                lines.append(f"- {line}")
        return "\n".join(lines)

    def _config_show(self) -> str:
        think_state = "on" if self.config.model.thinking else "off"
        if self._think_once:
            think_state += " (once)"
        block = "\n".join([
            f"model.name            = {self.config.model.name}",
            f"model.thinking        = {think_state}",
            f"model.num_ctx         = {self.config.model.num_ctx}  (0 = model default)",
            f"agent.max_steps       = {self.config.agent.max_steps}",
            f"agent.timeout_seconds = {self.config.agent.timeout_seconds}",
            f"agent.max_output_chars= {self.config.agent.max_output_chars}",
            f"safety.level          = {self.config.safety.level}  (use /safety to change for this session)",
            f"ollama.base_url       = {self.config.ollama.base_url}",
            f"ui.theme              = {self.config.ui.theme}  (use /config theme to change)",
            f"workspace             = {self.config.workspace}  (use /workspace to change)",
        ])
        return (
            "## Configuration\n\n"
            f"```\n{block}\n```\n\n"
            "Usage: `/config set <key> <value>` · `/config tools [<tool> …]` · `/config theme light|dark|auto`\n\n"
            "*Restart NanoHarness for most changes to take effect.*"
        )

    def _config_tools_command(self, rest: list[str]) -> CommandResult:
        """Handle /config tools [<tool> [global] [workspace]]."""
        if not rest:
            return CommandResult(output=self._config_tools_show(), is_markdown=True)

        tool = rest[0].lower()
        if tool not in TOOL_NAMES:
            return CommandResult(
                output=f"Unknown tool `{tool}`. Available: {', '.join(f'`{n}`' for n in TOOL_NAMES)}",
                is_markdown=True,
            )

        g_arg = rest[1].lower() if len(rest) >= 2 else "_"
        w_arg = rest[2].lower() if len(rest) >= 3 else "_"

        # Validate
        if g_arg not in ("on", "off", "_"):
            return CommandResult(
                output=f"Invalid global value `{g_arg}`. Use: `on` | `off` | `_` (skip)",
                is_markdown=True,
            )
        if w_arg not in ("on", "off", "inherit", "_"):
            return CommandResult(
                output=f"Invalid workspace value `{w_arg}`. Use: `on` | `off` | `inherit` | `_` (skip)",
                is_markdown=True,
            )

        from .config import write_config_toml, CONFIG_FILE
        msgs = []

        if g_arg != "_":
            setattr(self.config.tools, tool, g_arg == "on")
            write_config_toml(self.config)
            msgs.append(f"Global `{tool}` = `{g_arg}` *(saved to `{CONFIG_FILE}`)*")

        if w_arg != "_" and self.tools is not None:
            ws = self.tools._load_workspace_tools()
            if w_arg == "inherit":
                ws.pop(tool, None)
            else:
                ws[tool] = (w_arg == "on")
            self.tools._save_workspace_tools(ws)
            msgs.append(f"Workspace `{tool}` = `{w_arg}` *(saved to `.nanoharness/tools.json`)*")

        if not msgs:
            return CommandResult(
                output="Nothing changed (both columns skipped with `_`).",
                is_markdown=True,
            )
        return CommandResult(output="\n\n".join(msgs), is_markdown=True)

    def _config_tools_show(self) -> str:
        """List all tools with global and workspace enable state as a markdown table."""
        ws = self.tools._load_workspace_tools() if self.tools else {}
        rows = ["## Tools", "", "| Tool | Global | Workspace | Effective |", "|------|--------|-----------|-----------|"]
        for name in TOOL_NAMES:
            g_val = getattr(self.config.tools, name, True)
            g = "on" if g_val else "off"
            w = ("on" if ws[name] else "off") if name in ws else "inherit"
            eff = "on" if ws.get(name, g_val) else "off"
            rows.append(f"| `{name}` | {g} | {w} | {eff} |")
        rows += [
            "",
            "Set: `/config tools <tool> [global] [workspace]` — values: `on` | `off` | `_` skip; workspace also accepts `inherit`",
            "",
            "*TUI interactive editor: type `/config tools` with no arguments.*",
        ]
        return "\n".join(rows)

    def _config_theme_command(self, rest: list[str]) -> CommandResult:
        """Handle /config theme [light|dark|auto]."""
        if not rest:
            return CommandResult(
                output=f"Theme: {self.config.ui.theme}  (options: light | dark | auto)"
            )
        value = rest[0].lower()
        if value not in THEME_OPTIONS:
            return CommandResult(
                output=f"Invalid theme `{value}`. Use: `light` | `dark` | `auto`",
                is_markdown=True,
            )
        self.config.ui.theme = value
        from .config import write_config_toml, CONFIG_FILE
        write_config_toml(self.config)
        return CommandResult(
            output=f"Theme set to '{value}' (saved to {CONFIG_FILE})",
            theme_changed=True,
        )

    @staticmethod
    def _parse_int(value: str) -> int:
        """Parse integer with optional k/K suffix (e.g. '128k' → 131072)."""
        v = value.strip()
        if v.lower().endswith("k"):
            return int(v[:-1]) * 1024
        return int(v)

    def _config_set(self, key: str, value: str) -> str | None:
        """Apply key=value to config in-memory. Returns error string or None on success."""
        if key.startswith("/"):
            return f"Usage: /config set <key> <value>  (got extra '/config set' prefix?)"
        match key:
            case "model.name":
                self.config.model.name = value
            case "model.thinking":
                if value.lower() not in ("on", "off", "true", "false", "yes", "no", "1", "0"):
                    return f"Invalid value '{value}'. Use: on / off"
                self.config.model.thinking = value.lower() in ("on", "true", "yes", "1")
            case "model.num_ctx":
                try:
                    self.config.model.num_ctx = self._parse_int(value)
                except ValueError:
                    return f"Invalid value '{value}'. Must be an integer (e.g. 131072 or 128k). Use 0 for model default."
            case "agent.max_steps":
                try:
                    self.config.agent.max_steps = self._parse_int(value)
                except ValueError:
                    return f"Invalid value '{value}'. Must be an integer."
            case "agent.timeout_seconds":
                try:
                    self.config.agent.timeout_seconds = self._parse_int(value)
                except ValueError:
                    return f"Invalid value '{value}'. Must be an integer."
            case "agent.max_output_chars":
                try:
                    self.config.agent.max_output_chars = self._parse_int(value)
                except ValueError:
                    return f"Invalid value '{value}'. Must be an integer (e.g. 8000 or 8k)."
            case "safety.level":
                if value not in ("confirm", "workspace", "none"):
                    return f"Invalid value '{value}'. Use: confirm / workspace / none"
                self.config.safety.level = value
            case "ollama.base_url":
                self.config.ollama.base_url = value
            case "ui.theme":
                if value not in THEME_OPTIONS:
                    return f"Invalid value '{value}'. Use: light / dark / auto"
                self.config.ui.theme = value
            case _:
                return f"Unknown key '{key}'. Type /config to see all keys."
        return None
