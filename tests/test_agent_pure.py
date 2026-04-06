"""Tests for pure (non-async) parts of nanoharness/agent.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanoharness.agent import _parse_code_blocks, StreamEvent, Agent
from nanoharness.config import Config
from nanoharness.ollama import OllamaClient


class TestParseCodeBlocks:
    def test_single_bash(self):
        text = "Here is code:\n```bash\nls -la\n```\nDone."
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0] == ("bash", "ls -la")

    def test_multiple_blocks(self):
        text = "```bash\necho hi\n```\nThen:\n```python\nprint(1)\n```"
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 2
        assert blocks[0] == ("bash", "echo hi")
        assert blocks[1] == ("python", "print(1)")

    def test_no_blocks(self):
        assert _parse_code_blocks("just plain text") == []

    def test_no_language(self):
        text = "```\necho hello\n```"
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0][0] == "bash"  # defaults to bash

    def test_empty_block_ignored(self):
        text = "```bash\n\n```"
        blocks = _parse_code_blocks(text)
        assert blocks == []

    def test_multiline_code(self):
        text = "```python\nfor i in range(3):\n    print(i)\n```"
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 1
        assert "for i in range(3)" in blocks[0][1]
        assert "print(i)" in blocks[0][1]


class TestStreamEvent:
    def test_basic_to_dict(self):
        ev = StreamEvent(type="content", text="hello")
        d = ev.to_dict()
        assert d == {"type": "content", "text": "hello"}

    def test_tool_name_included(self):
        ev = StreamEvent(type="tool_call", text="", tool_name="bash")
        d = ev.to_dict()
        assert d["tool_name"] == "bash"

    def test_tool_name_excluded_when_empty(self):
        ev = StreamEvent(type="content", text="hi")
        d = ev.to_dict()
        assert "tool_name" not in d

    def test_tool_args_included(self):
        ev = StreamEvent(type="tool_call", text="", tool_name="bash", tool_args={"command": "ls"})
        d = ev.to_dict()
        assert d["tool_args"] == {"command": "ls"}

    def test_tool_args_excluded_when_empty(self):
        ev = StreamEvent(type="content", text="hi")
        d = ev.to_dict()
        assert "tool_args" not in d

    def test_tool_id_included(self):
        ev = StreamEvent(type="tool_call", text="", tool_name="bash", tool_id="call_123")
        d = ev.to_dict()
        assert d["tool_id"] == "call_123"

    def test_tool_id_excluded_when_empty(self):
        ev = StreamEvent(type="done", text="")
        d = ev.to_dict()
        assert "tool_id" not in d


class TestBuildMessages:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    def test_empty_history(self, agent: Agent):
        msgs = agent._build_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"
        assert "coding agent" in msgs[0]["content"]

    def test_history_included(self, agent: Agent):
        agent.history.append({"role": "user", "content": "hello"})
        agent.history.append({"role": "assistant", "content": "hi"})
        msgs = agent._build_messages()
        assert len(msgs) == 3
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"

    def test_fallback_strips_tool_messages(self, agent: Agent):
        agent.history.append({"role": "user", "content": "hello"})
        agent.history.append({"role": "assistant", "content": "using tool", "tool_calls": [{"id": "1"}]})
        agent.history.append({"role": "tool", "content": "result", "tool_call_id": "1"})
        msgs = agent._build_messages(system_override="fallback prompt")
        # tool role messages should be stripped
        roles = [m["role"] for m in msgs]
        assert "tool" not in roles
        # assistant messages should have tool_calls stripped
        for m in msgs:
            assert "tool_calls" not in m

    def test_system_prompt_contains_workspace(self, agent: Agent):
        msgs = agent._build_messages()
        assert str(agent.config.workspace) in msgs[0]["content"]


class TestClearHistory:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    def test_clear(self, agent: Agent):
        agent.history.append({"role": "user", "content": "hello"})
        agent._step_count = 5
        agent.clear_history()
        assert agent.history == []
        assert agent.step_count == 0
