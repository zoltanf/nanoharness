"""Tests for nanoharness/completion.py — pure logic, no mocking needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoharness.completion import (
    COMMANDS,
    THINK_OPTIONS,
    COMMAND_HINTS,
    is_incomplete_command,
    hint_for_input,
    dir_matches,
    path_matches,
    complete_line,
    complete_token,
)


class TestIsIncompleteCommand:
    def test_partial_prefix(self):
        assert is_incomplete_command("/thi") is True

    def test_exact_match(self):
        assert is_incomplete_command("/think") is False

    def test_with_args(self):
        assert is_incomplete_command("/think on") is False

    def test_ambiguous(self):
        assert is_incomplete_command("/c") is True

    def test_exact_clear(self):
        assert is_incomplete_command("/clear") is False

    def test_bare_slash(self):
        assert is_incomplete_command("/") is True

    def test_unknown_command(self):
        assert is_incomplete_command("/foo") is False

    def test_not_a_command(self):
        assert is_incomplete_command("hello") is False

    def test_empty(self):
        assert is_incomplete_command("") is False

    def test_partial_quit(self):
        assert is_incomplete_command("/q") is True

    def test_exact_quit(self):
        assert is_incomplete_command("/quit") is False


class TestHintForInput:
    def test_partial_think(self):
        hint = hint_for_input("/thi")
        assert "/think" in hint
        assert "on|off|once" in hint

    def test_think_with_space(self):
        hint = hint_for_input("/think ")
        assert "on|off|once" in hint

    def test_think_partial_arg(self):
        hint = hint_for_input("/think o")
        assert "on" in hint
        assert "off" in hint
        assert "once" in hint

    def test_partial_workspace(self):
        hint = hint_for_input("/w")
        assert "/workspace" in hint
        assert "<dir>" in hint

    def test_ambiguous_c(self):
        hint = hint_for_input("/c")
        assert "/clear" in hint
        assert "/config" in hint

    def test_bare_slash(self):
        hint = hint_for_input("/")
        for cmd in COMMANDS:
            assert cmd in hint

    def test_plain_text(self):
        assert hint_for_input("hello") == ""

    def test_trailing_think(self):
        hint = hint_for_input("explain this /thi")
        assert "/think" in hint
        assert "on" in hint

    def test_empty(self):
        assert hint_for_input("") == ""

    def test_exact_command_no_args(self):
        hint = hint_for_input("/clear")
        assert "/clear" in hint
        assert "Clear conversation" in hint


class TestDirMatches:
    def test_empty_partial(self, workspace: Path):
        matches = dir_matches(workspace, "")
        assert "src/" in matches
        assert "tests_dir/" in matches
        # Files should NOT be in dir_matches
        assert "hello.py" not in matches
        # Hidden dirs filtered
        assert ".hidden/" not in matches

    def test_partial_match(self, workspace: Path):
        matches = dir_matches(workspace, "sr")
        assert "src/" in matches
        assert "tests_dir/" not in matches

    def test_no_match(self, workspace: Path):
        matches = dir_matches(workspace, "zzz")
        assert matches == []

    def test_nonexistent_parent(self, workspace: Path):
        matches = dir_matches(workspace, "nonexistent/sub")
        assert matches == []


class TestPathMatches:
    def test_empty_partial(self, workspace: Path):
        matches = path_matches(workspace, "")
        assert "hello.py" in matches
        assert "src/" in matches
        assert ".hidden/" not in matches

    def test_partial_match(self, workspace: Path):
        matches = path_matches(workspace, "he")
        assert "hello.py" in matches

    def test_dir_has_slash(self, workspace: Path):
        matches = path_matches(workspace, "sr")
        assert "src/" in matches

    def test_no_match(self, workspace: Path):
        matches = path_matches(workspace, "zzz")
        assert matches == []


class TestCompleteLine:
    def test_workspace_dir_completion(self, workspace: Path):
        matches = complete_line(workspace, "/workspace ")
        assert any("src/" in m for m in matches)
        # Should not include files
        assert not any("hello.py" in m for m in matches)

    def test_think_options(self, workspace: Path):
        matches = complete_line(workspace, "/think ")
        assert "/think on" in matches
        assert "/think off" in matches
        assert "/think once" in matches

    def test_command_prefix(self, workspace: Path):
        matches = complete_line(workspace, "/thi")
        assert "/think" in matches

    def test_shell_escape(self, workspace: Path):
        matches = complete_line(workspace, "!he")
        assert any("hello.py" in m for m in matches)

    def test_bare_path(self, workspace: Path):
        matches = complete_line(workspace, "he")
        assert "hello.py" in matches


class TestCompleteToken:
    def test_command_prefix(self, workspace: Path):
        matches = complete_token(workspace, "/c")
        assert "/clear" in matches
        assert "/config" in matches

    def test_all_commands(self, workspace: Path):
        matches = complete_token(workspace, "/")
        for cmd in COMMANDS:
            assert cmd in matches

    def test_shell_prefix(self, workspace: Path):
        matches = complete_token(workspace, "!")
        assert any(m.startswith("!") for m in matches)

    def test_path(self, workspace: Path):
        matches = complete_token(workspace, "he")
        assert "hello.py" in matches


class TestConstants:
    def test_commands_list(self):
        assert "/think" in COMMANDS
        assert "/workspace" in COMMANDS
        assert "/clear" in COMMANDS
        assert "/quit" in COMMANDS

    def test_think_options(self):
        assert THINK_OPTIONS == ["on", "off", "once"]

    def test_command_hints_coverage(self):
        for cmd in COMMANDS:
            assert cmd in COMMAND_HINTS
