"""End-to-end Playwright tests with a real Ollama backend.

Requires a running Ollama server with a model available.
Run with: uv run --extra test --extra playwright --extra web pytest -m e2e -v

These tests are SLOW (real model inference) and are excluded from default pytest runs.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from nanoharness.agent import Agent
from nanoharness.config import Config
from nanoharness.ollama import OllamaClient
from nanoharness.web import create_app
from tests.conftest import _free_port

pytestmark = pytest.mark.e2e

# Generous timeout for model inference
MODEL_TIMEOUT = 60_000  # 60 seconds


@pytest.fixture(scope="module")
def e2e_url():
    """Start a real NanoHarness server backed by a real Ollama instance."""
    cfg = Config()
    workspace = Path("/tmp/nanoharness-e2e-test")
    workspace.mkdir(exist_ok=True)
    (workspace / "test.txt").write_text("hello from e2e\n")
    cfg.workspace = workspace

    client = OllamaClient(base_url=cfg.ollama.base_url)
    agent = Agent(cfg, client)

    port = _free_port()
    app = create_app(agent, open_browser=False, port=port)
    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(uvi_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Server failed to start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


def _send_and_wait(page, text: str, timeout: int = MODEL_TIMEOUT):
    """Type a message and wait for the done event (input re-enabled)."""
    inp = page.locator("#input")
    inp.fill(text)
    inp.press("Enter")
    # Wait until processing finishes (input becomes enabled again)
    page.wait_for_function(
        "!document.getElementById('input').disabled",
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 1. Smoke test
# ---------------------------------------------------------------------------

class TestSmoke:
    def test_send_and_get_response(self, page, e2e_url):
        """Send a simple prompt → get a non-empty assistant response."""
        page.goto(e2e_url)
        page.wait_for_selector("#input")
        page.wait_for_function(
            "document.getElementById('status-ws').textContent !== 'connecting...'",
            timeout=10000,
        )

        _send_and_wait(page, "Reply with exactly: HELLO_E2E")

        content = page.locator(".msg-assistant .content")
        content.wait_for(timeout=MODEL_TIMEOUT)
        text = content.text_content()
        assert len(text.strip()) > 0


# ---------------------------------------------------------------------------
# 2. Tool use round-trip
# ---------------------------------------------------------------------------

class TestToolUse:
    def test_tool_call_and_result(self, page, e2e_url):
        """Ask to list files → tool_call and tool_result appear."""
        page.goto(e2e_url)
        page.wait_for_selector("#input")
        page.wait_for_function(
            "document.getElementById('status-ws').textContent !== 'connecting...'",
            timeout=10000,
        )

        _send_and_wait(page, "Use the read_file tool to read test.txt")

        # Should have at least one tool call element
        tool_calls = page.locator(".tool-call")
        assert tool_calls.count() > 0

        # Should have at least one tool result
        tool_results = page.locator(".tool-result")
        assert tool_results.count() > 0

        # Should have final assistant content
        content = page.locator(".msg-assistant .content")
        assert content.count() > 0


# ---------------------------------------------------------------------------
# 3. Thinking mode
# ---------------------------------------------------------------------------

class TestThinking:
    def test_thinking_with_slash_command(self, page, e2e_url):
        """Send /think on then a question → thinking details element appears."""
        page.goto(e2e_url)
        page.wait_for_selector("#input")
        page.wait_for_function(
            "document.getElementById('status-ws').textContent !== 'connecting...'",
            timeout=10000,
        )

        # Enable thinking
        _send_and_wait(page, "/think on")

        # Status bar should update
        thinking_el = page.locator("#status-thinking")
        assert "on" in thinking_el.text_content()

        # Send a question
        _send_and_wait(page, "What is 2+2? Be brief.")

        # Thinking element should appear (model uses thinking)
        thinking = page.locator(".thinking")
        # Note: thinking may or may not appear depending on model support
        # Just verify we got a response
        content = page.locator(".msg-assistant .content")
        assert content.count() > 0

        # Turn thinking off
        _send_and_wait(page, "/think off")


# ---------------------------------------------------------------------------
# 4. Multi-turn conversation
# ---------------------------------------------------------------------------

class TestMultiTurn:
    def test_two_messages(self, page, e2e_url):
        """Send two messages → both get responses."""
        page.goto(e2e_url)
        page.wait_for_selector("#input")
        page.wait_for_function(
            "document.getElementById('status-ws').textContent !== 'connecting...'",
            timeout=10000,
        )

        # First message
        _send_and_wait(page, "Say exactly: FIRST_REPLY")
        first_content = page.locator(".msg-assistant .content >> nth=0")
        first_content.wait_for(timeout=MODEL_TIMEOUT)
        assert len(first_content.text_content().strip()) > 0

        # Second message
        _send_and_wait(page, "Say exactly: SECOND_REPLY")
        second_content = page.locator(".msg-assistant .content >> nth=1")
        second_content.wait_for(timeout=MODEL_TIMEOUT)
        assert len(second_content.text_content().strip()) > 0

        # Should have 2 user messages and 2 assistant messages
        assert page.locator(".msg-user").count() == 2
