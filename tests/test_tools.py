"""Tests for nanoharness/tools.py — ToolExecutor and helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoharness.tools import ToolExecutor, _clip


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

    def test_unrestricted_allows_outside(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="unrestricted")
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
    def test_simple(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._python_exec("print(2 + 2)")
        assert "4" in result

    def test_empty_code(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._python_exec("")
        assert "Error" in result

    def test_exception(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._python_exec("raise ValueError('boom')")
        assert "ValueError" in result
        assert "boom" in result


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
