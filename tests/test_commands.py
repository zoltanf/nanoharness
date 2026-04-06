"""Tests for nanoharness/commands.py — CommandHandler and CommandResult."""

from __future__ import annotations

from pathlib import Path

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
        assert "Model:" in r.output
        assert "Thinking:" in r.output
        assert "Safety:" in r.output
        assert "Workspace:" in r.output

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


class TestUnknownCommand:
    def test_unknown(self, handler: CommandHandler):
        r = handler.handle("/foo")
        assert "Unknown command" in r.output
