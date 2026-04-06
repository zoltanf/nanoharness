"""Tests for nanoharness/web.py — FastAPI endpoints with mocked Agent."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanoharness.agent import StreamEvent
from nanoharness.config import Config


def _make_mock_agent(config: Config, events: list[StreamEvent] | None = None):
    """Create a mock Agent that yields given events from process_input."""
    agent = MagicMock()
    agent.config = config
    agent.tools = MagicMock()
    agent.tools.get_todo_summary = MagicMock(return_value=None)
    agent.commands = MagicMock()
    agent.commands.think_once_pending = False

    if events is None:
        events = [
            StreamEvent(type="content", text="Hello!"),
            StreamEvent(type="done"),
        ]

    async def mock_process_input(text: str):
        for ev in events:
            yield ev

    agent.process_input = mock_process_input
    return agent


@pytest.fixture
def app(config: Config):
    """Create a FastAPI test app with a mock agent."""
    from nanoharness.web import create_app
    agent = _make_mock_agent(config)
    return create_app(agent, open_browser=False)


@pytest.fixture
def client(app):
    """Create a test client for the FastAPI app."""
    from starlette.testclient import TestClient
    return TestClient(app)


class TestIndexPage:
    def test_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_contains_model_name(self, client):
        resp = client.get("/")
        assert "gemma4:26b" in resp.text

    def test_contains_status_bar(self, client):
        resp = client.get("/")
        assert "status" in resp.text
        assert "think:" in resp.text


class TestSSEEndpoint:
    def test_chat_returns_events(self, client):
        resp = client.post(
            "/api/chat",
            json={"text": "hello"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        # Parse SSE events
        events = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        assert any(e["type"] == "content" for e in events)
        assert any(e["type"] == "done" for e in events)

    def test_empty_input(self, client):
        resp = client.post("/api/chat", json={"text": ""})
        assert resp.status_code == 200
        events = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        assert any(e["type"] == "error" for e in events)


class TestWebSocket:
    def test_send_receive(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "input", "text": "hello"})
            events = []
            while True:
                data = ws.receive_json()
                events.append(data)
                if data["type"] == "done":
                    break
            assert any(e["type"] == "content" for e in events)
            assert events[-1]["type"] == "done"

    def test_empty_input_ignored(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "input", "text": ""})
            # Send a real message to verify the connection is still alive
            ws.send_json({"type": "input", "text": "hello"})
            events = []
            while True:
                data = ws.receive_json()
                events.append(data)
                if data["type"] == "done":
                    break
            # Should only see events from "hello", not from empty input
            assert any(e["type"] == "content" for e in events)
