"""Tool definitions and execution for the agent."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from typing import Any

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
            "action": {"type": "string", "enum": ["add", "complete", "remove", "list"]},
            "task": {"type": "string"},
            "id": {"type": "integer"},
        }, "required": ["action"]},
    }},
]


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

    def _safe_path(self, path: str) -> Path:
        """Resolve path and ensure it stays within workspace (if safety != unrestricted)."""
        p = Path(path)
        if not p.is_absolute():
            p = self.workspace / p
        p = p.resolve()
        if self.safety != "unrestricted":
            if p != self.workspace and self.workspace not in p.parents:
                raise ValueError(f"Path escapes workspace: {path}")
        return p

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool by name and return the result as a string."""
        try:
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
                case "python_exec":
                    return self._python_exec(arguments.get("code", ""))
                case "todo":
                    return self._todo(
                        arguments.get("action", "list"),
                        arguments.get("task"),
                        arguments.get("id"),
                    )
                case _:
                    return f"Unknown tool: {name}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    async def _bash(self, command: str) -> str:
        if not command.strip():
            return "Error: empty command"
        env: dict[str, str] | None = None
        if self.safety == "workspace":
            import os
            env = {**os.environ, "HOME": str(self.workspace)}
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

    def _python_exec(self, code: str) -> str:
        if not code.strip():
            return "Error: empty code"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        env: dict[str, Any] = {"__name__": "__nano_exec__"}
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(code, env)
        except Exception as e:
            stderr_buf.write(f"\n{type(e).__name__}: {e}")
        out = stdout_buf.getvalue()
        err = stderr_buf.getvalue()
        result = ""
        if out:
            result += _clip(out, self.max_chars)
        if err:
            result += f"\nstderr: {_clip(err, self.max_chars)}"
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

            case _:
                return f"Error: unknown action: {action}"

    def get_todo_summary(self) -> str | None:
        """Get a brief todo summary for display in UI. Returns None if no tasks."""
        tasks = self._load_todo()
        if not tasks:
            return None
        done = sum(1 for t in tasks if t["done"])
        total = len(tasks)
        pending = [t["task"] for t in tasks if not t["done"]]
        summary = f"Tasks: {done}/{total} done"
        if pending:
            summary += f" | Next: {pending[0]}"
        return summary

    def get_todo_parts(self) -> tuple[str | None, str | None]:
        """Return (next_task, progress) for status bar display. Both None if no tasks."""
        tasks = self._load_todo()
        if not tasks:
            return None, None
        done = sum(1 for t in tasks if t["done"])
        total = len(tasks)
        pending = [t["task"] for t in tasks if not t["done"]]
        progress = f"Tasks: {done}/{total} done"
        next_task = pending[0] if pending else None
        return next_task, progress
