"""Tests for nanoharness/completion.py — pure logic, no mocking needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoharness.completion import (
    COMMANDS,
    THINK_OPTIONS,
    COMMAND_HINTS,
    is_incomplete_command,
    command_send_error,
    hint_for_input,
    abs_dir_matches,
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
        assert is_incomplete_command("/foo") is True

    def test_invalid_think_arg(self):
        assert is_incomplete_command("/think xyz") is True

    def test_valid_think_arg(self):
        assert is_incomplete_command("/think on") is False
        assert is_incomplete_command("/think off") is False
        assert is_incomplete_command("/think once") is False

    def test_think_trailing_text_blocked(self):
        """Extra text after a valid arg is blocked — not forwarded to the LLM."""
        assert is_incomplete_command("/think once blablabla") is True
        assert is_incomplete_command("/think on extra stuff") is True
        assert is_incomplete_command("/think off oops") is True

    def test_invalid_safety_arg(self):
        assert is_incomplete_command("/safety xyz") is True

    def test_valid_safety_arg(self):
        assert is_incomplete_command("/safety confirm") is False
        assert is_incomplete_command("/safety workspace") is False
        assert is_incomplete_command("/safety none") is False

    def test_safety_trailing_text_blocked(self):
        assert is_incomplete_command("/safety confirm extra") is True

    def test_invalid_update_arg(self):
        assert is_incomplete_command("/update xyz") is True

    def test_valid_update_arg(self):
        assert is_incomplete_command("/update ollama") is False
        assert is_incomplete_command("/update models") is False

    def test_update_trailing_text_blocked(self):
        assert is_incomplete_command("/update ollama extra") is True

    def test_known_command_no_arg_always_allowed(self):
        """Commands with no arg should not be blocked even if arg is normally required."""
        assert is_incomplete_command("/think") is False
        assert is_incomplete_command("/safety") is False
        assert is_incomplete_command("/update") is False
        assert is_incomplete_command("/workspace") is False

    def test_not_a_command(self):
        assert is_incomplete_command("hello") is False

    def test_empty(self):
        assert is_incomplete_command("") is False

    def test_partial_quit(self):
        assert is_incomplete_command("/q") is True

    def test_exact_quit(self):
        assert is_incomplete_command("/quit") is False


class TestCommandSendError:
    def test_unknown_command_message(self):
        err = command_send_error("/foo")
        assert "Unknown command" in err
        assert "/foo" in err
        assert "/help" in err

    def test_partial_prefix_suggests_matches(self):
        err = command_send_error("/thi")
        assert "Incomplete" in err or "/think" in err

    def test_invalid_think_arg(self):
        err = command_send_error("/think xyz")
        assert "/think" in err
        assert "xyz" in err

    def test_think_trailing_text_message(self):
        """Valid arg + trailing text produces a specific 'unexpected text' message."""
        err = command_send_error("/think once blablabla")
        assert "once" in err
        assert "Unexpected" in err or "unexpected" in err

    def test_invalid_safety_arg(self):
        err = command_send_error("/safety xyz")
        assert "/safety" in err
        assert "xyz" in err

    def test_invalid_update_arg(self):
        err = command_send_error("/update xyz")
        assert "/update" in err
        assert "xyz" in err

    def test_valid_command_returns_empty(self):
        assert command_send_error("/think on") == ""
        assert command_send_error("/clear") == ""
        assert command_send_error("hello world") == ""

    def test_non_command_returns_empty(self):
        assert command_send_error("hello") == ""
        assert command_send_error("") == ""


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

    def test_trailing_bare_slash_shows_think(self):
        """Embedded bare / should hint /think only, not all commands."""
        hint = hint_for_input("fix this /")
        assert "/think" in hint
        # Other commands must NOT appear
        assert "/clear" not in hint
        assert "/workspace" not in hint

    def test_trailing_think_partial_opt(self):
        """Embedded /think <opt> mid-message should show filtered options."""
        hint = hint_for_input("do this /think o")
        assert "on" in hint
        assert "once" in hint
        assert "/think" in hint

    def test_trailing_other_command_no_hint(self):
        """Commands other than /think embedded mid-message return no hint."""
        assert hint_for_input("do this /workspace") == ""
        assert hint_for_input("do this /clear") == ""

    def test_empty(self):
        assert hint_for_input("") == ""

    def test_exact_command_no_args(self):
        hint = hint_for_input("/clear")
        assert "/clear" in hint
        assert "Clear conversation" in hint


class TestAbsDirMatches:
    def test_trailing_slash_lists_inside(self, tmp_path: Path):
        """When partial ends with '/' the entries *inside* that dir are returned (no trailing slash)."""
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / "file.txt").write_text("x")  # files should be excluded

        matches = abs_dir_matches(str(tmp_path) + "/")
        names = [m.split("/")[-1] for m in matches]
        assert "alpha" in names
        assert "beta" in names
        # Files must not appear
        assert "file.txt" not in names
        # No trailing slashes
        assert not any(m.endswith("/") for m in matches)

    def test_partial_prefix_filters(self, tmp_path: Path):
        """Partial like '/tmp/foo' lists entries in /tmp starting with 'foo'."""
        (tmp_path / "foobar").mkdir()
        (tmp_path / "foobaz").mkdir()
        (tmp_path / "other").mkdir()

        partial = str(tmp_path) + "/foo"
        matches = abs_dir_matches(partial)
        names = [m.rstrip("/").split("/")[-1] for m in matches]
        assert "foobar" in names
        assert "foobaz" in names
        assert "other" not in names

    def test_empty_partial_lists_home_dirs(self):
        """Empty partial lists non-hidden directories in the home folder."""
        matches = abs_dir_matches("")
        assert isinstance(matches, list)
        # All results are "~/name" format (no trailing slash)
        assert all(m.startswith("~/") or m.startswith("/") for m in matches)
        assert not any(m.endswith("/") for m in matches)


class TestDirMatches:
    def test_empty_partial(self, workspace: Path):
        matches = dir_matches(workspace, "")
        assert "src" in matches
        assert "tests_dir" in matches
        # Files should NOT be in dir_matches
        assert "hello.py" not in matches
        # Hidden dirs filtered
        assert ".hidden" not in matches
        # No trailing slashes
        assert not any(m.endswith("/") for m in matches)

    def test_partial_match(self, workspace: Path):
        matches = dir_matches(workspace, "sr")
        assert "src" in matches
        assert "tests_dir" not in matches

    def test_case_insensitive(self, workspace: Path):
        """Lowercase prefix matches mixed-case directory names."""
        (workspace / "Backup").mkdir()
        matches = dir_matches(workspace, "b")
        assert "Backup" in matches

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
        assert "src" in matches
        assert ".hidden" not in matches
        # No trailing slashes on directories
        assert not any(m.endswith("/") for m in matches)

    def test_partial_match(self, workspace: Path):
        matches = path_matches(workspace, "he")
        assert "hello.py" in matches

    def test_dir_no_trailing_slash(self, workspace: Path):
        matches = path_matches(workspace, "sr")
        assert "src" in matches
        assert "src/" not in matches

    def test_case_insensitive(self, workspace: Path):
        """Lowercase prefix matches mixed-case file/folder names."""
        (workspace / "Documents").mkdir()
        (workspace / "Notes.txt").write_text("")
        matches = path_matches(workspace, "d")
        assert "Documents" in matches
        matches2 = path_matches(workspace, "n")
        assert "Notes.txt" in matches2

    def test_no_match(self, workspace: Path):
        matches = path_matches(workspace, "zzz")
        assert matches == []


class TestCompleteLine:
    def test_workspace_dir_completion(self, workspace: Path):
        # /workspace <path> completes absolute filesystem dirs, not workspace-relative ones.
        # With an empty arg it lists home-directory subdirs in "~/name" format (no trailing slash).
        matches = complete_line(workspace, "/workspace ")
        # All results must be full-line replacements starting with "/workspace "
        assert all(m.startswith("/workspace ") for m in matches)
        # No trailing slashes — the user adds "/" themselves if they want to go deeper
        assert not any(m.endswith("/") for m in matches)
        # Workspace-relative file names must NOT appear
        assert not any("hello.py" in m for m in matches)

    def test_think_options(self, workspace: Path):
        matches = complete_line(workspace, "/think ")
        assert "/think on" in matches
        assert "/think off" in matches
        assert "/think once" in matches

    def test_command_prefix(self, workspace: Path):
        matches = complete_line(workspace, "/thi")
        assert "/think" in matches

    def test_update_subcommands(self, workspace: Path):
        matches = complete_line(workspace, "/update ")
        assert "/update ollama" in matches
        assert "/update models" in matches

    def test_update_partial_subcommand(self, workspace: Path):
        matches = complete_line(workspace, "/update o")
        assert matches == ["/update ollama"]

    def test_trailing_space_no_completions(self, workspace: Path):
        """A command with trailing space but no subcommand handler returns nothing."""
        matches = complete_line(workspace, "/pull ")
        assert matches == []

    def test_trailing_space_no_subcommand(self, workspace: Path):
        """A command with no subcommands and a trailing space returns nothing."""
        matches = complete_line(workspace, "/clear ")
        assert matches == []

    def test_info_subcommand_completion(self, workspace: Path):
        """/info <partial> completes to subcommands."""
        assert complete_line(workspace, "/info ") == ["/info prompt", "/info context", "/info tools", "/info benchmark"]
        assert complete_line(workspace, "/info p") == ["/info prompt"]
        assert complete_line(workspace, "/info t") == ["/info tools"]
        assert complete_line(workspace, "/info b") == ["/info benchmark"]
        assert complete_line(workspace, "/info x") == []

    # --- Embedded /think mid-message ---

    def test_embedded_bare_slash(self, workspace: Path):
        """'text /' → /think variants only (not all commands, not file paths)."""
        matches = complete_line(workspace, "fix this /")
        assert "/think once" in matches
        assert "/think on" in matches
        assert "/think off" in matches
        assert not any("hello" in m for m in matches)
        assert not any("/workspace" in m for m in matches)

    def test_embedded_partial_command(self, workspace: Path):
        """'text /t' → /think variants."""
        matches = complete_line(workspace, "do something /t")
        assert "/think once" in matches
        assert "/think on" in matches

    def test_embedded_full_command(self, workspace: Path):
        """'text /think' (no space) → /think variants."""
        matches = complete_line(workspace, "do something /think")
        assert "/think once" in matches

    def test_embedded_think_trailing_space(self, workspace: Path):
        """'text /think ' (trailing space) → all THINK_OPTIONS."""
        matches = complete_line(workspace, "do something /think ")
        assert "/think once" in matches
        assert "/think on" in matches
        assert "/think off" in matches

    def test_embedded_think_partial_opt(self, workspace: Path):
        """'text /think on' → only options starting with 'on' (on + once, not off)."""
        matches = complete_line(workspace, "do something /think on")
        assert "/think on" in matches
        assert "/think once" in matches
        assert "/think off" not in matches

    def test_embedded_other_command_no_complete(self, workspace: Path):
        """'/workspace' and other commands embedded mid-message return nothing."""
        assert complete_line(workspace, "do this /workspace") == []
        assert complete_line(workspace, "do this /clear") == []

    def test_standalone_slash_still_all_commands(self, workspace: Path):
        """Bare '/' at start of line still offers all commands."""
        matches = complete_line(workspace, "/")
        assert "/think" in matches
        assert "/clear" in matches
        assert "/workspace" in matches

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

    def test_pull_and_update_registered(self):
        assert "/pull" in COMMANDS
        assert "/update" in COMMANDS

    def test_think_options(self):
        assert THINK_OPTIONS == ["on", "off", "once"]

    def test_command_hints_coverage(self):
        for cmd in COMMANDS:
            assert cmd in COMMAND_HINTS


class TestConfigToolsHints:
    def test_config_with_space_shows_tools_hint(self):
        hint = hint_for_input("/config ")
        assert "tools" in hint

    def test_config_t_partial(self):
        hint = hint_for_input("/config t")
        assert "tools" in hint

    def test_config_tools_with_space(self):
        hint = hint_for_input("/config tools ")
        assert "tool" in hint.lower()

    def test_config_tools_tool_name(self):
        hint = hint_for_input("/config tools bash ")
        assert "on" in hint
        assert "off" in hint

    def test_config_tools_workspace_arg(self):
        hint = hint_for_input("/config tools bash on ")
        assert "inherit" in hint or "on" in hint


class TestConfigToolsCompletion:
    def test_config_space_suggests_set_and_tools(self, workspace: Path):
        completions = complete_line(workspace, "/config ")
        assert any("set" in c for c in completions)
        assert any("tools" in c for c in completions)

    def test_config_t_suggests_tools(self, workspace: Path):
        completions = complete_line(workspace, "/config t")
        assert any("tools" in c for c in completions)

    def test_config_tools_completes_tool_names(self, workspace: Path):
        completions = complete_line(workspace, "/config tools ")
        from nanoharness.config import TOOL_NAMES
        for name in TOOL_NAMES:
            assert any(name in c for c in completions)

    def test_config_tools_partial_tool_name(self, workspace: Path):
        completions = complete_line(workspace, "/config tools ba")
        assert any("bash" in c for c in completions)
        assert not any("python_exec" in c for c in completions)

    def test_config_tools_global_values(self, workspace: Path):
        completions = complete_line(workspace, "/config tools bash ")
        assert any("on" in c for c in completions)
        assert any("off" in c for c in completions)
        assert any("_" in c for c in completions)

    def test_config_tools_workspace_values(self, workspace: Path):
        completions = complete_line(workspace, "/config tools bash on ")
        assert any("inherit" in c for c in completions)
        assert any(" on" in c for c in completions)
        assert any(" off" in c for c in completions)

    def test_config_set_still_works(self, workspace: Path):
        completions = complete_line(workspace, "/config set ")
        from nanoharness.completion import CONFIG_KEYS
        assert any("model.name" in c for c in completions)

    def test_config_space_also_suggests_theme(self, workspace: Path):
        completions = complete_line(workspace, "/config ")
        assert any("theme" in c for c in completions)


class TestConfigThemeHints:
    def test_config_theme_hint(self):
        hint = hint_for_input("/config theme")
        assert "light" in hint
        assert "dark" in hint
        assert "auto" in hint

    def test_config_theme_with_space(self):
        hint = hint_for_input("/config theme ")
        assert "light" in hint
        assert "dark" in hint
        assert "auto" in hint

    def test_config_t_shows_theme(self):
        hint = hint_for_input("/config t")
        assert "theme" in hint or "tools" in hint  # t prefix matches both


class TestConfigThemeCompletion:
    def test_config_theme_values(self, workspace: Path):
        completions = complete_line(workspace, "/config theme ")
        assert "/config theme light" in completions
        assert "/config theme dark" in completions
        assert "/config theme auto" in completions

    def test_config_theme_partial(self, workspace: Path):
        completions = complete_line(workspace, "/config theme l")
        assert "/config theme light" in completions
        assert "/config theme dark" not in completions

    def test_config_theme_partial_d(self, workspace: Path):
        completions = complete_line(workspace, "/config theme d")
        assert "/config theme dark" in completions
        assert "/config theme light" not in completions

    def test_config_theme_partial_a(self, workspace: Path):
        completions = complete_line(workspace, "/config theme a")
        assert "/config theme auto" in completions
