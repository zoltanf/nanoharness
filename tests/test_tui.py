from __future__ import annotations

from pathlib import Path

from textual.containers import VerticalScroll

from nanoharness.agent import Agent
from nanoharness.agent import StreamEvent
from nanoharness.tui import CompletingInput, NanoHarnessApp, WorkspaceModal


async def test_bare_workspace_opens_recent_workspace_modal(config, mock_client, workspace, monkeypatch):
    ws_a = workspace / "recent-a"
    ws_b = workspace / "recent-b"
    ws_a.mkdir()
    ws_b.mkdir()
    monkeypatch.setattr("nanoharness.tui.load_recent_workspaces", lambda: [str(ws_a), str(ws_b)])

    agent = Agent(config, mock_client)
    app = NanoHarnessApp(agent)

    async with app.run_test() as pilot:
        inp = app.query_one(CompletingInput)
        inp.load_text("/workspace")
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen_stack[-1], WorkspaceModal)
        modal = app.screen_stack[-1]
        assert modal._paths == [ws_a.resolve(), ws_b.resolve()]


async def test_workspace_modal_select_sets_workspace_and_history(config, mock_client, workspace, monkeypatch):
    ws_a = workspace / "recent-a"
    ws_b = workspace / "recent-b"
    ws_a.mkdir()
    ws_b.mkdir()
    monkeypatch.setattr("nanoharness.tui.load_recent_workspaces", lambda: [str(ws_a), str(ws_b)])

    saved: list[Path] = []
    monkeypatch.setattr(
        "nanoharness.tui.save_recent_workspace",
        lambda path: saved.append(Path(path).resolve()),
    )

    agent = Agent(config, mock_client)
    app = NanoHarnessApp(agent)

    async with app.run_test() as pilot:
        inp = app.query_one(CompletingInput)
        inp.load_text("/workspace")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()

        assert agent.config.workspace == ws_b.resolve()
        assert saved == [ws_b.resolve()]
        assert app._history._path == ws_b / ".nanoharness" / "history"
        assert inp._history is app._history
        assert not isinstance(app.screen_stack[-1], WorkspaceModal)


async def test_tool_result_scrolls_chat_to_bottom(config, mock_client):
    agent = Agent(config, mock_client)

    async def fake_process_input(_text: str):
        yield StreamEvent(
            type="tool_result",
            text="".join(f"line {i}\n" for i in range(200)),
            tool_name="bash",
            lines_shown=200,
            lines_total=200,
        )
        yield StreamEvent(type="done")

    agent.process_input = fake_process_input
    app = NanoHarnessApp(agent)

    async with app.run_test(size=(80, 10)) as pilot:
        inp = app.query_one(CompletingInput)
        inp.load_text("show tool output")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        chat_log = app.query_one("#chat-log", VerticalScroll)
        assert chat_log.max_scroll_y > 0
        assert chat_log.scroll_y == chat_log.max_scroll_y
