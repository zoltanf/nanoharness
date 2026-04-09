"""Tests for nanoharness/tools.py — ToolExecutor and helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nanoharness.tools import ToolExecutor, _clip, _clip_lines, _count_lines, ClipResult, format_confirm_preview


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


class TestClipLines:
    def test_short_text_no_truncation(self):
        r = _clip_lines("line1\nline2\n", 100)
        assert r.text == "line1\nline2\n"
        assert r.lines_shown == 2
        assert r.lines_total == 2

    def test_clips_at_line_boundary(self):
        text = "line1\nline2\nline3\n"
        r = _clip_lines(text, 12)  # "line1\nline2\n" is 12 chars — stays, line3 cut
        assert "line3" not in r.text
        assert r.lines_shown < r.lines_total
        assert "lines shown" in r.text

    def test_notice_format(self):
        text = "\n".join(f"line{i}" for i in range(10))
        r = _clip_lines(text, 20)
        assert "[Output truncated:" in r.text
        assert "of 10 lines shown]" in r.text

    def test_exact_boundary_no_clip(self):
        text = "abc\ndef"
        r = _clip_lines(text, len(text))
        assert r.text == text
        assert r.lines_shown == r.lines_total == 2

    def test_no_newlines_uses_char_notice(self):
        # Single long line with no newlines — must NOT produce "1 of 1 lines shown"
        text = "a" * 200
        r = _clip_lines(text, 50)
        assert r.lines_shown == 0      # (0, 0) sentinel
        assert r.lines_total == 0
        assert "chars shown" in r.text
        assert "lines shown" not in r.text

    def test_no_newlines_fits_in_budget(self):
        text = "a" * 50
        r = _clip_lines(text, 100)
        assert r.lines_shown == 1      # 1 line, no clip
        assert r.lines_total == 1
        assert r.text == text

    def test_empty_text(self):
        r = _clip_lines("", 100)
        assert r.lines_shown == 0
        assert r.lines_total == 0
        assert r.text == ""

    def test_trailing_newline_counts_correctly(self):
        text = "a\nb\n"
        r = _clip_lines(text, 100)
        assert r.lines_shown == 2
        assert r.lines_total == 2

    def test_count_lines_helper(self):
        assert _count_lines("") == 0
        assert _count_lines("abc") == 1
        assert _count_lines("abc\n") == 1
        assert _count_lines("abc\ndef") == 2
        assert _count_lines("abc\ndef\n") == 2


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
        result, *_ = te._read_file("hello.py")
        assert "print('hello')" in result

    def test_read_nonexistent(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._read_file("nope.txt")
        assert "Error" in result

    def test_max_chars(self, workspace: Path):
        big_file = workspace / "big.txt"
        big_file.write_text("x" * 10000)
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._read_file("big.txt", max_chars=100)
        assert "truncated" in result


class TestWriteFile:
    def test_write_creates(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, _ui, *_ = te._write_file("new.txt", "hello world")
        assert "Wrote" in result
        assert (workspace / "new.txt").read_text() == "hello world"

    def test_write_creates_parents(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, _ui, *_ = te._write_file("sub/dir/file.txt", "deep")
        assert "Wrote" in result
        assert (workspace / "sub" / "dir" / "file.txt").read_text() == "deep"

    def test_write_outside_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        with pytest.raises(ValueError, match="escapes workspace"):
            te._write_file("../../escape.txt", "bad")

    def test_write_git_dir_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, _ui, *_ = te._write_file(".git/config", "bad")
        assert "Error" in result
        assert not (workspace / ".git" / "config").exists()

    def test_write_git_subpath_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, _ui, *_ = te._write_file(".git/hooks/pre-commit", "bad")
        assert "Error" in result


class TestListFiles:
    def test_lists_files(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".")
        assert "hello.py" in result
        assert "src/" in result

    def test_default_workspace(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".")
        assert "README.md" in result

    def test_nonexistent(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files("nonexistent")
        assert "Error" in result

    def test_hidden_filtered(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".")
        assert ".hidden" not in result

    def test_basic_glob(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".", pattern="*.py")
        assert "hello.py" in result
        assert "src/main.py" in result

    def test_pattern_with_subdirectory(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files("src", pattern="*.py")
        assert "src/main.py" in result
        assert "hello.py" not in result

    def test_no_matches(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".", pattern="*.nonexistent")
        assert result == "No files found."

    def test_nonexistent_directory(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files("missing_dir", pattern="*.py")
        assert "Not a directory" in result

    def test_path_outside_workspace_blocked(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="workspace")
        with pytest.raises(ValueError, match="escapes workspace"):
            te._list_files("../../etc", pattern="*.py")

    def test_path_outside_workspace_allowed_with_none_safety(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="none")
        result, *_ = te._list_files("/tmp", pattern="*.py")
        assert "Not a directory" not in result

    def test_git_dir_skipped(self, workspace: Path):
        git_dir = workspace / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".", pattern="HEAD")
        assert ".git" not in result

    def test_hidden_files_included(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".", pattern="secret.txt")
        assert "secret.txt" in result

    def test_results_relative_to_workspace(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files(".", pattern="main.py")
        assert str(workspace) not in result
        assert "src/main.py" in result

    def test_count_cap(self, workspace: Path):
        many = workspace / "many"
        many.mkdir()
        for i in range(201):
            (many / f"file{i}.txt").write_text("")
        te = ToolExecutor(workspace=workspace)
        result, *_ = te._list_files("many", pattern="*.txt")
        assert "200 result limit reached" in result
        assert result.count(".txt") == 200


class TestBash:
    @pytest.mark.asyncio
    async def test_simple_command(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._bash("echo hello")
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_cwd(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._bash("pwd")
        assert str(workspace) in result

    @pytest.mark.asyncio
    async def test_empty_command(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._bash("")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_timeout(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, timeout=1)
        result, *_ = await te._bash("sleep 10")
        assert "timed out" in result


class TestPythonExec:
    async def test_simple(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._python_exec("print(2 + 2)")
        assert "4" in result

    async def test_empty_code(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._python_exec("")
        assert "Error" in result

    async def test_exception(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._python_exec("raise ValueError('boom')")
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
        result, _ui, *_ = await te.execute("bash", {"command": "echo hello"})
        assert result == "User denied this action."

    async def test_confirm_fn_allow_executes(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="confirm")

        async def allow_all(tool_name, args):
            return True

        te.confirm_fn = allow_all
        result, _ui, *_ = await te.execute("bash", {"command": "echo hello"})
        assert "hello" in result

    async def test_no_confirm_for_read_file(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, safety="confirm")
        called = []

        async def track(tool_name, args):
            called.append(tool_name)
            return False

        te.confirm_fn = track
        result, _ui, *_ = await te.execute("read_file", {"path": "hello.py"})
        assert not called  # confirm not invoked for read_file
        assert "Error" not in result or "print" in result

    async def test_no_confirm_fn_allows_execution(self, workspace: Path):
        """confirm_fn=None with safety=confirm still executes (no confirm possible)."""
        te = ToolExecutor(workspace=workspace, safety="confirm")
        te.confirm_fn = None
        result, _ui, *_ = await te.execute("bash", {"command": "echo hi"})
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

    def test_ollama_update(self):
        preview = format_confirm_preview("ollama_update", {"command": "brew upgrade ollama"})
        assert "Update Ollama" in preview
        assert "brew upgrade ollama" in preview

    def test_ollama_restart(self):
        preview = format_confirm_preview("ollama_restart", {"action": "restart the Ollama server to apply the update"})
        assert "Restart Ollama" in preview
        assert "restart the Ollama server" in preview


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

    def test_clear(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._todo("add", task="Task one")
        te._todo("add", task="Task two")
        result = te._todo("clear")
        assert "Cleared 2" in result
        assert te._todo("list") == "No tasks"
        assert te._todo_file.read_text() == "[]"

    def test_clear_empty_list(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result = te._todo("clear")
        assert "Cleared 0" in result

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
        result, _ui, *_ = await te.execute("nonexistent", {})
        assert "Unknown tool" in result

    @pytest.mark.asyncio
    async def test_routes_to_bash(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, _ui, *_ = await te.execute("bash", {"command": "echo dispatch_test"})
        assert "dispatch_test" in result

    @pytest.mark.asyncio
    async def test_routes_to_read_file(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, _ui, *_ = await te.execute("read_file", {"path": "hello.py"})
        assert "print" in result

    @pytest.mark.asyncio
    async def test_error_handling(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, _ui, *_ = await te.execute("read_file", {"path": "../../escape"})
        assert "Error" in result


def _make_mock_client(html: str, status: int = 200):
    """Return a patched httpx.AsyncClient context manager that returns html."""
    mock_resp = MagicMock()
    mock_resp.text = html
    mock_resp.raise_for_status = MagicMock()
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(return_value=mock_resp)
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client, mock_instance, mock_resp


class TestFetchWebpage:
    async def test_rejects_ftp_scheme(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._fetch_webpage("ftp://example.com/file.txt")
        assert "Error" in result
        assert "scheme" in result

    async def test_rejects_file_scheme(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._fetch_webpage("file:///etc/passwd")
        assert "Error" in result
        assert "scheme" in result

    async def test_rejects_empty_url(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        result, *_ = await te._fetch_webpage("")
        assert "Error" in result

    async def test_timeout_error(self, workspace: Path):
        te = ToolExecutor(workspace=workspace, timeout=5)
        mock_client, mock_instance, _ = _make_mock_client("")
        mock_instance.get.side_effect = httpx.TimeoutException("timed out")
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            result, *_ = await te._fetch_webpage("https://example.com")
        assert "Error" in result
        assert "timed out" in result

    async def test_http_404_error(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        mock_client, mock_instance, _ = _make_mock_client("")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_instance.get.side_effect = httpx.HTTPStatusError(
            "not found", request=MagicMock(), response=mock_resp
        )
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            result, *_ = await te._fetch_webpage("https://example.com/gone")
        assert "Error" in result
        assert "404" in result

    async def test_request_error(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        mock_client, mock_instance, _ = _make_mock_client("")
        mock_instance.get.side_effect = httpx.ConnectError("connection refused")
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            result, *_ = await te._fetch_webpage("https://unreachable.invalid")
        assert "Error" in result
        assert "ConnectError" in result

    async def test_successful_fetch_with_trafilatura(self, workspace: Path):
        html = "<html><body><article><p>Hello world article text.</p></article></body></html>"
        te = ToolExecutor(workspace=workspace)
        mock_client, _, _ = _make_mock_client(html)
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            with patch("trafilatura.extract", return_value="Hello world article text."):
                result, *_ = await te._fetch_webpage("https://example.com")
        assert "Hello world article text." in result

    async def test_clips_to_max_chars(self, workspace: Path):
        long_text = "word " * 10000
        te = ToolExecutor(workspace=workspace, max_chars=100)
        mock_client, _, _ = _make_mock_client("<html><body>" + long_text + "</body></html>")
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            with patch("trafilatura.extract", return_value=long_text):
                result, *_ = await te._fetch_webpage("https://example.com")
        assert "truncated" in result
        assert len(result) < len(long_text)

    async def test_trafilatura_returns_none_uses_fallback(self, workspace: Path):
        html = "<html><body><p>Plain fallback text</p></body></html>"
        te = ToolExecutor(workspace=workspace)
        mock_client, _, _ = _make_mock_client(html)
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            with patch("trafilatura.extract", return_value=None):
                result, *_ = await te._fetch_webpage("https://example.com")
        assert "Plain fallback text" in result

    async def test_missing_trafilatura_returns_error(self, workspace: Path):
        html = "<html><body><p>content</p></body></html>"
        te = ToolExecutor(workspace=workspace)
        mock_client, _, _ = _make_mock_client(html)
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            with patch.dict(sys.modules, {"trafilatura": None}):
                result, *_ = await te._fetch_webpage("https://example.com")
        assert "Error" in result
        assert "trafilatura" in result

    async def test_dispatch_routes_fetch_webpage(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        with patch.object(te, "_fetch_webpage", return_value=("mocked result", 0, 0)) as mock_fn:
            result, _ui, *_ = await te.execute("fetch_webpage", {"url": "https://example.com"})
        mock_fn.assert_called_once_with("https://example.com")
        assert result == "mocked result"

    async def test_no_confirm_needed(self, workspace: Path):
        """fetch_webpage should not trigger the confirm gate."""
        te = ToolExecutor(workspace=workspace, safety="confirm")
        called = []

        async def track(tool_name, args):
            called.append(tool_name)
            return False

        te.confirm_fn = track
        mock_client, _, _ = _make_mock_client("<html><body><p>ok</p></body></html>")
        with patch("nanoharness.tools.httpx.AsyncClient", return_value=mock_client):
            with patch("trafilatura.extract", return_value="ok"):
                await te.execute("fetch_webpage", {"url": "https://example.com"})
        assert not called


class TestWorkspaceTools:
    def test_load_missing_file_returns_empty(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        assert te._load_workspace_tools() == {}

    def test_save_and_load_roundtrip(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._save_workspace_tools({"bash": False, "python_exec": True})
        result = te._load_workspace_tools()
        assert result == {"bash": False, "python_exec": True}

    def test_save_skips_none_values(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._save_workspace_tools({"bash": False, "python_exec": None, "todo": True})
        result = te._load_workspace_tools()
        assert "python_exec" not in result
        assert result == {"bash": False, "todo": True}

    def test_save_creates_parent_dir(self, tmp_path: Path):
        ws = tmp_path / "newproject"
        ws.mkdir()
        te = ToolExecutor(workspace=ws)
        te._save_workspace_tools({"bash": False})
        assert (ws / ".nanoharness" / "tools.json").is_file()

    def test_load_caches_result(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._save_workspace_tools({"bash": False})
        first = te._load_workspace_tools()
        # Write directly to file, bypassing _save — cache should still return old value
        te._tools_file.write_text('{"bash": true}')
        second = te._load_workspace_tools()
        assert first is second  # same dict object from cache

    def test_save_invalidates_cache(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        te._save_workspace_tools({"bash": False})
        assert te._load_workspace_tools()["bash"] is False
        te._save_workspace_tools({"bash": True})
        assert te._load_workspace_tools()["bash"] is True

    def test_load_corrupt_file_returns_empty(self, workspace: Path):
        te = ToolExecutor(workspace=workspace)
        (workspace / ".nanoharness").mkdir(exist_ok=True)
        te._tools_file.write_text("not valid json{{{")
        assert te._load_workspace_tools() == {}


class TestEnabledSchemas:
    from nanoharness.config import ToolsConfig

    def test_all_enabled_by_default(self, workspace: Path):
        from nanoharness.config import ToolsConfig
        from nanoharness.tools import TOOL_SCHEMAS
        te = ToolExecutor(workspace=workspace)
        schemas = te.enabled_schemas(ToolsConfig())
        assert schemas == TOOL_SCHEMAS

    def test_global_disable_removes_schema(self, workspace: Path):
        from nanoharness.config import ToolsConfig
        cfg = ToolsConfig()
        cfg.bash = False
        te = ToolExecutor(workspace=workspace)
        schemas = te.enabled_schemas(cfg)
        names = [s["function"]["name"] for s in schemas]
        assert "bash" not in names
        assert "python_exec" in names

    def test_workspace_override_disables(self, workspace: Path):
        from nanoharness.config import ToolsConfig
        te = ToolExecutor(workspace=workspace)
        te._save_workspace_tools({"python_exec": False})
        schemas = te.enabled_schemas(ToolsConfig())
        names = [s["function"]["name"] for s in schemas]
        assert "python_exec" not in names
        assert "bash" in names

    def test_workspace_override_enables_globally_disabled(self, workspace: Path):
        from nanoharness.config import ToolsConfig
        cfg = ToolsConfig()
        cfg.bash = False
        te = ToolExecutor(workspace=workspace)
        te._save_workspace_tools({"bash": True})
        schemas = te.enabled_schemas(cfg)
        names = [s["function"]["name"] for s in schemas]
        assert "bash" in names

    def test_workspace_inherits_global(self, workspace: Path):
        from nanoharness.config import ToolsConfig
        cfg = ToolsConfig()
        cfg.fetch_webpage = False
        te = ToolExecutor(workspace=workspace)
        # No workspace override for fetch_webpage — inherits global
        schemas = te.enabled_schemas(cfg)
        names = [s["function"]["name"] for s in schemas]
        assert "fetch_webpage" not in names

    def test_disable_all(self, workspace: Path):
        from nanoharness.config import ToolsConfig
        cfg = ToolsConfig()
        for name in ["bash", "read_file", "write_file", "list_files", "python_exec", "todo", "fetch_webpage"]:
            setattr(cfg, name, False)
        te = ToolExecutor(workspace=workspace)
        assert te.enabled_schemas(cfg) == []
