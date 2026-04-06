"""Shared fixtures for NanoHarness tests."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoharness.config import Config


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace with test files and directories."""
    # Files
    (tmp_path / "hello.py").write_text("print('hello')\n")
    (tmp_path / "README.md").write_text("# Test\n")
    (tmp_path / "data.txt").write_text("some data\n")

    # Directories
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# main\n")
    (tmp_path / "tests_dir").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.txt").write_text("hidden\n")

    return tmp_path


@pytest.fixture
def config(workspace: Path) -> Config:
    """Return a Config with workspace set to the temp directory."""
    cfg = Config()
    cfg.workspace = workspace
    return cfg


@pytest.fixture
def unrestricted_config(workspace: Path) -> Config:
    """Return a Config with unrestricted safety."""
    cfg = Config()
    cfg.workspace = workspace
    cfg.safety.level = "unrestricted"
    return cfg


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return an AsyncMock of OllamaClient."""
    client = AsyncMock()
    client.check_health = AsyncMock(return_value=True)
    client.get_version = AsyncMock(return_value="0.20.2")
    client.list_models = AsyncMock(return_value=[{"name": "gemma4:26b"}])
    client.has_model = AsyncMock(return_value=(True, [{"name": "gemma4:26b"}]))
    client.close = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Playwright helpers: mock agent + live server on a free port
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_mock_agent(
    config: Config,
    events: list | None = None,
) -> MagicMock:
    """Create a mock Agent that yields canned StreamEvents from process_input."""
    from nanoharness.agent import StreamEvent

    agent = MagicMock()
    agent.config = config
    agent.tools = MagicMock()
    agent.tools.get_todo_summary = MagicMock(return_value=None)
    agent.commands = MagicMock()
    agent.commands.think_once_pending = False

    if events is None:
        events = [
            StreamEvent(type="content", text="Hello from mock!"),
            StreamEvent(type="done"),
        ]

    async def mock_process_input(text: str):
        for ev in events:
            yield ev

    agent.process_input = mock_process_input
    return agent


@pytest.fixture(scope="session")
def pw_server_url() -> Generator[str, None, None]:
    """Start a FastAPI server with mock agent in a background thread.

    Session-scoped so the server is started once for all playwright tests.
    """
    import asyncio
    import uvicorn
    from nanoharness.web import create_app

    port = _free_port()
    cfg = Config()
    cfg.workspace = Path("/tmp/nanoharness-test")
    cfg.workspace.mkdir(exist_ok=True)
    agent = make_mock_agent(cfg)
    app = create_app(agent, open_browser=False, port=port)

    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(uvi_config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
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
