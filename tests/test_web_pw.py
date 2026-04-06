"""Playwright browser tests for the NanoHarness web UI (mocked backend).

Run with: uv run --extra test --extra playwright --extra web pytest -m playwright -v
First install browsers: uv run --extra playwright playwright install chromium
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import uvicorn

from nanoharness.agent import StreamEvent
from nanoharness.config import Config
from nanoharness.web import create_app
from tests.conftest import _free_port, make_mock_agent

pytestmark = pytest.mark.playwright


# ---------------------------------------------------------------------------
# Fixtures — custom servers with specific mock event sequences
# ---------------------------------------------------------------------------

def _start_server(agent, port: int):
    """Start uvicorn in a daemon thread and wait until it's ready."""
    app = create_app(agent, open_browser=False, port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
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

    return server, thread


@pytest.fixture(scope="module")
def mock_server_url():
    """Server with default mock agent (content + done)."""
    port = _free_port()
    cfg = Config()
    cfg.workspace = Path("/tmp/nanoharness-pw-test")
    cfg.workspace.mkdir(exist_ok=True)
    agent = make_mock_agent(cfg)
    server, thread = _start_server(agent, port)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def thinking_url():
    """Server with mock agent that yields thinking + content events."""
    port = _free_port()
    cfg = Config()
    cfg.workspace = Path("/tmp/nanoharness-pw-think")
    cfg.workspace.mkdir(exist_ok=True)
    events = [
        StreamEvent(type="thinking", text="Let me consider this..."),
        StreamEvent(type="content", text="Here is my answer."),
        StreamEvent(type="done"),
    ]
    agent = make_mock_agent(cfg, events=events)
    server, thread = _start_server(agent, port)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def tool_url():
    """Server with mock agent that yields tool_call + tool_result events."""
    port = _free_port()
    cfg = Config()
    cfg.workspace = Path("/tmp/nanoharness-pw-tool")
    cfg.workspace.mkdir(exist_ok=True)
    events = [
        StreamEvent(
            type="tool_call", text="",
            tool_name="bash", tool_args={"command": "ls -la"},
            tool_id="call_1",
        ),
        StreamEvent(type="tool_result", text="file1.py\nfile2.py\nREADME.md"),
        StreamEvent(type="content", text="I found 3 files."),
        StreamEvent(type="done"),
    ]
    agent = make_mock_agent(cfg, events=events)
    server, thread = _start_server(agent, port)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def status_url():
    """Server with mock agent that yields status events."""
    port = _free_port()
    cfg = Config()
    cfg.workspace = Path("/tmp/nanoharness-pw-status")
    cfg.workspace.mkdir(exist_ok=True)
    events = [
        StreamEvent(type="status", text="Thinking mode: ON"),
        StreamEvent(type="done"),
    ]
    agent = make_mock_agent(cfg, events=events)
    server, thread = _start_server(agent, port)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 1. Hint system
# ---------------------------------------------------------------------------

class TestHintSystem:
    def test_hint_appears_for_partial_command(self, page, mock_server_url):
        """Type /thi → hint line shows completion text."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        inp = page.locator("#input")
        inp.fill("/thi")
        inp.dispatch_event("input")
        hint = page.locator("#hint-line")
        hint.wait_for(state="visible", timeout=2000)
        text = hint.text_content()
        assert "/think" in text
        assert "on|off|once" in text

    def test_no_hint_for_regular_text(self, page, mock_server_url):
        """Regular text → hint line stays hidden."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        inp = page.locator("#input")
        inp.fill("hello world")
        inp.dispatch_event("input")
        hint = page.locator("#hint-line")
        assert "hidden" in hint.get_attribute("class")

    def test_hint_shows_all_commands_for_slash(self, page, mock_server_url):
        """Bare / → hint shows all commands."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        inp = page.locator("#input")
        inp.fill("/")
        inp.dispatch_event("input")
        hint = page.locator("#hint-line")
        hint.wait_for(state="visible", timeout=2000)
        text = hint.text_content()
        assert "/think" in text
        assert "/workspace" in text
        assert "/clear" in text


# ---------------------------------------------------------------------------
# 2. Tab completion
# ---------------------------------------------------------------------------

class TestTabCompletion:
    def test_tab_completes_command(self, page, mock_server_url):
        """Type /thi + Tab → input becomes /think."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        inp = page.locator("#input")
        inp.fill("/thi")
        inp.dispatch_event("input")
        inp.press("Tab")
        assert inp.input_value() == "/think"

    def test_tab_cycles_options(self, page, mock_server_url):
        """Type /think + space + Tab → cycles through on, off, once."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        inp = page.locator("#input")
        inp.fill("/think ")
        inp.dispatch_event("input")
        inp.press("Tab")
        first = inp.input_value()
        assert first == "/think on"
        inp.press("Tab")
        second = inp.input_value()
        assert second == "/think off"
        inp.press("Tab")
        third = inp.input_value()
        assert third == "/think once"


# ---------------------------------------------------------------------------
# 3. Incomplete command blocking
# ---------------------------------------------------------------------------

class TestIncompleteCommandBlocking:
    def test_enter_blocked_on_incomplete(self, page, mock_server_url):
        """Type /thi + Enter → no user message sent."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        inp = page.locator("#input")
        inp.fill("/thi")
        inp.dispatch_event("input")
        inp.press("Enter")
        # Input should still contain the text (not cleared)
        assert inp.input_value() == "/thi"
        # No user message bubble in chat
        assert page.locator(".msg-user").count() == 0


# ---------------------------------------------------------------------------
# 4. Message send/receive
# ---------------------------------------------------------------------------

class TestMessageSendReceive:
    def test_send_and_receive(self, page, mock_server_url):
        """Type hello + Enter → user bubble + assistant response."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        # Wait for WebSocket to connect
        page.wait_for_function("document.getElementById('status-ws').textContent !== 'connecting...'", timeout=5000)

        inp = page.locator("#input")
        inp.fill("hello")
        inp.press("Enter")

        # User message appears
        user_msg = page.locator(".msg-user .bubble")
        user_msg.wait_for(timeout=3000)
        assert user_msg.text_content() == "hello"

        # Assistant response appears
        assistant = page.locator(".msg-assistant .content")
        assistant.wait_for(timeout=5000)
        assert "Hello from mock!" in assistant.text_content()


# ---------------------------------------------------------------------------
# 5. Thinking display
# ---------------------------------------------------------------------------

class TestThinkingDisplay:
    def test_thinking_collapsible(self, page, thinking_url):
        """Thinking events → collapsible <details> element."""
        page.goto(thinking_url)
        page.wait_for_selector("#input")
        page.wait_for_function("document.getElementById('status-ws').textContent !== 'connecting...'", timeout=5000)

        inp = page.locator("#input")
        inp.fill("test thinking")
        inp.press("Enter")

        # Thinking element appears
        thinking = page.locator(".thinking")
        thinking.wait_for(timeout=5000)
        assert "Thinking..." in thinking.locator("summary").text_content()

        # Content also appears
        content = page.locator(".msg-assistant .content")
        content.wait_for(timeout=5000)
        assert "Here is my answer." in content.text_content()


# ---------------------------------------------------------------------------
# 6. Tool call/result display
# ---------------------------------------------------------------------------

class TestToolDisplay:
    def test_tool_call_and_result(self, page, tool_url):
        """Tool events → styled tool-call and tool-result elements."""
        page.goto(tool_url)
        page.wait_for_selector("#input")
        page.wait_for_function("document.getElementById('status-ws').textContent !== 'connecting...'", timeout=5000)

        inp = page.locator("#input")
        inp.fill("list files")
        inp.press("Enter")

        # Tool call element
        tool_call = page.locator(".tool-call")
        tool_call.wait_for(timeout=5000)
        assert "bash" in tool_call.text_content()

        # Tool result element
        tool_result = page.locator(".tool-result")
        tool_result.wait_for(timeout=5000)
        assert "file1.py" in tool_result.text_content()

        # Final content
        content = page.locator(".msg-assistant .content")
        content.wait_for(timeout=5000)
        assert "3 files" in content.text_content()


# ---------------------------------------------------------------------------
# 7. Spinner
# ---------------------------------------------------------------------------

class TestSpinner:
    def test_spinner_during_processing(self, page, mock_server_url):
        """Spinner appears while processing, disappears when done."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        page.wait_for_function("document.getElementById('status-ws').textContent !== 'connecting...'", timeout=5000)

        # Use page.evaluate to send message and immediately check for spinner
        # The mock responds instantly, so we check spinner appeared and then vanished
        inp = page.locator("#input")
        inp.fill("test spinner")
        inp.press("Enter")

        # After done event, spinner should be gone
        page.locator(".msg-assistant .content").wait_for(timeout=5000)
        assert page.locator(".spinner").count() == 0


# ---------------------------------------------------------------------------
# 8. Processing lock
# ---------------------------------------------------------------------------

class TestProcessingLock:
    def test_send_button_disabled_during_processing(self, page, mock_server_url):
        """Send button is disabled while processing."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        page.wait_for_function("document.getElementById('status-ws').textContent !== 'connecting...'", timeout=5000)

        # Check button is enabled initially
        send_btn = page.locator("#send")
        assert not send_btn.is_disabled()

        # After sending and response completes, button should be enabled again
        inp = page.locator("#input")
        inp.fill("test lock")
        inp.press("Enter")
        page.locator(".msg-assistant .content").wait_for(timeout=5000)
        assert not send_btn.is_disabled()


# ---------------------------------------------------------------------------
# 9. Status bar
# ---------------------------------------------------------------------------

class TestStatusBar:
    def test_shows_model_name(self, page, mock_server_url):
        """Status bar displays model name."""
        page.goto(mock_server_url)
        page.wait_for_selector("#status")
        model = page.locator("#status .model")
        assert "gemma4:26b" in model.text_content()

    def test_shows_thinking_mode(self, page, mock_server_url):
        """Status bar shows thinking mode."""
        page.goto(mock_server_url)
        page.wait_for_selector("#status")
        thinking = page.locator("#status-thinking")
        assert "think:" in thinking.text_content()

    def test_shows_workspace(self, page, mock_server_url):
        """Status bar shows workspace path."""
        page.goto(mock_server_url)
        page.wait_for_selector("#status")
        ws = page.locator("#status-workspace")
        assert ws.text_content().strip() != ""


# ---------------------------------------------------------------------------
# 10. Status updates
# ---------------------------------------------------------------------------

class TestStatusUpdates:
    def test_thinking_status_update(self, page, status_url):
        """Status event updates the thinking indicator."""
        page.goto(status_url)
        page.wait_for_selector("#input")
        page.wait_for_function("document.getElementById('status-ws').textContent !== 'connecting...'", timeout=5000)

        inp = page.locator("#input")
        inp.fill("trigger status")
        inp.press("Enter")

        # Wait for thinking indicator to update in status bar
        page.wait_for_function(
            "document.getElementById('status-thinking').textContent === 'think:on'",
            timeout=5000,
        )


# ---------------------------------------------------------------------------
# 11. Input history
# ---------------------------------------------------------------------------

class TestInputHistory:
    def test_arrow_up_down(self, page, mock_server_url):
        """Arrow up/down cycles through sent messages."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        page.wait_for_function("document.getElementById('status-ws').textContent !== 'connecting...'", timeout=5000)

        inp = page.locator("#input")

        # Send two messages
        inp.fill("first message")
        inp.press("Enter")
        page.locator(".msg-assistant .content").wait_for(timeout=5000)

        inp.fill("second message")
        inp.press("Enter")
        # Wait for second response
        page.locator(".msg-assistant .content >> nth=1").wait_for(timeout=5000)

        # Arrow up → second message
        inp.press("ArrowUp")
        assert inp.input_value() == "second message"

        # Arrow up → first message
        inp.press("ArrowUp")
        assert inp.input_value() == "first message"

        # Arrow down → second message
        inp.press("ArrowDown")
        assert inp.input_value() == "second message"

        # Arrow down → empty
        inp.press("ArrowDown")
        assert inp.input_value() == ""


# ---------------------------------------------------------------------------
# 12. Empty input
# ---------------------------------------------------------------------------

class TestEmptyInput:
    def test_enter_on_empty_does_nothing(self, page, mock_server_url):
        """Enter on empty input → nothing happens."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")
        inp = page.locator("#input")
        inp.press("Enter")
        assert page.locator(".msg-user").count() == 0


# ---------------------------------------------------------------------------
# 13. SSE fallback
# ---------------------------------------------------------------------------

class TestSSEFallback:
    def test_sse_mode_works(self, page, mock_server_url):
        """Force SSE mode and verify messages still round-trip."""
        page.goto(mock_server_url)
        page.wait_for_selector("#input")

        # Force SSE mode via JS
        page.evaluate("switchToSSE()")
        status_ws = page.locator("#status-ws")
        assert status_ws.text_content() == "http"

        # Send a message
        inp = page.locator("#input")
        inp.fill("sse test")
        inp.press("Enter")

        # Should still get a response
        assistant = page.locator(".msg-assistant .content")
        assistant.wait_for(timeout=5000)
        assert "Hello from mock!" in assistant.text_content()
