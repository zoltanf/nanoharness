"""Tests for pure (non-async) parts of nanoharness/agent.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def test_progress_serializes(self):
        ev = StreamEvent(type="progress", text="pulling: 500/1000 MB (50%)")
        d = ev.to_dict()
        assert d["type"] == "progress"
        assert d["text"] == "pulling: 500/1000 MB (50%)"


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


class TestPollReconnect:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    async def test_returns_true_immediately_when_healthy(self, agent: Agent):
        """Check health before sleeping — returns True on first call with no sleep."""
        call_order: list[str] = []

        async def record_check() -> bool:
            call_order.append("check")
            return True

        async def record_sleep(_: float) -> None:
            call_order.append("sleep")

        agent.client.check_health = record_check
        with patch("nanoharness.agent.asyncio.sleep", record_sleep):
            result = await agent._poll_reconnect(timeout=5.0)

        assert result is True
        assert call_order == ["check"]  # no sleep: returned before reaching it

    async def test_returns_false_after_timeout(self, agent: Agent):
        agent.client.check_health = AsyncMock(return_value=False)
        with patch("nanoharness.agent.asyncio.sleep"):
            result = await agent._poll_reconnect(timeout=0.001)
        assert result is False

    async def test_returns_true_on_eventual_reconnect(self, agent: Agent):
        agent.client.check_health = AsyncMock(side_effect=[False, False, True])
        with patch("nanoharness.agent.asyncio.sleep"):
            result = await agent._poll_reconnect(timeout=10.0)
        assert result is True
        assert agent.client.check_health.call_count == 3


class TestAskConfirm:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    async def test_delegates_to_confirm_fn_true(self, agent: Agent):
        agent.tools.confirm_fn = AsyncMock(return_value=True)
        result = await agent._ask_confirm("ollama_update", {"command": "brew upgrade ollama"})
        assert result is True
        agent.tools.confirm_fn.assert_called_once_with(
            "ollama_update", {"command": "brew upgrade ollama"}
        )

    async def test_delegates_to_confirm_fn_false(self, agent: Agent):
        agent.tools.confirm_fn = AsyncMock(return_value=False)
        result = await agent._ask_confirm("bash", {"command": "rm -rf /"})
        assert result is False

    async def test_no_confirm_fn_returns_default_true(self, agent: Agent):
        agent.tools.confirm_fn = None
        assert await agent._ask_confirm("bash", {}, default=True) is True

    async def test_no_confirm_fn_returns_default_false(self, agent: Agent):
        agent.tools.confirm_fn = None
        assert await agent._ask_confirm("ollama_restart", {}, default=False) is False


class TestPullRouting:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    async def test_uses_config_model_when_no_arg(self, agent: Agent):
        agent.config.model.name = "gemma3:12b"
        captured: list[str] = []

        async def fake_pull(model: str):
            captured.append(model)
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_command", fake_pull):
            [e async for e in agent.process_input("/pull")]

        assert captured == ["gemma3:12b"]

    async def test_uses_explicit_model_arg(self, agent: Agent):
        captured: list[str] = []

        async def fake_pull(model: str):
            captured.append(model)
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_command", fake_pull):
            [e async for e in agent.process_input("/pull llama3:8b")]

        assert captured == ["llama3:8b"]

    async def test_yields_all_events_from_pull_command(self, agent: Agent):
        async def fake_pull(model: str):
            yield StreamEvent(type="progress", text="downloading: 10/100 MB")
            yield StreamEvent(type="status", text="✓ done")
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_command", fake_pull):
            events = [e async for e in agent.process_input("/pull mymodel")]

        assert [e.type for e in events] == ["progress", "status", "done"]

    async def test_pull_all_routes_to_pull_all_command(self, agent: Agent):
        called: list[bool] = []

        async def fake_pull_all():
            called.append(True)
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_all_command", fake_pull_all):
            [e async for e in agent.process_input("/pull all")]

        assert called == [True]


class TestPullAllCommand:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    async def test_no_models_yields_status_and_done(self, agent: Agent):
        agent.client.list_models = AsyncMock(return_value=[])
        events = [e async for e in agent._pull_all_command()]
        assert events[-1].type == "done"
        assert any("No local models" in e.text for e in events if e.type == "status")

    async def test_pulls_each_model_in_sequence(self, agent: Agent):
        agent.client.list_models = AsyncMock(return_value=[
            {"name": "gemma3:12b"},
            {"name": "llama3:8b"},
        ])
        pulled: list[str] = []

        async def fake_pull(model: str):
            pulled.append(model)
            yield StreamEvent(type="status", text=f"✓ Successfully pulled {model}")
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_command", fake_pull):
            events = [e async for e in agent._pull_all_command()]

        assert pulled == ["gemma3:12b", "llama3:8b"]
        assert events[-1].type == "done"
        # Final summary mentions success
        assert any("✓ All 2" in e.text for e in events if e.type == "status")

    async def test_partial_failure_reported_in_summary(self, agent: Agent):
        agent.client.list_models = AsyncMock(return_value=[
            {"name": "good:latest"},
            {"name": "bad:latest"},
        ])

        async def fake_pull(model: str):
            if model == "bad:latest":
                yield StreamEvent(type="error", text=f"Failed to pull {model}")
            else:
                yield StreamEvent(type="status", text=f"✓ Successfully pulled {model}")
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_command", fake_pull):
            events = [e async for e in agent._pull_all_command()]

        summary = next(e.text for e in events if e.type == "status" and "succeeded" in e.text)
        assert "1/2" in summary
        assert "bad:latest" in summary

    async def test_list_models_error_yields_error_and_done(self, agent: Agent):
        agent.client.list_models = AsyncMock(side_effect=Exception("connection refused"))
        events = [e async for e in agent._pull_all_command()]
        assert any(e.type == "error" for e in events)
        assert events[-1].type == "done"

    async def test_intermediate_done_events_suppressed(self, agent: Agent):
        """The done from each individual _pull_command is swallowed; only one final done."""
        agent.client.list_models = AsyncMock(return_value=[
            {"name": "m1:latest"},
            {"name": "m2:latest"},
        ])

        async def fake_pull(model: str):
            yield StreamEvent(type="progress", text=f"pulling {model}")
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_command", fake_pull):
            events = [e async for e in agent._pull_all_command()]

        done_events = [e for e in events if e.type == "done"]
        assert len(done_events) == 1


class TestUpdateOllamaRouting:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    async def test_update_ollama_routes(self, agent: Agent):
        called: list[bool] = []

        async def fake_update():
            called.append(True)
            yield StreamEvent(type="done")

        with patch.object(agent, "_update_ollama_command", fake_update):
            [e async for e in agent.process_input("/update ollama")]

        assert called == [True]

    async def test_update_models_routes_to_pull_all(self, agent: Agent):
        called: list[bool] = []

        async def fake_pull_all():
            called.append(True)
            yield StreamEvent(type="done")

        with patch.object(agent, "_pull_all_command", fake_pull_all):
            [e async for e in agent.process_input("/update models")]

        assert called == [True]

    async def test_update_no_subcommand_yields_usage(self, agent: Agent):
        events = [e async for e in agent.process_input("/update")]
        assert any(e.type == "status" and "Usage" in e.text for e in events)
        assert any(e.type == "done" for e in events)

    async def test_unsupported_platform_yields_status_and_done(self, agent: Agent):
        with patch("nanoharness.agent.platform.system", return_value="Windows"):
            events = [e async for e in agent._update_ollama_command()]

        assert "done" in [e.type for e in events]
        status_texts = [e.text for e in events if e.type == "status"]
        assert any("not supported" in t.lower() for t in status_texts)
        assert any("https://ollama.com/download" in t for t in status_texts)

    async def test_brew_install_detected(self, agent: Agent):
        """Homebrew install path selects 'brew upgrade ollama'."""
        agent.tools.confirm_fn = AsyncMock(return_value=False)  # cancel after confirm
        with (
            patch("nanoharness.agent.platform.system", return_value="Darwin"),
            patch("nanoharness.agent.shutil.which", return_value="/opt/homebrew/bin/ollama"),
        ):
            events = [e async for e in agent._update_ollama_command()]

        # confirm was shown with the brew command
        call_args = agent.tools.confirm_fn.call_args
        assert call_args[0][0] == "ollama_update"
        assert "brew upgrade ollama" in call_args[0][1]["command"]

    async def test_direct_install_uses_curl(self, agent: Agent):
        """Non-brew macOS install selects the curl installer."""
        agent.tools.confirm_fn = AsyncMock(return_value=False)  # cancel after confirm
        with (
            patch("nanoharness.agent.platform.system", return_value="Darwin"),
            patch("nanoharness.agent.shutil.which", return_value="/usr/local/bin/ollama"),
        ):
            events = [e async for e in agent._update_ollama_command()]

        call_args = agent.tools.confirm_fn.call_args
        assert "install.sh" in call_args[0][1]["command"]


class TestReconnectCheck:
    @pytest.fixture
    def agent(self, config: Config, mock_client: AsyncMock) -> Agent:
        return Agent(config, mock_client)

    async def test_unhealthy_poll_fails_yields_error_and_returns_early(self, agent: Agent):
        """Health check fails + reconnect poll times out → yield error, skip LLM."""
        agent.client.check_health = AsyncMock(return_value=False)
        agent._poll_reconnect = AsyncMock(return_value=False)

        events = [e async for e in agent.process_input("hello")]

        assert any(e.type == "error" for e in events)
        error_text = next(e.text for e in events if e.type == "error")
        assert "30s" in error_text or "reconnect" in error_text.lower()
        # Early return: never reached history.append
        assert agent.history == []
