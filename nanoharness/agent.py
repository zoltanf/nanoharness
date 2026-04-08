"""Core agent loop: conversation management, tool execution, history truncation."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Any

import platform
import shutil

import httpx

from .config import Config
from .ollama import OllamaClient
from .tools import TOOL_SCHEMAS, ToolExecutor
from .commands import CommandHandler
from . import logging as log

# Fixed overhead: tool schemas are sent on every LLM call but not included in
# _build_messages. Pre-compute once so the token estimate can account for them.
_TOOL_SCHEMAS_CHARS: int = len(json.dumps(TOOL_SCHEMAS))


SYSTEM_PROMPT = (
    "You are a coding agent. Use tools to complete tasks. Be direct and concise.\n"
    "Working directory: {workspace}\n"
    "Safety: {safety} — "
    "confirm: workspace-contained + user must approve bash/python/write; "
    "workspace: workspace-contained, env scrubbed; "
    "none: no restrictions."
)

FALLBACK_SYSTEM_PROMPT = (
    "You are a coding agent. Accomplish the task by writing bash commands or code.\n"
    "When you need to run a command, write it in a ```bash block.\n"
    "When you need to create a file, use cat <<'EOF' > filename or echo.\n"
    "Working directory: {workspace}"
)


def _parse_code_blocks(text: str) -> list[tuple[str, str]]:
    """Extract fenced code blocks from text. Returns [(lang, code), ...]."""
    pattern = r"```(\w*)\s*\n(.*?)```"
    blocks = re.findall(pattern, text, re.DOTALL)
    return [(lang.lower() or "bash", code.strip()) for lang, code in blocks if code.strip()]


@dataclass
class StreamEvent:
    """Events emitted by the agent during processing."""
    type: str  # "content" | "thinking" | "tool_call" | "tool_result" | "done" | "error" | "status" | "progress"
    text: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    tool_id: str = ""

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "text": self.text}
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.tool_args:
            d["tool_args"] = self.tool_args
        if self.tool_id:
            d["tool_id"] = self.tool_id
        return d


class Agent:
    def __init__(self, config: Config, client: OllamaClient):
        self.config = config
        self.client = client
        self.tools = ToolExecutor(
            workspace=config.workspace,
            safety=config.safety.level,
            timeout=config.agent.timeout_seconds,
            max_chars=config.agent.max_output_chars,
        )
        self.commands = CommandHandler(config)
        self.commands.tools = self.tools
        self.history: list[dict] = []
        self._step_count = 0
        self._prev_thinking = False
        self.last_prompt_tokens: int = 0
        self._last_build_chars: int = 0
        self.context_size: int = 0  # fetched from /api/ps on first successful load

    @property
    def step_count(self) -> int:
        return self._step_count

    def _apply_workspace(self) -> None:
        """Update tools executor after workspace change."""
        ws = self.config.workspace
        self.tools = ToolExecutor(
            workspace=ws,
            safety=self.config.safety.level,
            timeout=self.config.agent.timeout_seconds,
            max_chars=self.config.agent.max_output_chars,
        )
        log.log_event("workspace_changed", str(ws))

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(workspace=self.config.workspace, safety=self.config.safety.level)

    def _fallback_system_prompt(self) -> str:
        return FALLBACK_SYSTEM_PROMPT.format(workspace=self.config.workspace)

    def _build_messages(self, system_override: str | None = None) -> list[dict]:
        """Build message list with system prompt + truncated history."""
        sys_content = system_override or self._system_prompt()
        messages = [{"role": "system", "content": sys_content}]
        effective_ctx = self.config.model.num_ctx or self.context_size or 200_000
        budget = effective_ctx * 4  # chars ≈ 4× token count

        used = len(messages[0]["content"])

        selected: list[dict] = []
        for msg in reversed(self.history):
            content = msg.get("content", "")
            msg_chars = len(content) + len(json.dumps(msg.get("tool_calls", [])))
            if used + msg_chars > budget:
                break
            selected.append(msg)
            used += msg_chars

        selected.reverse()
        # For fallback messages, strip tool-related messages (role=tool)
        # since the fallback doesn't use tool calling
        if system_override:
            selected = [m for m in selected if m.get("role") != "tool"]
            # Also strip tool_calls from assistant messages
            cleaned = []
            for m in selected:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    cleaned.append({"role": "assistant", "content": m.get("content", "")})
                else:
                    cleaned.append(m)
            selected = cleaned

        messages.extend(selected)

        # Track chars for token estimation (not for fallback builds)
        if not system_override:
            self._last_build_chars = used

        log.log_history_state(self.history)
        log.get_logger().debug(
            f"BUILD_MESSAGES | total={len(messages)} | history={len(self.history)} "
            f"| selected={len(selected)} | chars={used}"
        )
        return messages

    def clear_history(self) -> None:
        self.history.clear()
        self._step_count = 0
        self.last_prompt_tokens = 0
        self._last_build_chars = 0
        log.log_event("clear_history")

    async def _stream_response(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a single LLM call and yield events. Returns accumulated state via the events."""
        try:
            async for chunk in self.client.chat_stream(
                messages=messages,
                model=self.config.model.name,
                tools=tools,
                think=self.config.model.thinking,
                num_ctx=self.config.model.num_ctx,
            ):
                if chunk.content:
                    yield StreamEvent(type="content", text=chunk.content)
                if chunk.thinking:
                    yield StreamEvent(type="thinking", text=chunk.thinking)
                if chunk.tool_calls:
                    yield StreamEvent(type="_tool_calls_raw", text=json.dumps(chunk.tool_calls))
                if chunk.done and chunk.eval_count:
                    yield StreamEvent(type="_eval_count", text=str(chunk.eval_count))
                if chunk.done and chunk.prompt_eval_count:
                    yield StreamEvent(type="_prompt_eval_count", text=str(chunk.prompt_eval_count))
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.log_error("stream_response", e)
            yield StreamEvent(type="error", text=f"Ollama connection refused: {e}")
        except (httpx.RemoteProtocolError, httpx.ReadError) as e:
            log.log_error("stream_response", e)
            yield StreamEvent(type="error", text=f"Ollama connection lost mid-stream: {e}")
        except Exception as e:
            log.log_error("stream_response", e)
            yield StreamEvent(type="error", text=f"Ollama error: {e}")

    async def _fallback_code_execution(
        self, content: str
    ) -> AsyncIterator[StreamEvent]:
        """Parse code blocks from content and execute them as bash or python."""
        blocks = _parse_code_blocks(content)
        if not blocks:
            log.log_event("fallback_no_blocks", f"no code blocks found in {len(content)} chars")
            return

        for lang, code in blocks:
            if lang in ("bash", "sh", "shell", "zsh", ""):
                yield StreamEvent(
                    type="tool_call",
                    tool_name="bash (fallback)",
                    tool_args={"command": code},
                )
                log.log_tool_exec_start("bash_fallback", {"command": code}, "fallback")
                t0 = time.monotonic()
                result = await self.tools.execute("bash", {"command": code})
                duration = time.monotonic() - t0
                log.log_tool_exec_end("bash_fallback", "fallback", result, duration)

                yield StreamEvent(type="tool_result", text=result, tool_name="bash")

                # Add to history so the model sees the result
                self.history.append({
                    "role": "assistant",
                    "content": f"I ran: {code}",
                })
                self.history.append({
                    "role": "user",
                    "content": f"Command output:\n{result}",
                })

            elif lang in ("python", "py"):
                yield StreamEvent(
                    type="tool_call",
                    tool_name="python_exec (fallback)",
                    tool_args={"code": code[:100] + ("..." if len(code) > 100 else "")},
                )
                log.log_tool_exec_start("python_fallback", {"code": code[:100]}, "fallback")
                t0 = time.monotonic()
                result = await self.tools.execute("python_exec", {"code": code})
                duration = time.monotonic() - t0
                log.log_tool_exec_end("python_fallback", "fallback", result, duration)

                yield StreamEvent(type="tool_result", text=result, tool_name="python_exec")

                self.history.append({
                    "role": "assistant",
                    "content": f"I ran Python code.",
                })
                self.history.append({
                    "role": "user",
                    "content": f"Code output:\n{result}",
                })

    async def _info_command(self) -> AsyncIterator[StreamEvent]:
        """Fetch Ollama server info, /api/ps and /api/show and yield a formatted content event."""
        model = self.config.model.name

        version, running_models, show_data = await asyncio.gather(
            self.client.get_version(),
            self.client.get_running_models(),
            self.client.get_model_info(model),
        )

        ps_data: dict = {}
        for m in running_models:
            name = m.get("name", "")
            if name == model or name.split(":")[0] == model.split(":")[0]:
                ps_data = m
                break

        from rich.markup import escape as mesc

        def section(title: str, rows: list[tuple[str, str]]) -> str:
            """Render a titled section with aligned key-value rows as Rich markup."""
            label_w = max((len(r[0]) for r in rows), default=10)
            sep = "─" * (label_w + 24)
            lines = [f"[bold cyan]{mesc(title)}[/]", f"[dim]{sep}[/]"]
            for label, value in rows:
                lines.append(f"[dim]{label:<{label_w}}[/]  {mesc(value)}")
            return "\n".join(lines)

        parts: list[str] = []

        # ── Ollama server ────────────────────────────────────────────────────
        server_rows: list[tuple[str, str]] = [
            ("Version", version),
            ("URL",     self.config.ollama.base_url),
        ]
        parts.append(section("Ollama", server_rows))

        # ── Model header ─────────────────────────────────────────────────────
        parts.append(f"[bold]{mesc(model)}[/]")

        det = show_data.get("details", {})
        caps = show_data.get("capabilities", [])
        modified = show_data.get("modified_at", "")
        meta_rows: list[tuple[str, str]] = []
        if det.get("family"):             meta_rows.append(("Family",       det["family"]))
        if det.get("format"):             meta_rows.append(("Format",       det["format"].upper()))
        if det.get("parameter_size"):     meta_rows.append(("Size",         det["parameter_size"]))
        if det.get("quantization_level"): meta_rows.append(("Quantization", det["quantization_level"]))
        if caps:                          meta_rows.append(("Capabilities", ", ".join(caps)))
        if modified:                      meta_rows.append(("Modified",     modified[:19].replace("T", " ")))
        if meta_rows:
            parts.append(section("Model", meta_rows))

        # ── Running instance (/api/ps) ───────────────────────────────────────
        if ps_data:
            ps_rows: list[tuple[str, str]] = []
            ctx = ps_data.get("context_length", 0)
            if ctx:
                ps_rows.append(("Context loaded", f"{ctx:,} tokens"))
            size_vram = ps_data.get("size_vram", 0)
            if size_vram:
                ps_rows.append(("VRAM usage", f"{size_vram / 1024**3:.2f} GB"))
            size_total = ps_data.get("size", 0)
            if size_total:
                ps_rows.append(("Model size", f"{size_total / 1024**3:.2f} GB"))
            expires = ps_data.get("expires_at", "")
            if expires:
                ps_rows.append(("Expires at", expires[:19].replace("T", " ")))
            if ps_rows:
                parts.append(section("Running instance", ps_rows))
        else:
            parts.append("[dim italic]Model not currently loaded[/]")

        # ── Parameters (/api/show) ───────────────────────────────────────────
        params_str = show_data.get("parameters", "").strip()
        if params_str:
            param_rows: list[tuple[str, str]] = []
            for line in params_str.splitlines():
                line_parts = line.strip().split(None, 1)
                if len(line_parts) == 2:
                    param_rows.append((line_parts[0], line_parts[1]))
                elif len(line_parts) == 1:
                    param_rows.append((line_parts[0], ""))
            if param_rows:
                parts.append(section("Parameters", param_rows))

        # ── Architecture (/api/show model_info) ─────────────────────────────
        mi = show_data.get("model_info", {})
        if mi:
            _arch_fields = {
                "context_length":           "Context length",
                "embedding_length":         "Embedding length",
                "block_count":              "Layers",
                "feed_forward_length":      "Feed-forward length",
                "attention.head_count":     "Attention heads",
                "attention.head_count_kv":  "Attention heads KV",
            }
            arch_rows: list[tuple[str, str]] = []
            arch = mi.get("general.architecture", "")
            if arch:
                arch_rows.append(("Architecture", arch))
            param_count = mi.get("general.parameter_count", 0)
            if param_count:
                arch_rows.append(("Parameters", f"{param_count:,}"))
            for key, val in sorted(mi.items()):
                suffix = key.split(".", 1)[-1] if "." in key else key
                label = _arch_fields.get(suffix)
                if label:
                    arch_rows.append((label, f"{val:,}" if isinstance(val, int) else str(val)))
            if arch_rows:
                parts.append(section("Architecture", arch_rows))

        yield StreamEvent(type="markup", text="\n\n".join(parts))
        yield StreamEvent(type="done")

    async def _poll_reconnect(self, timeout: float = 30.0) -> bool:
        """Poll Ollama health until it responds or timeout expires. Returns True if reconnected."""
        deadline = time.monotonic() + timeout
        delay = 0.5
        while time.monotonic() < deadline:
            if await self.client.check_health():
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
        return False

    async def _ask_confirm(self, action_id: str, params: dict, *, default: bool = True) -> bool:
        """Ask for confirmation if confirm_fn is set, otherwise return default."""
        if self.tools.confirm_fn:
            return await self.tools.confirm_fn(action_id, params)
        return default

    async def _stream_subprocess_output(self, proc: asyncio.subprocess.Process) -> AsyncIterator[StreamEvent]:
        """Yield progress events for each non-empty line from proc.stdout, then wait."""
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            if line:
                yield StreamEvent(type="progress", text=line)
        await proc.wait()

    async def _pull_command(self, model: str) -> AsyncIterator[StreamEvent]:
        """Pull a model from Ollama with live progress events."""
        queue: asyncio.Queue = asyncio.Queue()

        def _cb(status: str, completed: int, total: int) -> None:
            if total > 0:
                pct = completed * 100 // total
                mb_done = completed // (1024 * 1024)
                mb_total = total // (1024 * 1024)
                text = f"{status}: {mb_done}/{mb_total} MB ({pct}%)"
            else:
                text = status
            queue.put_nowait(StreamEvent(type="progress", text=text))

        pull_task = asyncio.create_task(self.client.pull_model(model, callback=_cb))

        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.05)
                yield ev
            except asyncio.TimeoutError:
                if pull_task.done():
                    break

        # Drain any final events the task put in just before finishing
        while not queue.empty():
            yield queue.get_nowait()

        try:
            success = pull_task.result()
        except Exception as e:
            yield StreamEvent(type="error", text=f"Pull failed: {e}")
            yield StreamEvent(type="done")
            return

        if success:
            yield StreamEvent(type="status", text=f"✓ Successfully pulled {model}")
        else:
            yield StreamEvent(type="error", text=f"Failed to pull {model}")
        yield StreamEvent(type="done")

    async def _pull_all_command(self) -> AsyncIterator[StreamEvent]:
        """Pull every locally installed model to update them to the latest version."""
        try:
            models = await self.client.list_models()
        except Exception as e:
            yield StreamEvent(type="error", text=f"Failed to list models: {e}")
            yield StreamEvent(type="done")
            return

        if not models:
            yield StreamEvent(type="status", text="No local models found.")
            yield StreamEvent(type="done")
            return

        names = [m["name"] for m in models]
        yield StreamEvent(type="status", text=f"Pulling {len(names)} model(s): {', '.join(names)}")

        failed: list[str] = []
        for i, name in enumerate(names, 1):
            yield StreamEvent(type="status", text=f"[{i}/{len(names)}] Pulling {name}...")
            pull_failed = False
            async for ev in self._pull_command(name):
                if ev.type == "done":
                    break
                if ev.type == "error":
                    pull_failed = True
                yield ev
            if pull_failed:
                failed.append(name)

        if failed:
            yield StreamEvent(
                type="status",
                text=f"Completed: {len(names) - len(failed)}/{len(names)} succeeded. Failed: {', '.join(failed)}",
            )
        else:
            yield StreamEvent(type="status", text=f"✓ All {len(names)} model(s) up to date.")
        yield StreamEvent(type="done")

    async def _detect_ollama_restart_cmd(self, system: str, is_brew: bool) -> str:
        """Return the appropriate shell command to restart the Ollama server."""
        if system == "Darwin":
            if is_brew:
                proc = await asyncio.create_subprocess_shell(
                    "brew services list 2>/dev/null | grep -q '^ollama '",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode == 0:
                    return "brew services restart ollama"
            # Check for a launchd-managed Ollama service
            proc = await asyncio.create_subprocess_shell(
                "launchctl list 2>/dev/null | grep -q com.ollama",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                return "launchctl stop com.ollama.ollama 2>/dev/null; launchctl start com.ollama.ollama"
            return "pkill -x ollama 2>/dev/null; sleep 1; ollama serve > /dev/null 2>&1 &"
        if system == "Linux":
            proc = await asyncio.create_subprocess_shell(
                "systemctl is-active --quiet ollama 2>/dev/null",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                return "systemctl restart ollama"
        return "pkill -x ollama 2>/dev/null; sleep 1; ollama serve > /dev/null 2>&1 &"

    async def _update_ollama_command(self) -> AsyncIterator[StreamEvent]:
        """Update Ollama to the latest version with confirmation and optional restart."""
        system = platform.system()
        if system not in ("Darwin", "Linux"):
            yield StreamEvent(
                type="status",
                text=(
                    f"Automatic update is not supported on {system}.\n"
                    "Visit https://ollama.com/download to update manually."
                ),
            )
            yield StreamEvent(type="done")
            return

        # Detect Homebrew vs direct install
        ollama_path = shutil.which("ollama") or ""
        is_brew = any(p in ollama_path for p in ["/homebrew", "/Homebrew", "/Cellar"])

        if system == "Darwin" and is_brew:
            update_cmd = "brew upgrade ollama"
        else:
            update_cmd = "curl -fsSL https://ollama.com/install.sh | sh"

        allowed = await self._ask_confirm("ollama_update", {"command": update_cmd}, default=True)

        if not allowed:
            yield StreamEvent(type="status", text="Update cancelled.")
            yield StreamEvent(type="done")
            return

        yield StreamEvent(type="status", text=f"Running: {update_cmd}")

        proc = await asyncio.create_subprocess_shell(
            update_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for ev in self._stream_subprocess_output(proc):
            yield ev

        if proc.returncode != 0:
            yield StreamEvent(
                type="error",
                text=f"Update failed (exit {proc.returncode}). Try running manually:\n  {update_cmd}",
            )
            yield StreamEvent(type="done")
            return

        yield StreamEvent(type="status", text="✓ Ollama updated successfully.")

        restart_allowed = await self._ask_confirm(
            "ollama_restart",
            {"action": "restart the Ollama server to apply the update"},
            default=False,
        )

        if not restart_allowed:
            yield StreamEvent(
                type="status",
                text="Restart skipped. Run 'ollama serve' (or restart the Ollama service) to use the updated version.",
            )
            yield StreamEvent(type="done")
            return

        restart_cmd = await self._detect_ollama_restart_cmd(system, is_brew)
        yield StreamEvent(type="status", text=f"Restarting Ollama: {restart_cmd}")

        restart_proc = await asyncio.create_subprocess_shell(
            restart_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for ev in self._stream_subprocess_output(restart_proc):
            yield ev

        # Poll until Ollama is healthy again
        yield StreamEvent(type="status", text="Waiting for Ollama to come back up...")
        if await self._poll_reconnect(timeout=30.0):
            ver = await self.client.get_version()
            yield StreamEvent(type="status", text=f"✓ Ollama is back up (version {ver}).")
        else:
            yield StreamEvent(
                type="status",
                text="Ollama has not come back up yet. Start it manually with: ollama serve",
            )
        yield StreamEvent(type="done")

    async def process_input(self, user_input: str) -> AsyncIterator[StreamEvent]:
        """Process user input and yield stream events."""
        log.log_user_input(user_input)
        # Resolve context window size once per session (doesn't change while the model is loaded).
        # Re-resolve if num_ctx config changes (always live) or context_size is still unknown.
        if self.config.model.num_ctx:
            self.context_size = self.config.model.num_ctx
        else:
            # /api/ps gives the actual context_length the running model was loaded with.
            # Re-check each turn: the model may not be loaded on the first call but will be
            # on subsequent ones. Only fall back to /api/show's architecture default if the
            # model isn't running yet and we have no value at all.
            loaded = await self.client.get_loaded_context_size(self.config.model.name)
            if loaded:
                self.context_size = loaded
            elif not self.context_size:
                self.context_size = await self.client.get_model_context_size(self.config.model.name)

        # Async commands handled before CommandHandler (need streaming or platform calls)
        stripped = user_input.strip()
        cmd = stripped.lower()
        if cmd.startswith("/pull"):
            parts = stripped.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            if arg.lower() == "all":
                async for ev in self._pull_all_command():
                    yield ev
            else:
                async for ev in self._pull_command(arg if arg else self.config.model.name):
                    yield ev
            return

        if cmd.startswith("/update"):
            parts = stripped.split(maxsplit=1)
            subcmd = parts[1].strip().lower() if len(parts) > 1 else ""
            if subcmd == "ollama":
                async for ev in self._update_ollama_command():
                    yield ev
            elif subcmd == "models":
                async for ev in self._pull_all_command():
                    yield ev
            else:
                yield StreamEvent(type="status", text="Usage: /update ollama | /update models")
                yield StreamEvent(type="done")
            return

        if cmd == "/info":
            async for ev in self._info_command():
                yield ev
            return

        # Handle slash commands and shell escape
        if self.commands.is_command(user_input):
            result = self.commands.handle(user_input)
            log.log_command(user_input, result.output)

            # Shell escape: execute bash command directly
            if result.shell_command:
                shell_cmd = result.shell_command
                yield StreamEvent(
                    type="tool_call",
                    tool_name="bash (shell)",
                    tool_args={"command": shell_cmd},
                )
                t0 = time.monotonic()
                shell_result = await self.tools.execute("bash", {"command": shell_cmd}, confirm=False)
                log.log_tool_exec_end("bash_shell", "shell", shell_result, time.monotonic() - t0)
                yield StreamEvent(type="tool_result", text=shell_result, tool_name="bash")
                yield StreamEvent(type="done")
                return

            # Workspace changed: update tools executor
            if result.workspace_changed:
                self._apply_workspace()

            if result.clear_history:
                self.clear_history()
            yield StreamEvent(type="status", text=result.output)
            yield StreamEvent(type="done", text="quit" if result.should_quit else "")
            return

        # Check for inline /think once suffix (e.g. "explain this /think once")
        think_once_inline = False
        if re.search(r'/think\s+once\s*$', user_input, re.IGNORECASE):
            user_input = re.sub(r'\s*/think\s+once\s*$', '', user_input, flags=re.IGNORECASE).strip()
            if user_input:
                think_once_inline = True
                self._prev_thinking = self.config.model.thinking
                self.config.model.thinking = True
                yield StreamEvent(type="status", text="Thinking mode: ON (this message only)")

        # Reconnect if Ollama became unavailable between turns (e.g. after /update-ollama)
        if not await self.client.check_health():
            yield StreamEvent(type="status", text="⚠ Ollama is not responding. Reconnecting...")
            if await self._poll_reconnect():
                yield StreamEvent(type="status", text="✓ Reconnected to Ollama.")
            else:
                yield StreamEvent(
                    type="error",
                    text="Could not reconnect to Ollama after 30s. Is it still running?",
                )
                return

        # Add user message to history
        self.history.append({"role": "user", "content": user_input})
        self._step_count = 0

        # Agent loop: keep going while model makes tool calls
        while True:
            self._step_count += 1
            log.log_agent_step(self._step_count, self.config.agent.max_steps)

            if self._step_count > self.config.agent.max_steps:
                log.log_event("max_steps_reached", f"step={self._step_count}")
                self.commands.consume_think_once()
                if think_once_inline:
                    self.config.model.thinking = self._prev_thinking
                yield StreamEvent(
                    type="error",
                    text=f"Reached max steps ({self.config.agent.max_steps}). Stopping.",
                )
                return

            messages = self._build_messages()
            # Char-based token estimate: 4 chars ≈ 1 token. Include tool schema
            # chars since they are sent on every call but not counted by
            # _build_messages. This gives a realistic estimate even when KV-cache
            # causes Ollama's prompt_eval_count to report only the delta.
            self.last_prompt_tokens = (self._last_build_chars + _TOOL_SCHEMAS_CHARS) // 4

            # Stream response from Ollama
            content_acc = ""
            thinking_acc = ""
            tool_calls: list[dict] = []
            eval_count = 0
            had_error = False

            async for ev in self._stream_response(messages, tools=TOOL_SCHEMAS):
                if ev.type == "content":
                    content_acc += ev.text
                    yield ev
                elif ev.type == "thinking":
                    thinking_acc += ev.text
                    yield ev
                elif ev.type == "_tool_calls_raw":
                    tool_calls.extend(json.loads(ev.text))
                elif ev.type == "_eval_count":
                    eval_count = int(ev.text)
                elif ev.type == "_prompt_eval_count":
                    # Use max of char estimate and Ollama's report. Ollama's value is
                    # accurate on a full evaluation but undercounts on KV-cache hits.
                    reported = int(ev.text)
                    self.last_prompt_tokens = max(self.last_prompt_tokens, reported)
                elif ev.type == "error":
                    had_error = True
                    yield ev

            if had_error:
                return

            log.log_event(
                "assistant_response",
                f"content_len={len(content_acc)} thinking_len={len(thinking_acc)} tool_calls={len(tool_calls)}"
            )

            # ---------- FALLBACK: model generated tokens but produced no visible output ----------
            # This happens with Gemma 4 MoE: thinking consumes tokens but tool calls
            # are never emitted for write/create tasks. Detected by: no content, no
            # tool_calls, but eval_count > 0 (or thinking was present).
            empty_response = not tool_calls and not content_acc.strip()
            had_hidden_activity = thinking_acc.strip() or eval_count > 10
            if empty_response and had_hidden_activity:
                log.log_event(
                    "fallback_triggered",
                    f"thinking_len={len(thinking_acc)} eval_count={eval_count} "
                    f"but no content/tools — retrying without tools"
                )
                yield StreamEvent(
                    type="status",
                    text="[fallback] Model planned but didn't call tools. Retrying without tool mode..."
                )

                # Retry without tools — model will produce text with code blocks
                fallback_messages = self._build_messages(
                    system_override=self._fallback_system_prompt()
                )

                fb_content = ""
                fb_thinking = ""
                had_error = False

                async for ev in self._stream_response(fallback_messages, tools=None):
                    if ev.type == "content":
                        fb_content += ev.text
                        yield ev
                    elif ev.type == "thinking":
                        fb_thinking += ev.text
                        yield ev
                    elif ev.type == "error":
                        had_error = True
                        yield ev

                if had_error:
                    return

                log.log_event(
                    "fallback_response",
                    f"content_len={len(fb_content)} thinking_len={len(fb_thinking)}"
                )

                # Add fallback response to history
                self.history.append({"role": "assistant", "content": fb_content})

                # Parse and execute code blocks from the fallback response
                blocks = _parse_code_blocks(fb_content)
                if blocks:
                    yield StreamEvent(
                        type="status",
                        text=f"[fallback] Executing {len(blocks)} code block(s) from response..."
                    )
                    async for fb_ev in self._fallback_code_execution(fb_content):
                        yield fb_ev

                self.commands.consume_think_once()
                if think_once_inline:
                    self.config.model.thinking = self._prev_thinking
                yield StreamEvent(type="done")
                return

            # ---------- Normal path ----------

            # Build assistant message for history (strip thinking)
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content_acc}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.history.append(assistant_msg)

            # No tool calls → done, wait for next user input
            if not tool_calls:
                log.log_event("turn_complete", "no tool calls, waiting for user")
                self.commands.consume_think_once()
                if think_once_inline:
                    self.config.model.thinking = self._prev_thinking
                yield StreamEvent(type="done")
                return

            # Yield tool_call events first
            for tc in tool_calls:
                func = tc.get("function", {})
                yield StreamEvent(
                    type="tool_call",
                    tool_name=func.get("name", ""),
                    tool_args=func.get("arguments", {}),
                    tool_id=tc.get("id", ""),
                )

            # Execute all tools in parallel
            async def _run_tool(tc: dict) -> tuple[str, str, str, dict]:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})
                call_id = tc.get("id", "")

                log.log_tool_exec_start(name, args, call_id)
                t0 = time.monotonic()

                try:
                    result = await self.tools.execute(name, args)
                except Exception as e:
                    log.log_error(f"tool_exec_{name}", e)
                    result = f"Error: {type(e).__name__}: {e}"

                duration = time.monotonic() - t0
                log.log_tool_exec_end(name, call_id, result, duration)
                return call_id, name, result, args

            tasks = [_run_tool(tc) for tc in tool_calls]
            results = await asyncio.gather(*tasks)

            # Add tool results to history and yield events
            for call_id, name, result, args in results:
                self.history.append({
                    "role": "tool",
                    "content": result,
                    "tool_call_id": call_id,
                })
                yield StreamEvent(
                    type="tool_result",
                    text=result,
                    tool_name=name,
                    tool_id=call_id,
                )

            log.log_event("tool_loop_continue", f"tool_results={len(results)}, continuing agent loop")
            # Continue loop — model will see tool results
