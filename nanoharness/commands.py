"""Slash command handler shared between TUI and web UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .tools import ToolExecutor


@dataclass
class CommandResult:
    """Result of a slash command execution."""
    output: str
    should_quit: bool = False
    clear_history: bool = False
    shell_command: str | None = None
    workspace_changed: bool = False
    refresh_status: bool = False


HELP_TEXT = """/think              - Toggle thinking mode (on/off/once)
/workspace DIR      - Switch workspace directory
/code               - Open workspace in VS Code
/clear              - Clear conversation history
/config             - Show current configuration
/config set KEY VAL - Edit a config value (persists to ~/.nanoharness/config.toml)
/info               - Show model details from Ollama (api/ps + api/show)
/todo               - Show current task list
/todo clear         - Remove all tasks
/todo add TEXT      - Add a task
/todo done ID       - Mark a task done
/todo remove ID     - Remove a task
/help               - Show this help
/quit               - Exit NanoHarness
!<cmd>              - Run shell command directly (e.g. !ls -la)
<msg> /think once   - Think for this message only"""

CONFIG_KEYS = [
    "model.name",
    "model.thinking",
    "model.num_ctx",
    "agent.max_steps",
    "agent.timeout_seconds",
    "agent.max_output_chars",
    "safety.level",
    "ollama.base_url",
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
                return CommandResult(output="Usage: !<command> (e.g. !ls -la)")
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
                    return CommandResult(output=f"Current workspace: {self.config.workspace}\nUsage: /workspace <path>")
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
                    return CommandResult(output=self._config_show())

                set_parts = arg.split(maxsplit=2)
                if set_parts[0].lower() != "set" or len(set_parts) < 3:
                    return CommandResult(
                        output="Usage: /config set <key> <value>\nType /config to see all keys and current values."
                    )

                key, value = set_parts[1].lower(), set_parts[2]
                err = self._config_set(key, value)
                if err:
                    return CommandResult(output=f"Error: {err}")

                from .config import write_config_toml, CONFIG_FILE
                write_config_toml(self.config)
                return CommandResult(
                    output=(
                        f"Set {key} = {value}\n"
                        f"Saved to {CONFIG_FILE}\n"
                        "Restart NanoHarness for changes to take effect."
                    )
                )

            case "/help":
                return CommandResult(output=HELP_TEXT)

            case "/code":
                import subprocess
                ws = str(self.config.workspace)
                try:
                    subprocess.Popen(["code", ws])
                    return CommandResult(output=f"Opening VS Code: {ws}")
                except FileNotFoundError:
                    return CommandResult(output="Error: 'code' not found. Install the VS Code CLI via: Shell Command: Install 'code' command in PATH")

            case "/todo":
                return self._todo_command(arg)

            case "/quit" | "/exit" | "/q":
                return CommandResult(output="Goodbye.", should_quit=True)

            case _:
                return CommandResult(output=f"Unknown command: {cmd}. Type /help for available commands.")

    def _todo_command(self, arg: str) -> CommandResult:
        if self.tools is None:
            return CommandResult(output="Todo not available.")
        sub_parts = arg.split(maxsplit=1)
        sub = sub_parts[0].lower() if sub_parts else ""
        rest = sub_parts[1] if len(sub_parts) > 1 else ""

        match sub:
            case "" | "list":
                output = self.tools._todo("list")
            case "clear":
                self.tools._save_todo([])
                output = "All tasks cleared."
            case "add":
                if not rest:
                    return CommandResult(output="Usage: /todo add <task text>")
                output = self.tools._todo("add", task=rest)
            case "done":
                try:
                    output = self.tools._todo("complete", task_id=int(rest))
                except ValueError:
                    return CommandResult(output="Usage: /todo done <id>")
            case "remove":
                try:
                    output = self.tools._todo("remove", task_id=int(rest))
                except ValueError:
                    return CommandResult(output="Usage: /todo remove <id>")
            case _:
                return CommandResult(output="Usage: /todo [list|clear|add TEXT|done ID|remove ID]")
        return CommandResult(output=output, refresh_status=True)

    def _config_show(self) -> str:
        think_state = "on" if self.config.model.thinking else "off"
        if self._think_once:
            think_state += " (once)"
        lines = [
            "Configuration  (key = value)",
            f"  model.name            = {self.config.model.name}",
            f"  model.thinking        = {think_state}",
            f"  model.num_ctx         = {self.config.model.num_ctx}  (0 = model default)",
            f"  agent.max_steps       = {self.config.agent.max_steps}",
            f"  agent.timeout_seconds = {self.config.agent.timeout_seconds}",
            f"  agent.max_output_chars= {self.config.agent.max_output_chars}",
            f"  safety.level          = {self.config.safety.level}",
            f"  ollama.base_url       = {self.config.ollama.base_url}",
            f"  workspace             = {self.config.workspace}  (use /workspace to change)",
            "",
            "Usage: /config set <key> <value>",
            "Restart NanoHarness for changes to take effect.",
        ]
        return "\n".join(lines)

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
                if value not in ("workspace", "unrestricted", "confirm"):
                    return f"Invalid value '{value}'. Use: workspace / unrestricted / confirm"
                self.config.safety.level = value
            case "ollama.base_url":
                self.config.ollama.base_url = value
            case _:
                return f"Unknown key '{key}'. Type /config to see all keys."
        return None
