"""Async Ollama API client using httpx. No third-party Ollama libraries."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import httpx

from . import logging as log


@dataclass
class ChatChunk:
    """A single streaming chunk from /api/chat."""
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    done: bool = False
    done_reason: str = ""
    # Stats from final chunk
    eval_count: int = 0
    prompt_eval_count: int = 0
    eval_duration: int = 0          # nanoseconds
    prompt_eval_duration: int = 0   # nanoseconds
    total_duration: int = 0         # nanoseconds
    load_duration: int = 0          # nanoseconds


@dataclass
class ChatResponse:
    """Accumulated response from a complete /api/chat call."""
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    done_reason: str = ""
    eval_count: int = 0
    prompt_eval_count: int = 0


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434", timeout: float = 300.0):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def close(self) -> None:
        if hasattr(self, "_log_monitor_task"):
            self._log_monitor_task.cancel()
        await self._client.aclose()

    async def check_health(self) -> bool:
        """Check if Ollama is running."""
        try:
            t0 = time.monotonic()
            r = await self._client.get("/")
            ok = r.status_code == 200
            log.log_startup("health_check", f"status={r.status_code} ok={ok} duration={time.monotonic()-t0:.3f}s")
            return ok
        except httpx.ConnectError as e:
            log.log_startup("health_check", f"FAILED: {e}")
            return False

    async def get_version(self) -> str:
        """GET /api/version — return Ollama server version string."""
        try:
            r = await self._client.get("/api/version")
            r.raise_for_status()
            ver = r.json().get("version", "unknown")
            log.log_startup("ollama_version", f"server={ver}")
            return ver
        except Exception as e:
            log.log_startup("ollama_version", f"FAILED: {e}")
            return "unknown"

    async def get_running_models(self) -> list[dict]:
        """GET /api/ps — list currently loaded/running models."""
        try:
            r = await self._client.get("/api/ps")
            r.raise_for_status()
            models = r.json().get("models", [])
            for m in models:
                log.log_startup(
                    "running_model",
                    f"name={m.get('name')} size={m.get('size')} "
                    f"vram={m.get('size_vram')} ctx={m.get('context_length')} expires={m.get('expires_at')}"
                )
            return models
        except Exception:
            return []

    async def get_loaded_context_size(self, model: str) -> int:
        """Return context_length for a currently loaded model via /api/ps. Returns 0 if not loaded."""
        models = await self.get_running_models()
        for m in models:
            name = m.get("name", "")
            if name == model or name.split(":")[0] == model.split(":")[0]:
                ctx = m.get("context_length", 0)
                if ctx:
                    log.log_startup("get_loaded_context_size", f"model={model} context_length={ctx}")
                    return int(ctx)
        return 0

    async def start_log_monitor(self) -> None:
        """Start background task to tail Ollama server logs if available.

        Ollama server logs go to stderr of the `ollama serve` process.
        On macOS with Homebrew there's no persistent log file by default,
        so we find the PID and read from /dev/fd/<pid>/2 if possible.
        This is best-effort — if we can't read the logs, we silently skip.
        """
        self._log_monitor_task = asyncio.create_task(self._tail_ollama_logs())

    async def _tail_ollama_logs(self) -> None:
        """Best-effort Ollama server diagnostics via API."""
        try:
            version = await self.get_version()
            await self.get_running_models()

            # Check for version mismatch (common issue: stale server)
            try:
                result = await asyncio.create_subprocess_exec(
                    "ollama", "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await result.communicate()
                cli_output = (out or err or b"").decode().strip()
                # Parse "ollama version is X.Y.Z"
                if "version" in cli_output.lower():
                    m = re.search(r'(\d+\.\d+\.\d+)', cli_output)
                    if m:
                        cli_ver = m.group(1)
                        if cli_ver != version:
                            log.get_logger().warning(
                                f"OLLAMA_VERSION_MISMATCH | server={version} cli={cli_ver} "
                                f"— restart ollama serve to use the updated version"
                            )
            except FileNotFoundError:
                pass  # ollama CLI not on PATH

        except Exception as e:
            log.log_startup("log_monitor", f"failed: {e}")

    async def get_model_info(self, model: str) -> dict:
        """POST /api/show — return full model info dict (details, model_info, parameters, capabilities)."""
        try:
            r = await self._client.post("/api/show", json={"name": model})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.log_error("get_model_info", e)
            return {}

    async def get_model_context_size(self, model: str) -> int:
        """Return num_ctx for a model via /api/show. Returns 0 if unavailable."""
        try:
            r = await self._client.post("/api/show", json={"name": model})
            r.raise_for_status()
            data = r.json()
            # num_ctx set explicitly in the Modelfile parameters block
            for line in data.get("parameters", "").splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[0].lower() == "num_ctx":
                    return int(parts[1])
            # fallback: native architecture context_length from model_info
            for key, val in data.get("model_info", {}).items():
                if key.endswith(".context_length"):
                    return int(val)
        except Exception:
            pass
        return 0

    async def list_models(self) -> list[dict]:
        """GET /api/tags — list installed models."""
        t0 = time.monotonic()
        r = await self._client.get("/api/tags")
        r.raise_for_status()
        models = r.json().get("models", [])
        names = [m.get("name", "?") for m in models]
        log.log_startup("list_models", f"count={len(models)} names={names} duration={time.monotonic()-t0:.3f}s")
        return models

    async def has_model(self, name: str) -> tuple[bool, list[dict]]:
        """Check if a model is locally available. Returns (found, models_list)."""
        models = await self.list_models()
        for m in models:
            mn = m.get("name", "")
            if mn == name or mn.split(":")[0] == name.split(":")[0]:
                log.log_startup("has_model", f"model={name} found as {mn}")
                return True, models
        log.log_startup("has_model", f"model={name} NOT FOUND")
        return False, models

    async def pull_model(
        self,
        name: str,
        callback: Callable[[str, int, int], None] | None = None,
    ) -> bool:
        """POST /api/pull — pull a model with streaming progress."""
        log.log_startup("pull_model", f"starting pull for {name}")
        async with self._client.stream(
            "POST",
            "/api/pull",
            json={"model": name, "stream": True},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                status = data.get("status", "")
                completed = data.get("completed", 0)
                total = data.get("total", 0)
                if callback:
                    callback(status, completed, total)
                if status == "success":
                    log.log_startup("pull_model", f"SUCCESS for {name}")
                    return True
        log.log_startup("pull_model", f"FAILED for {name} (no success status)")
        return False

    async def chat_stream(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        think: bool = False,
        temperature: float = 1.0,
        top_p: float = 0.95,
        top_k: int = 64,
        num_ctx: int = 0,
    ) -> AsyncIterator[ChatChunk]:
        """POST /api/chat with streaming. Yields ChatChunk objects."""
        options: dict[str, Any] = {
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if num_ctx:
            options["num_ctx"] = num_ctx
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
            "options": options,
        }
        if tools:
            payload["tools"] = tools

        log.log_api_request(model, len(messages), bool(tools), think)
        log.log_api_request_messages(messages)

        t0 = time.monotonic()
        chunk_count = 0
        content_acc = ""
        thinking_acc = ""
        all_tool_calls: list[dict] = []
        last_eval = 0
        last_prompt_eval = 0

        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    msg = data.get("message", {})
                    chunk = ChatChunk(
                        content=msg.get("content", ""),
                        thinking=msg.get("thinking", ""),
                        tool_calls=msg.get("tool_calls", []),
                        done=data.get("done", False),
                        done_reason=data.get("done_reason", ""),
                        eval_count=data.get("eval_count", 0),
                        prompt_eval_count=data.get("prompt_eval_count", 0),
                        eval_duration=data.get("eval_duration", 0),
                        prompt_eval_duration=data.get("prompt_eval_duration", 0),
                        total_duration=data.get("total_duration", 0),
                        load_duration=data.get("load_duration", 0),
                    )

                    chunk_count += 1
                    content_acc += chunk.content
                    thinking_acc += chunk.thinking
                    if chunk.tool_calls:
                        all_tool_calls.extend(chunk.tool_calls)
                    if chunk.eval_count:
                        last_eval = chunk.eval_count
                    if chunk.prompt_eval_count:
                        last_prompt_eval = chunk.prompt_eval_count

                    log.log_api_chunk(chunk_count, chunk.content, chunk.thinking, chunk.tool_calls, chunk.done)

                    # Log full raw JSON on final chunk for diagnostics
                    if chunk.done:
                        log.get_logger().debug(f"RAW_FINAL_CHUNK | {json.dumps(data)}")

                    yield chunk

        except Exception as e:
            log.log_error("chat_stream", e)
            raise

        duration = time.monotonic() - t0
        log.log_api_response_complete(
            content_acc, thinking_acc, all_tool_calls,
            chunk_count, duration,
            last_eval, last_prompt_eval,
        )

        # Detect suspected Ollama tool parsing bug (ollama/ollama#15315):
        # Model generates tokens (eval_count > 0) but response is empty.
        # This happens when Gemma 4's custom tool format can't be parsed.
        if (
            tools
            and last_eval > 10
            and not content_acc.strip()
            and not thinking_acc.strip()
            and not all_tool_calls
        ):
            log.get_logger().warning(
                f"SUSPECTED_TOOL_PARSE_BUG | eval_count={last_eval} but empty response. "
                f"See https://github.com/ollama/ollama/issues/15315 — "
                f"Gemma 4 tool calls use custom delimiters that Ollama fails to parse. "
                f"Tokens were generated but Ollama could not convert them to tool_calls."
            )

    async def chat(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        think: bool = False,
        temperature: float = 1.0,
        top_p: float = 0.95,
        top_k: int = 64,
        num_ctx: int = 0,
    ) -> ChatResponse:
        """POST /api/chat without streaming. Returns accumulated ChatResponse."""
        options: dict[str, Any] = {
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if num_ctx:
            options["num_ctx"] = num_ctx
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": think,
            "options": options,
        }
        if tools:
            payload["tools"] = tools

        log.log_api_request(model, len(messages), bool(tools), think)
        log.log_api_request_messages(messages)

        t0 = time.monotonic()
        try:
            r = await self._client.post("/api/chat", json=payload)
            r.raise_for_status()
        except Exception as e:
            log.log_error("chat", e)
            raise

        data = r.json()
        msg = data.get("message", {})
        resp = ChatResponse(
            content=msg.get("content", ""),
            thinking=msg.get("thinking", ""),
            tool_calls=msg.get("tool_calls", []),
            done_reason=data.get("done_reason", ""),
            eval_count=data.get("eval_count", 0),
            prompt_eval_count=data.get("prompt_eval_count", 0),
        )

        duration = time.monotonic() - t0
        log.log_api_response_complete(
            resp.content, resp.thinking, resp.tool_calls,
            1, duration, resp.eval_count, resp.prompt_eval_count,
        )
        return resp
