"""Core agent loop: conversation management, tool execution, history truncation."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Any

from .config import Config
from .ollama import OllamaClient
from .tools import TOOL_SCHEMAS, ToolExecutor
from .commands import CommandHandler
from . import logging as log


SYSTEM_PROMPT = "You are a coding agent. Use tools to complete tasks. Be direct and concise.\nWorking directory: {workspace}\nSafety: {safety} — all file and shell operations must stay within the working directory."

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
    type: str  # "content" | "thinking" | "tool_call" | "tool_result" | "done" | "error" | "status"
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
        self.history: list[dict] = []
        self._step_count = 0
        self._prev_thinking = False
        self.last_prompt_tokens: int = 0
        self._peak_prompt_tokens: int = 0  # cumulative max to handle KV-cache undercounting
        self.context_size: int = 0  # fetched from /api/show on first use

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
        self._peak_prompt_tokens = 0
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
        """Fetch /api/ps and /api/show and yield a formatted content event."""
        model = self.config.model.name

        running_models, show_data = await asyncio.gather(
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

        # ── Header ──────────────────────────────────────────────────────────
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

    async def process_input(self, user_input: str) -> AsyncIterator[StreamEvent]:
        """Process user input and yield stream events."""
        log.log_user_input(user_input)
        # Resolve context window size once per session (doesn't change while the model is loaded).
        # Re-resolve if num_ctx config changes (always live) or context_size is still unknown.
        if self.config.model.num_ctx:
            self.context_size = self.config.model.num_ctx
        elif not self.context_size:
            # /api/ps gives the actual context_length the running model was loaded with
            loaded = await self.client.get_loaded_context_size(self.config.model.name)
            self.context_size = loaded or await self.client.get_model_context_size(self.config.model.name)

        # /info — async command, handled before CommandHandler
        if user_input.strip().lower() == "/info":
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
                shell_result = await self.tools.execute("bash", {"command": shell_cmd})
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
            if result.should_quit:
                yield StreamEvent(type="done", text="quit")
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
                    reported = int(ev.text)
                    self._peak_prompt_tokens = max(self._peak_prompt_tokens, reported)
                    self.last_prompt_tokens = self._peak_prompt_tokens
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
