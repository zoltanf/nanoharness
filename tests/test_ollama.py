"""Tests for nanoharness/ollama.py — OllamaClient with mocked httpx."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nanoharness.ollama import OllamaClient, ChatChunk, ChatResponse


@pytest.fixture
def client() -> OllamaClient:
    return OllamaClient(base_url="http://localhost:11434")


class TestChatChunkDataclass:
    def test_defaults(self):
        c = ChatChunk()
        assert c.content == ""
        assert c.thinking == ""
        assert c.tool_calls == []
        assert c.done is False

    def test_fields(self):
        c = ChatChunk(content="hi", thinking="hmm", done=True, eval_count=42)
        assert c.content == "hi"
        assert c.eval_count == 42


class TestChatResponseDataclass:
    def test_defaults(self):
        r = ChatResponse()
        assert r.content == ""
        assert r.tool_calls == []

    def test_fields(self):
        r = ChatResponse(content="answer", eval_count=100)
        assert r.content == "answer"
        assert r.eval_count == 100


class TestCheckHealth:
    @pytest.mark.asyncio
    async def test_healthy(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.status_code = 200
        client._client.get = AsyncMock(return_value=mock_response)
        assert await client.check_health() is True

    @pytest.mark.asyncio
    async def test_connection_error(self, client: OllamaClient):
        client._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        assert await client.check_health() is False


class TestGetVersion:
    @pytest.mark.asyncio
    async def test_success(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.json.return_value = {"version": "0.20.2"}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        ver = await client.get_version()
        assert ver == "0.20.2"

    @pytest.mark.asyncio
    async def test_error(self, client: OllamaClient):
        client._client.get = AsyncMock(side_effect=Exception("fail"))
        ver = await client.get_version()
        assert ver == "unknown"


class TestListModels:
    @pytest.mark.asyncio
    async def test_success(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.json.return_value = {"models": [{"name": "gemma4:26b"}, {"name": "llama3:8b"}]}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        models = await client.list_models()
        assert len(models) == 2
        assert models[0]["name"] == "gemma4:26b"

    @pytest.mark.asyncio
    async def test_empty(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.json.return_value = {"models": []}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        models = await client.list_models()
        assert models == []


class TestHasModel:
    @pytest.mark.asyncio
    async def test_found(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.json.return_value = {"models": [{"name": "gemma4:26b"}]}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        found, models = await client.has_model("gemma4:26b")
        assert found is True
        assert len(models) == 1

    @pytest.mark.asyncio
    async def test_not_found(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.json.return_value = {"models": [{"name": "llama3:8b"}]}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        found, models = await client.has_model("gemma4:26b")
        assert found is False

    @pytest.mark.asyncio
    async def test_partial_match(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.json.return_value = {"models": [{"name": "gemma4:26b"}]}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)
        found, _ = await client.has_model("gemma4")
        assert found is True


class TestChat:
    @pytest.mark.asyncio
    async def test_non_streaming(self, client: OllamaClient):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "Hello!", "thinking": "", "tool_calls": []},
            "done": True,
            "done_reason": "stop",
            "eval_count": 10,
            "prompt_eval_count": 5,
        }
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        resp = await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gemma4:26b",
        )
        assert resp.content == "Hello!"
        assert resp.eval_count == 10

    @pytest.mark.asyncio
    async def test_with_tool_calls(self, client: OllamaClient):
        tool_call = {"function": {"name": "bash", "arguments": {"command": "ls"}}}
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "", "tool_calls": [tool_call]},
            "done": True,
            "eval_count": 5,
            "prompt_eval_count": 3,
        }
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        resp = await client.chat(
            messages=[{"role": "user", "content": "list files"}],
            model="gemma4:26b",
            tools=[{"type": "function", "function": {"name": "bash"}}],
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "bash"
