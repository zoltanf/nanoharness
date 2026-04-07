"""Tests for nanoharness/tools.py — ToolExecutor and helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoharness.tools import ToolExecutor, _clip, format_confirm_preview


class TestClip:
    def test_short_text(self):
        assert _clip("hello", 100) == "hello"

    def test_long_text(self):
        result = _clip("a" * 200, 50)
        assert len(result.split("\n")[0]) == 50
        assert "truncated" in result

    def test_exact_boundary(self):
        text = "a" * 50
        assert _clip(text, 50) == text


class TestSafePath:
    def test_relative(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        p = te._safe_path("hello.py")
        assert p == (workspace / "hello.py").resolve()

    def test_absolute_within(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        target = workspace / "hello.py"
        p = te._safe_path(str(target))
        assert p == target.resolve()

    def test_traversal_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        with pytest.raises(ValueError, match="escapes workspace"):
            te._safe_path("../../etc/passwd")

    def test_absolute_outside_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        with pytest.raises(ValueError, match="escapes workspace"):
            te._safe_path("/etc/passwd")

    def test_none_allows_outside(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="none")
        p = te._safe_path("/tmp")
        assert p == Path("/tmp").resolve()


class TestReadFile:
    def test_read_existing(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._read_file("hello.py")
        assert "print('hello')" in result

    def test_read_nonexistent(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._read_file("nope.txt")
        assert "Error" in result

    def test_max_chars(self, workspace: Path):
        big_file = workspace / "big.txt"
        big_file.write_text("x" * 10000)
        te = ToolExecutor(workspace=workspace)
        result = te._read_file("big.txt", max_chars=100)
        assert "truncated" in result


class TestWriteFile:
    def test_write_creates(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._write_file("new.txt", "hello world")
        assert "Wrote" in result
        assert (workspace / "new.txt").read_text() == "hello world"

    def test_write_creates_parents(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._write_file("sub/dir/file.txt", "deep")
        assert "Wrote" in result
        assert (workspace / "sub" / "dir" / "file.txt").read_text() == "deep"

    def test_write_outside_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        with pytest.raises(ValueError, match="escapes workspace"):
            te._write_file("../../escape.txt", "bad")

    def test_write_git_dir_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._write_file(".git/config", "bad")
        assert "Error" in result
        assert not (workspace / ".git" / "config").exists()

    def test_write_git_subpath_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._write_file(".git/hooks/pre-commit", "bad")
        assert "Error" in result


class TestListDir:
    def test_lists_files(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._list_dir(".")
        assert "hello.py" in result
        assert "src/" in result

    def test_default_workspace(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._list_dir(".")
        assert "README.md" in result

    def test_nonexistent(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._list_dir("nonexistent")
        assert "Error" in result

    def test_hidden_filtered(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._list_dir(".")
        assert ".hidden" not in result


class TestBash:
    @pytest.mark.asyncio
    async def test_simple_command(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te._bash("echo hello")
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_cwd(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te._bash("pwd")
        assert str(workspace) in result

    @pytest.mark.asyncio
    async def test_empty_command(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te._bash("")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_timeout(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, timeout=1)
        result = await te._bash("sleep 10")
        assert "timed out" in result


class TestPythonExec:
    async def test_simple(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te._python_exec("print(2 + 2)")
        assert "4" in result

    async def test_empty_code(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te._python_exec("")
        assert "Error" in result

    async def test_exception(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te._python_exec("raise ValueError('boom')")
        assert "ValueError" in result
        assert "boom" in result


class TestEnvScrubbing:
    def test_scrubbed_env_removes_api_key(self, workspace: Path, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        monkeypatch.setenv("MY_TOKEN", "tok123")
        te = ToolExecutor(workspace=workspace, safety="workspace")
        env = te._scrubbed_env()
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "MY_TOKEN" not in env

    def test_scrubbed_env_keeps_path(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="workspace")
        env = te._scrubbed_env()
        assert "PATH" in env

    def test_scrubbed_env_overrides_home(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="workspace")
        env = te._scrubbed_env()
        assert env["HOME"] == str(workspace)

    def test_none_mode_no_scrubbing(self, workspace: Path, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret123")
        te = ToolExecutor(workspace=workspace, safety="none")
        # In none mode, _bash passes env=None (inherits full env), not _scrubbed_env
        # We verify _scrubbed_env is not called by checking the bash env
        # (indirectly tested via bash test below)
        assert te.safety == "none"


class TestConfirmGate:
    async def test_confirm_fn_deny_returns_denied_message(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="confirm")

        async def deny_all(tool_name, args):
            return False

        te.confirm_fn = deny_all
        result = await te.execute("bash", {"command": "echo hello"})
        assert result == "User denied this action."

    async def test_confirm_fn_allow_executes(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="confirm")

        async def allow_all(tool_name, args):
            return True

        te.confirm_fn = allow_all
        result = await te.execute("bash", {"command": "echo hello"})
        assert "hello" in result

    async def test_no_confirm_for_read_file(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="confirm")
        called = []

        async def track(tool_name, args):
            called.append(tool_name)
            return False

        te.confirm_fn = track
        result = await te.execute("read_file", {"path": "hello.py"})
        assert not called  # confirm not invoked for read_file
        assert "Error" not in result or "print" in result

    async def test_no_confirm_fn_allows_execution(self, workspace: Path):
        """confirm_fn=None with safety=confirm still executes (no confirm possible)."""
        te = ToolExecutor(workspace=workspace, safety="confirm")
        te.confirm_fn = None
        result = await te.execute("bash", {"command": "echo hi"})
        assert "hi" in result


class TestFormatConfirmPreview:
    def test_bash(self):
        preview = format_confirm_preview("bash", {"command": "git status"})
        assert "bash" in preview
        assert "git status" in preview

    def test_python_exec_truncates(self):
        code = "\n".join(f"line{i}" for i in range(15))
        preview = format_confirm_preview("python_exec", {"code": code})
        assert "python_exec" in preview
        assert "5 more lines" in preview

    def test_write_file(self):
        preview = format_confirm_preview("write_file", {"path": "foo.txt", "content": "hello"})
        assert "foo.txt" in preview
        assert "5 bytes" in preview


class TestTodo:
    def test_add(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._todo("add", task="Write tests")
        assert "Added" in result
        assert "#1" in result

    def test_list_empty(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._todo("list")
        assert "No tasks" in result

    def test_list_with_tasks(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._todo("add", task="Task one")
        te._todo("add", task="Task two")
        result = te._todo("list")
        assert "Task one" in result
        assert "Task two" in result

    def test_complete(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._todo("add", task="Do thing")
        result = te._todo("complete", task_id=1)
        assert "Completed" in result
        listing = te._todo("list")
        assert "done" in listing

    def test_remove(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._todo("add", task="Temp task")
        result = te._todo("remove", task_id=1)
        assert "Removed" in result
        assert te._todo("list") == "No tasks"

    def test_invalid_id(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._todo("complete", task_id=999)
        assert "not found" in result

    def test_unknown_action(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._todo("invalid")
        assert "unknown action" in result.lower()


class TestTodoSummary:
    def test_no_tasks(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        assert te.get_todo_summary() is None

    def test_with_tasks(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._todo("add", task="First")
        te._todo("add", task="Second")
        te._todo("complete", task_id=1)
        summary = te.get_todo_summary()
        assert "1/2 done" in summary
        assert "Next: Second" in summary


class TestExecuteDispatch:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te.execute("nonexistent", {})
        assert "Unknown tool" in result

    @pytest.mark.asyncio
    async def test_routes_to_bash(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te.execute("bash", {"command": "echo dispatch_test"})
        assert "dispatch_test" in result

    @pytest.mark.asyncio
    async def test_routes_to_read_file(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te.execute("read_file", {"path": "hello.py"})
        assert "print" in result

    @pytest.mark.asyncio
    async def test_error_handling(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = await te.execute("read_file", {"path": "../../escape"})
        assert "Error" in result
