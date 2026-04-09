"""Tests for nanoharness/commands.py — CommandHandler and CommandResult."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nanoharness.commands import CommandHandler, CommandResult, HELP_TEXT
from nanoharness.config import Config


@pytest.fixture
def handler(config: Config) -> CommandHandler:
    return CommandHandler(config)


class TestCommandResult:
    def test_defaults(self):
        r = CommandResult(output="hello")
        assert r.output == "hello"
        assert r.should_quit is False
        assert r.clear_history is False
        assert r.shell_command is None
        assert r.workspace_changed is False

    def test_all_fields(self):
        r = CommandResult(
            output="x", should_quit=True, clear_history=True,
            shell_command="ls", workspace_changed=True,
        )
        assert r.should_quit is True
        assert r.shell_command == "ls"


class TestIsCommand:
    def test_slash(self, handler: CommandHandler):
        assert handler.is_command("/think") is True

    def test_bang(self, handler: CommandHandler):
        assert handler.is_command("!ls") is True

    def test_plain(self, handler: CommandHandler):
        assert handler.is_command("hello") is False

    def test_whitespace(self, handler: CommandHandler):
        assert handler.is_command("  /think  ") is True


class TestIsShell:
    def test_bang(self, handler: CommandHandler):
        assert handler.is_shell("!ls") is True

    def test_slash(self, handler: CommandHandler):
        assert handler.is_shell("/think") is False


class TestThinkCommand:
    def test_toggle_on(self, handler: CommandHandler):
        assert handler.config.model.thinking is False
        r = handler.handle("/think")
        assert "ON" in r.output
        assert handler.config.model.thinking is True

    def test_toggle_off(self, handler: CommandHandler):
        handler.config.model.thinking = True
        r = handler.handle("/think")
        assert "OFF" in r.output
        assert handler.config.model.thinking is False

    def test_explicit_on(self, handler: CommandHandler):
        r = handler.handle("/think on")
        assert "ON" in r.output
        assert handler.config.model.thinking is True

    def test_explicit_off(self, handler: CommandHandler):
        handler.config.model.thinking = True
        r = handler.handle("/think off")
        assert "OFF" in r.output
        assert handler.config.model.thinking is False

    def test_once(self, handler: CommandHandler):
        r = handler.handle("/think once")
        assert "once" in r.output.lower()
        assert handler.config.model.thinking is True
        assert handler.think_once_pending is True

    def test_consume_think_once(self, handler: CommandHandler):
        handler.handle("/think once")
        assert handler.think_once_pending is True
        handler.consume_think_once()
        assert handler.think_once_pending is False
        assert handler.config.model.thinking is False

    def test_consume_noop_when_not_pending(self, handler: CommandHandler):
        handler.config.model.thinking = True
        handler.consume_think_once()
        # Should not change thinking if think_once was not set
        assert handler.config.model.thinking is True

    def test_double_toggle(self, handler: CommandHandler):
        handler.handle("/think")
        handler.handle("/think")
        assert handler.config.model.thinking is False


class TestWorkspaceCommand:
    def test_no_arg(self, handler: CommandHandler):
        r = handler.handle("/workspace")
        assert "Current workspace" in r.output
        assert "Usage" in r.output
        assert r.workspace_changed is False

    def test_valid_dir(self, handler: CommandHandler, workspace: Path):
        subdir = workspace / "src"
        r = handler.handle(f"/workspace {subdir}")
        assert r.workspace_changed is True
        assert handler.config.workspace == subdir

    def test_nonexistent(self, handler: CommandHandler):
        r = handler.handle("/workspace /nonexistent/path")
        assert "Error" in r.output
        assert r.workspace_changed is False

    def test_relative_path(self, handler: CommandHandler, workspace: Path):
        r = handler.handle("/workspace src")
        assert r.workspace_changed is True
        assert handler.config.workspace == (workspace / "src").resolve()

    def test_tilde_expansion(self, handler: CommandHandler):
        r = handler.handle("/workspace ~")
        assert r.workspace_changed is True
        assert handler.config.workspace == Path.home()


class TestClearCommand:
    def test_clear(self, handler: CommandHandler):
        r = handler.handle("/clear")
        assert r.clear_history is True
        assert "cleared" in r.output.lower()


class TestConfigCommand:
    def test_shows_fields(self, handler: CommandHandler):
        r = handler.handle("/config")
        assert "model.name" in r.output
        assert "model.thinking" in r.output
        assert "safety.level" in r.output
        assert "workspace" in r.output

    def test_shows_once(self, handler: CommandHandler):
        handler.handle("/think once")
        r = handler.handle("/config")
        assert "once" in r.output


class TestHelpCommand:
    def test_help(self, handler: CommandHandler):
        r = handler.handle("/help")
        assert r.output == HELP_TEXT


class TestQuitCommand:
    def test_quit(self, handler: CommandHandler):
        r = handler.handle("/quit")
        assert r.should_quit is True

    def test_exit(self, handler: CommandHandler):
        r = handler.handle("/exit")
        assert r.should_quit is True

    def test_q(self, handler: CommandHandler):
        r = handler.handle("/q")
        assert r.should_quit is True


class TestShellEscape:
    def test_basic(self, handler: CommandHandler):
        r = handler.handle("!ls")
        assert r.shell_command == "ls"

    def test_empty(self, handler: CommandHandler):
        r = handler.handle("!")
        assert r.shell_command is None
        assert "Usage" in r.output

    def test_with_args(self, handler: CommandHandler):
        r = handler.handle("!ls -la /tmp")
        assert r.shell_command == "ls -la /tmp"


class TestLazygitCommand:
    def test_not_installed(self, handler: CommandHandler):
        with patch("shutil.which", return_value=None):
            r = handler.handle("/lazygit")
        assert "Error" in r.output
        assert "https://github.com/jesseduffield/lazygit" in r.output

    def test_macos_opens_terminal(self, handler: CommandHandler):
        with (
            patch("shutil.which", return_value="/usr/local/bin/lazygit"),
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.Popen") as mock_popen,
        ):
            r = handler.handle("/lazygit")
        assert r.should_quit is False
        assert "lazygit" in r.output.lower()
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "osascript"
        assert "lazygit" in args[2]

    def test_linux_opens_terminal(self, handler: CommandHandler):
        def which_side_effect(name):
            if name == "lazygit":
                return "/usr/bin/lazygit"
            if name == "gnome-terminal":
                return "/usr/bin/gnome-terminal"
            return None

        with (
            patch("shutil.which", side_effect=which_side_effect),
            patch("platform.system", return_value="Linux"),
            patch("subprocess.Popen") as mock_popen,
        ):
            r = handler.handle("/lazygit")
        assert r.should_quit is False
        assert "lazygit" in r.output.lower()
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "gnome-terminal"

    def test_linux_no_terminal(self, handler: CommandHandler):
        def which_side_effect(name):
            if name == "lazygit":
                return "/usr/bin/lazygit"
            return None

        with (
            patch("shutil.which", side_effect=which_side_effect),
            patch("platform.system", return_value="Linux"),
        ):
            r = handler.handle("/lazygit")
        assert "Error" in r.output
        assert "terminal" in r.output.lower()

    def test_unsupported_platform(self, handler: CommandHandler):
        with (
            patch("shutil.which", return_value="/usr/bin/lazygit"),
            patch("platform.system", return_value="Windows"),
        ):
            r = handler.handle("/lazygit")
        assert "Error" in r.output
        assert "Unsupported" in r.output


class TestSafetyCommand:
    def test_no_arg_shows_level(self, handler: CommandHandler):
        r = handler.handle("/safety")
        assert "workspace" in r.output
        assert "confirm" in r.output
        assert "none" in r.output

    def test_set_none(self, handler: CommandHandler):
        r = handler.handle("/safety none")
        assert "none" in r.output
        assert handler.config.safety.level == "none"

    def test_set_confirm(self, handler: CommandHandler):
        r = handler.handle("/safety confirm")
        assert "confirm" in r.output
        assert handler.config.safety.level == "confirm"

    def test_set_workspace(self, handler: CommandHandler):
        handler.config.safety.level = "none"
        r = handler.handle("/safety workspace")
        assert "workspace" in r.output
        assert handler.config.safety.level == "workspace"

    def test_invalid_level(self, handler: CommandHandler):
        r = handler.handle("/safety unrestricted")
        assert "Usage" in r.output
        assert handler.config.safety.level == "workspace"  # unchanged

    def test_updates_tools_executor(self, handler: CommandHandler, workspace):
        from nanoharness.tools import ToolExecutor
        handler.tools = ToolExecutor(workspace=workspace)
        handler.handle("/safety none")
        assert handler.tools.safety == "none"

    def test_config_set_saves_valid_levels(self, handler: CommandHandler):
        from unittest.mock import patch
        with patch("nanoharness.config.write_config_toml"):
            r = handler.handle("/config set safety.level none")
            assert "Error" not in r.output
            r = handler.handle("/config set safety.level confirm")
            assert "Error" not in r.output
            r = handler.handle("/config set safety.level workspace")
            assert "Error" not in r.output

    def test_config_set_rejects_unrestricted(self, handler: CommandHandler):
        from unittest.mock import patch
        with patch("nanoharness.config.write_config_toml"):
            r = handler.handle("/config set safety.level unrestricted")
            assert "Error" in r.output


class TestUnknownCommand:
    def test_unknown(self, handler: CommandHandler):
        r = handler.handle("/foo")
        assert "Unknown command" in r.output


class TestConfigToolsCommand:
    @pytest.fixture
    def handler_with_tools(self, config: Config, workspace: Path) -> CommandHandler:
        from nanoharness.tools import ToolExecutor
        h = CommandHandler(config)
        h.tools = ToolExecutor(workspace=workspace)
        return h

    def test_list_all_enabled(self, handler_with_tools: CommandHandler):
        r = handler_with_tools.handle("/config tools")
        assert "bash" in r.output
        assert "python_exec" in r.output
        assert "global" in r.output.lower() or "/" in r.output

    def test_list_shows_inherit(self, handler_with_tools: CommandHandler):
        r = handler_with_tools.handle("/config tools")
        assert "inherit" in r.output

    def test_list_shows_effective(self, handler_with_tools: CommandHandler):
        r = handler_with_tools.handle("/config tools")
        # All tools enabled by default, effective = on
        assert "on" in r.output

    def test_set_global_off(self, handler_with_tools: CommandHandler):
        from unittest.mock import patch
        with patch("nanoharness.config.write_config_toml"):
            r = handler_with_tools.handle("/config tools bash off")
        assert "Error" not in r.output
        assert handler_with_tools.config.tools.bash is False

    def test_set_global_on(self, handler_with_tools: CommandHandler):
        handler_with_tools.config.tools.bash = False
        from unittest.mock import patch
        with patch("nanoharness.config.write_config_toml"):
            r = handler_with_tools.handle("/config tools bash on")
        assert handler_with_tools.config.tools.bash is True

    def test_set_workspace_off(self, handler_with_tools: CommandHandler):
        from unittest.mock import patch
        with patch("nanoharness.config.write_config_toml"):
            r = handler_with_tools.handle("/config tools python_exec _ off")
        assert "Error" not in r.output
        ws = handler_with_tools.tools._load_workspace_tools()
        assert ws.get("python_exec") is False

    def test_set_workspace_inherit_removes_override(self, handler_with_tools: CommandHandler):
        handler_with_tools.tools._save_workspace_tools({"bash": False})
        from unittest.mock import patch
        with patch("nanoharness.config.write_config_toml"):
            r = handler_with_tools.handle("/config tools bash _ inherit")
        assert "Error" not in r.output
        ws = handler_with_tools.tools._load_workspace_tools()
        assert "bash" not in ws

    def test_skip_both_columns(self, handler_with_tools: CommandHandler):
        r = handler_with_tools.handle("/config tools bash _ _")
        assert "Nothing changed" in r.output

    def test_unknown_tool(self, handler_with_tools: CommandHandler):
        r = handler_with_tools.handle("/config tools nonexistent off")
        assert "Unknown tool" in r.output or "Error" in r.output

    def test_invalid_global_value(self, handler_with_tools: CommandHandler):
        r = handler_with_tools.handle("/config tools bash maybe")
        assert "Error" in r.output or "Invalid" in r.output

    def test_invalid_workspace_value(self, handler_with_tools: CommandHandler):
        r = handler_with_tools.handle("/config tools bash _ maybe")
        assert "Error" in r.output or "Invalid" in r.output

    def test_no_tools_executor(self, handler: CommandHandler):
        """Listing tools without ToolExecutor attached still works."""
        r = handler.handle("/config tools")
        assert "bash" in r.output
