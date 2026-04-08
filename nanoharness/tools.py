"""Tool definitions and execution for the agent."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

# Ultra-terse tool schemas for minimal token usage
TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run shell command",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read file contents. Use offset to start reading from a byte position (e.g. to continue after truncation).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "Byte offset to start reading from (default: 0)"},
            "max_chars": {"type": "integer"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write content to file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List directory contents",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Find files matching a glob pattern recursively within the workspace. Skips .git. Returns paths relative to workspace root.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.py', '**/*.toml', '*config*'"},
            "path": {"type": "string", "description": "Directory to search in (default: workspace root)"},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "python_exec",
        "description": "Execute Python code, return stdout",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"},
        }, "required": ["code"]},
    }},
    {"type": "function", "function": {
        "name": "todo",
        "description": "Task list: add/complete/remove/list tasks",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "complete", "remove", "list", "clear"]},
            "task": {"type": "string"},
            "id": {"type": "integer"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "fetch_webpage",
        "description": "Fetch a URL and return its main text content",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "https:// or http:// URL"},
        }, "required": ["url"]},
    }},
]


def format_confirm_preview(tool_name: str, args: dict) -> str:
    """Return a short human-readable preview of a tool call for confirmation prompts."""
    if tool_name == "bash":
        cmd = args.get("command", "")
        preview = cmd[:200] + ("..." if len(cmd) > 200 else "")
        return f"bash\n  $ {preview}"
    if tool_name == "python_exec":
        lines = args.get("code", "").splitlines()
        shown = lines[:10]
        rest = len(lines) - 10
        code_preview = "\n  ".join(shown)
        suffix = f"\n  ... ({rest} more lines)" if rest > 0 else ""
        return f"python_exec\n  {code_preview}{suffix}"
    if tool_name == "write_file":
        path = args.get("path", "")
        size = len(args.get("content", ""))
        return f"write_file\n  path: {path}  size: {size} bytes"
    if tool_name == "ollama_update":
        cmd = args.get("command", "")
        return f"Update Ollama\n  $ {cmd}"
    if tool_name == "ollama_restart":
        action = args.get("action", "restart the Ollama server")
        return f"Restart Ollama\n  {action}"
    return tool_name


_SENSITIVE_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GITHUB_")
_SENSITIVE_SUBSTRINGS = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_CREDENTIAL", "_DSN")
_SENSITIVE_EXACT = frozenset({
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "DATABASE_URL", "PGPASSWORD",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
})

_CONFIRM_TOOLS = frozenset({"bash", "python_exec", "write_file"})


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


class ToolExecutor:
    def __init__(self, workspace: Path, safety: str = "workspace", timeout: int = 30, max_chars: int = 8000):
        self.workspace = workspace.resolve()
        self.safety = safety
        self.timeout = timeout
        self.max_chars = max_chars
        self._todo_file = workspace / ".nanoharness" / "todo.json"
        self.confirm_fn: Callable[[str, dict], Awaitable[bool]] | None = None

    def _safe_path(self, path: str) -> Path:
        """Resolve path and ensure it stays within workspace (if safety != none)."""
        p = Path(path)
        if not p.is_absolute():
            p = self.workspace / p
        p = p.resolve()
        if self.safety != "none":
            if p != self.workspace and self.workspace not in p.parents:
                raise ValueError(f"Path escapes workspace: {path}")
        return p

    def _scrubbed_env(self) -> dict[str, str]:
        """Return a copy of os.environ with sensitive vars stripped and HOME overridden."""
        env = {}
        for k, v in os.environ.items():
            ku = k.upper()
            if ku in _SENSITIVE_EXACT:
                continue
            if any(ku.startswith(p) for p in _SENSITIVE_PREFIXES):
                continue
            if any(s in ku for s in _SENSITIVE_SUBSTRINGS):
                continue
            env[k] = v
        env["HOME"] = str(self.workspace)
        return env

    async def execute(self, name: str, arguments: dict[str, Any], *, confirm: bool = True) -> str:
        """Execute a tool by name and return the result as a string."""
        try:
            if confirm and self.safety == "confirm" and name in _CONFIRM_TOOLS and self.confirm_fn:
                allowed = await self.confirm_fn(name, arguments)
                if not allowed:
                    return "User denied this action."
            match name:
                case "bash":
                    return await self._bash(arguments.get("command", ""))
                case "read_file":
                    return self._read_file(
                        arguments.get("path", ""),
                        arguments.get("max_chars", 0),
                        arguments.get("offset", 0),
                    )
                case "write_file":
                    return self._write_file(
                        arguments.get("path", ""),
                        arguments.get("content", ""),
                    )
                case "list_dir":
                    return self._list_dir(arguments.get("path", "."))
                case "search_files":
                    return self._search_files(
                        arguments.get("pattern", ""),
                        arguments.get("path", "."),
                    )
                case "python_exec":
                    return await self._python_exec(arguments.get("code", ""))
                case "todo":
                    return self._todo(
                        arguments.get("action", "list"),
                        arguments.get("task"),
                        arguments.get("id"),
                    )
                case "fetch_webpage":
                    return await self._fetch_webpage(arguments.get("url", ""))
                case _:
                    return f"Unknown tool: {name}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    async def _bash(self, command: str) -> str:
        if not command.strip():
            return "Error: empty command"
        env = self._scrubbed_env() if self.safety != "none" else None
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace),
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: command timed out after {self.timeout}s"
        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        err = stderr.decode("utf-8", errors="replace") if stderr else ""
        result = ""
        if out:
            result += _clip(out, self.max_chars)
        if err:
            result += f"\nstderr: {_clip(err, self.max_chars)}"
        if proc.returncode != 0:
            result += f"\nexit code: {proc.returncode}"
        return result.strip() or "(no output)"

    def _read_file(self, path: str, max_chars: int = 0, offset: int = 0) -> str:
        p = self._safe_path(path)
        limit = min(max_chars, self.max_chars) if max_chars else self.max_chars
        try:
            size = p.stat().st_size
            with open(p, encoding="utf-8", errors="replace") as f:
                if offset:
                    f.seek(offset)
                text = f.read(limit + 1)
        except (FileNotFoundError, IsADirectoryError, OSError) as e:
            return f"Error: {e}"
        prefix = f"[offset {offset}] " if offset else ""
        if len(text) > limit:
            end = offset + limit
            return prefix + text[:limit] + f"\n...[truncated, showing bytes {offset}-{end} of {size}]"
        return prefix + text

    def _write_file(self, path: str, content: str) -> str:
        p = self._safe_path(path)
        if any(part.lower() == ".git" for part in p.relative_to(self.workspace).parts):
            return "Error: write_file cannot modify .git directory"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {p.relative_to(self.workspace)}"

    def _list_dir(self, path: str = ".") -> str:
        p = self._safe_path(path)
        try:
            items = list(p.iterdir())
        except (FileNotFoundError, NotADirectoryError, OSError) as e:
            return f"Error: {e}"
        entries = sorted(
            f"{x.name}/" if x.is_dir() else x.name
            for x in items
            if not x.name.startswith(".")
        )
        if not entries:
            return "(empty directory)"
        return "\n".join(entries)

    def _search_files(self, pattern: str, path: str = ".") -> str:
        if not pattern:
            return "Error: pattern is required"
        base = self._safe_path(path)
        if not base.is_dir():
            return f"Not a directory: {path}"
        MAX_RESULTS = 200
        results = []
        for match in base.rglob(pattern):
            if ".git" in match.parts:
                continue
            try:
                rel = match.relative_to(self.workspace)
            except ValueError:
                rel = match
            results.append(str(rel))
            if len(results) >= MAX_RESULTS:
                break
        if not results:
            return "No files found."
        output = "\n".join(results)
        if len(results) >= MAX_RESULTS:
            output += "\n...[200 result limit reached — narrow your pattern or specify a subdirectory]"
        return _clip(output, self.max_chars)

    async def _python_exec(self, code: str) -> str:
        if not code.strip():
            return "Error: empty code"
        env = self._scrubbed_env() if self.safety != "none" else None
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return f"Error: python_exec timed out after {self.timeout}s"
        finally:
            os.unlink(tmp_path)
        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        err = stderr.decode("utf-8", errors="replace") if stderr else ""
        result = ""
        if out:
            result += _clip(out, self.max_chars)
        if err:
            result += f"\nstderr: {_clip(err, self.max_chars)}"
        if proc.returncode != 0:
            result += f"\nexit code: {proc.returncode}"
        return result.strip() or "(no output)"

    def _load_todo(self) -> list[dict]:
        if not self._todo_file.is_file():
            return []
        try:
            return json.loads(self._todo_file.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _save_todo(self, tasks: list[dict]) -> None:
        self._todo_file.parent.mkdir(parents=True, exist_ok=True)
        self._todo_file.write_text(json.dumps(tasks, indent=2))

    def _todo(self, action: str, task: str | None = None, task_id: int | None = None) -> str:
        tasks = self._load_todo()

        match action:
            case "add":
                if not task:
                    return "Error: 'task' required for add"
                new_id = max((t["id"] for t in tasks), default=0) + 1
                tasks.append({"id": new_id, "task": task, "done": False})
                self._save_todo(tasks)
                return f"Added task #{new_id}: {task}"

            case "complete":
                if task_id is None:
                    return "Error: 'id' required for complete"
                for t in tasks:
                    if t["id"] == task_id:
                        t["done"] = True
                        self._save_todo(tasks)
                        return f"Completed task #{task_id}: {t['task']}"
                return f"Error: task #{task_id} not found"

            case "remove":
                if task_id is None:
                    return "Error: 'id' required for remove"
                before = len(tasks)
                tasks = [t for t in tasks if t["id"] != task_id]
                if len(tasks) == before:
                    return f"Error: task #{task_id} not found"
                self._save_todo(tasks)
                return f"Removed task #{task_id}"

            case "list":
                if not tasks:
                    return "No tasks"
                lines = []
                for t in tasks:
                    status = "done" if t["done"] else "pending"
                    lines.append(f"#{t['id']} [{status}] {t['task']}")
                return "\n".join(lines)

            case "clear":
                count = len(tasks)
                self._save_todo([])
                return f"Cleared {count} task{'s' if count != 1 else ''}"

            case _:
                return f"Error: unknown action: {action}"

    def _get_task_stats(self) -> tuple[int, int, list[str]]:
        """Return (done_count, total_count, pending_task_names)."""
        tasks = self._load_todo()
        done = sum(1 for t in tasks if t["done"])
        pending = [t["task"] for t in tasks if not t["done"]]
        return done, len(tasks), pending

    def get_todo_summary(self) -> str | None:
        """Get a brief todo summary for display in UI. Returns None if no tasks."""
        done, total, pending = self._get_task_stats()
        if not total:
            return None
        summary = f"Tasks: {done}/{total} done"
        if pending:
            summary += f" | Next: {pending[0]}"
        return summary

    def get_todo_parts(self) -> tuple[str | None, str | None]:
        """Return (next_task, progress) for status bar display. Both None if no tasks."""
        done, total, pending = self._get_task_stats()
        if not total:
            return None, None
        return pending[0] if pending else None, f"Tasks: {done}/{total} done"

    async def _fetch_webpage(self, url: str) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Error: unsupported scheme '{parsed.scheme}'. Only http/https are allowed."
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; NanoHarness/1.0)"},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
        except httpx.TimeoutException:
            return f"Error: request timed out after {self.timeout}s"
        except httpx.HTTPStatusError as e:
            return f"Error: HTTP {e.response.status_code} for {url}"
        except httpx.RequestError as e:
            return f"Error: {type(e).__name__}: {e}"
        try:
            import trafilatura
            text = trafilatura.extract(html, include_links=False, include_images=False)
            if not text:
                from html.parser import HTMLParser

                class _S(HTMLParser):
                    def __init__(self): super().__init__(); self._p: list[str] = []
                    def handle_data(self, d): self._p.append(d)
                    def get_text(self): return " ".join(self._p)

                s = _S()
                s.feed(html)
                text = s.get_text()
        except ImportError:
            return "Error: 'trafilatura' not installed. Run: uv add trafilatura"
        return _clip(text.strip() or "(no content extracted)", self.max_chars)
