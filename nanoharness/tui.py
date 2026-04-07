"""Textual TUI for NanoHarness."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.worker import Worker
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.timer import Timer
from textual.message import Message
from textual.widgets import Static, TextArea
from rich.text import Text
from rich.markup import escape
from rich.markdown import Markdown

from .completion import complete_line, hint_for_input, is_incomplete_command
from . import logging as dbg, BANNER as _BANNER

if TYPE_CHECKING:
    from .agent import Agent

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class HintLine(Static):
    """Single-line widget showing inline command hints as user types."""

    def __init__(self) -> None:
        super().__init__("")
        self.display = False

    def set_hint(self, text: str) -> None:
        if text:
            self.update(text)
            self.display = True
        else:
            self.update("")
            self.display = False


class CompletingInput(TextArea):
    """Multi-line input widget with tab completion for file paths and commands.

    Submit with Enter. Ctrl+J inserts a newline (terminals can't distinguish Shift/Alt+Enter).
    """

    BINDINGS = [
        Binding("pageup",   "scroll_chat_up",   show=False),
        Binding("pagedown", "scroll_chat_down", show=False),
        Binding("home",     "scroll_chat_home", show=False),
        Binding("end",      "scroll_chat_end",  show=False),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, agent: Agent, **kwargs: object) -> None:
        super().__init__(show_line_numbers=False, soft_wrap=True, **kwargs)
        self._agent = agent
        self._tab_matches: list[str] = []
        self._tab_index: int = -1
        self._tab_prefix: str = ""

    def _on_key(self, event: Key) -> None:
        if event.key == "up":
            row = self.cursor_location[0]
            if row == 0:
                event.prevent_default()
                event.stop()
                self.app.query_one("#chat-log", VerticalScroll).scroll_up(animate=False)
                return
        if event.key == "down":
            lines = self.text.split("\n")
            row = self.cursor_location[0]
            if row >= len(lines) - 1:
                event.prevent_default()
                event.stop()
                self.app.query_one("#chat-log", VerticalScroll).scroll_down(animate=False)
                return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            # While the model is responding, keep text intact so the user can type ahead
            if getattr(self.app, "_processing", False):
                return
            text = self.text
            # Belt-and-suspenders: read_only=True makes TextArea._on_key return early,
            # preventing a stray newline even if prevent_default() is somehow bypassed.
            # Restored immediately in on_completing_input_submitted before any await.
            self.read_only = True
            self.post_message(self.Submitted(text))
            return
        if event.key == "ctrl+j":  # Ctrl+J (LF) = newline; Shift/Alt+Enter can't be distinguished from Enter in terminals
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "tab":
            event.prevent_default()
            event.stop()
            self._do_tab_complete()
            return
        self._tab_matches = []
        self._tab_index = -1
        self._tab_prefix = ""

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Update the hint line as the user types."""
        try:
            hint_line = self.app.query_one(HintLine)
        except Exception:
            return
        row = self.cursor_location[0]
        lines = self.text.split("\n")
        current_line = lines[row] if row < len(lines) else ""
        hint_line.set_hint(hint_for_input(current_line))

    def _do_tab_complete(self) -> None:
        row, _col = self.cursor_location
        lines = self.text.split("\n")
        current_line = lines[row] if row < len(lines) else ""

        if self._tab_matches and self._tab_index >= 0:
            self._tab_index = (self._tab_index + 1) % len(self._tab_matches)
            new_line = self._tab_prefix + self._tab_matches[self._tab_index]
            self.replace(new_line, (row, 0), (row, len(current_line)))
            self.cursor_location = (row, len(new_line))
            return

        matches = complete_line(self._agent.config.workspace, current_line)
        if not matches:
            return

        # Determine the prefix to preserve (text before the token being completed).
        # For full-line commands at start (/workspace <dir>, /think <opt>), complete_line
        # already returns full-line replacements — no prefix needed.
        # For all other cases, preserve everything before the last whitespace-separated token.
        stripped = current_line.lstrip()
        if stripped.lower().startswith("/workspace ") or stripped.lower().startswith("/think "):
            prefix = ""
        else:
            parts = stripped.rsplit(None, 1)
            last_token = parts[-1] if parts else ""
            prefix = current_line[: len(current_line) - len(last_token)]

        self._tab_prefix = prefix
        self._tab_matches = matches
        self._tab_index = 0
        new_line = prefix + matches[0]
        self.replace(new_line, (row, 0), (row, len(current_line)))
        self.cursor_location = (row, len(new_line))

    def action_scroll_chat_up(self) -> None:
        self.app.query_one("#chat-log", VerticalScroll).scroll_page_up(animate=False)

    def action_scroll_chat_down(self) -> None:
        self.app.query_one("#chat-log", VerticalScroll).scroll_page_down(animate=False)

    def action_scroll_chat_home(self) -> None:
        self.app.query_one("#chat-log", VerticalScroll).scroll_home(animate=False)

    def action_scroll_chat_end(self) -> None:
        self.app.query_one("#chat-log", VerticalScroll).scroll_end(animate=False)


class SpinnerLine(Static):
    """Animated spinner shown while waiting for model response."""

    def __init__(self) -> None:
        super().__init__()
        self._frame = 0
        self._label = "Thinking"
        self._timer: Timer | None = None

    def start(self, label: str = "Thinking") -> None:
        self._label = label
        self._frame = 0
        self._update_display()
        self.display = True
        if self._timer is None:
            self._timer = self.set_interval(0.08, self._tick)

    def stop(self) -> None:
        self.display = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
        self._update_display()

    def _update_display(self) -> None:
        frame = SPINNER_FRAMES[self._frame]
        self.update(f"{frame} {self._label}...")


class StatusBar(Static):
    """Bottom status bar showing model info."""

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self.agent = agent
        self._update_text()

    # Column widths for the 4-column layout
    _COL = (16, 10, 10)  # model, context, think — workspace fills the rest

    def _update_text(self) -> None:
        import os
        cfg = self.agent.config

        # --- model ---
        model_val = cfg.model.name

        # --- context ---
        used = self.agent.last_prompt_tokens
        ctx_max = self.agent.context_size
        used_str = f"{used // 1000}k" if used >= 1000 else str(used)
        max_str = f"{ctx_max // 1000}k" if ctx_max else "?"
        ctx_val = f"{used_str} / {max_str}"
        ctx_style = "bold dark_red" if (ctx_max and used / ctx_max > 0.70) else "bold"

        # --- think ---
        if self.agent.commands.think_once_pending:
            think_val, think_style = "once", "yellow"
        elif cfg.model.thinking:
            think_val, think_style = "on", "green"
        else:
            think_val, think_style = "off", "dim"

        # --- workspace (shorten ~) ---
        ws = str(cfg.workspace)
        home = os.path.expanduser("~")
        if ws.startswith(home):
            ws = "~" + ws[len(home):]

        # --- todo (own column) ---
        next_task, progress = self.agent.tools.get_todo_parts()

        w0, w1, w2 = self._COL
        sep = " │ "

        # Workspace column width: wide enough for both the path and the safety label
        safety_label = f"safety: {cfg.safety.level}"
        w3 = max(len(ws), len(safety_label))

        # Row 1 — values (all bold, uniform colour)
        todo_val = f"Next: {next_task}" if next_task else ""
        val_row = (
            f" [bold]{model_val:<{w0}}[/]{sep}"
            f"[{ctx_style}]{ctx_val:^{w1}}[/]{sep}"
            f"[bold]{think_val:^{w2}}[/]{sep}"
            f"[bold]{ws:<{w3}}[/]"
            + (f"{sep}[bold]{todo_val}[/]" if progress else "")
        )

        # Row 2 — labels (same separators as row 1)
        lbl_row = (
            f" [dim]{'model':<{w0}}[/]{sep}"
            f"[dim]{'context':^{w1}}[/]{sep}"
            f"[dim]{'think':^{w2}}[/]{sep}"
            f"[dim]{safety_label:<{w3}}[/]"
            + (f"{sep}[dim]{progress}[/]" if progress else "")
        )

        self.update(f"{val_row}\n{lbl_row}")

    def refresh_status(self) -> None:
        self._update_text()


class NanoHarnessApp(App):
    CSS = """
    #chat-log {
        height: 1fr;
        padding: 0 1;
        scrollbar-gutter: stable;
    }
    #chat-log > .chat-msg {
        width: 100%;
        height: auto;
    }
    SpinnerLine {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    StatusBar {
        height: 2;
        background: $accent;
        color: $text;
        padding: 0 0;
    }
    HintLine {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: #0d2137;
    }
    CompletingInput {
        height: auto;
        min-height: 1;
        max-height: 8;
        border: none;
        padding: 1 1;
        background: #0d2137;
        color: white;
    }
    CompletingInput > .text-area--cursor-line {
        background: #0d2137;
    }
    #bottom-panel {
        dock: bottom;
        height: auto;
        background: #0d2137;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("escape", "interrupt", "Interrupt", show=True),
    ]

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self.agent = agent
        self.title = "NanoHarness"
        self._processing = False
        self._agent_worker: Worker | None = None

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat-log")
        with Vertical(id="bottom-panel"):
            yield SpinnerLine()
            yield HintLine()
            yield CompletingInput(agent=self.agent)
            yield StatusBar(self.agent)

    def _append_chat(self, content, *, markup: bool = False) -> Static:
        """Mount a new Static widget in the chat scroll and return it."""
        chat = self.query_one("#chat-log", VerticalScroll)
        widget = Static(content, markup=markup, classes="chat-msg")
        chat.mount(widget)
        widget.scroll_visible()
        return widget

    def _scroll_chat(self) -> None:
        chat = self.query_one("#chat-log", VerticalScroll)
        chat.scroll_end(animate=False)

    def _show_welcome(self) -> None:
        cfg = self.agent.config
        self._append_chat(Text.from_markup(f"[bold green]{_BANNER}[/]\n"))
        self._append_chat(
            Text.from_markup(
                f"[dim]v0.1.0 — {cfg.model.name} — {cfg.workspace}[/]\n"
                f"[dim]Type /help for commands[/]"
            )
        )
        if cfg.debug:
            self._append_chat(Text.from_markup("[dim]Debug logging: ON[/]"))

    def on_mount(self) -> None:
        self._show_welcome()
        self.query_one(SpinnerLine).display = False
        # Disable focus on the chat scroll so clicks don't steal focus from input
        chat_log = self.query_one("#chat-log", VerticalScroll)
        chat_log.can_focus = False
        self.query_one(CompletingInput).focus()

    def on_focus(self, event: object) -> None:
        """Always redirect focus back to the input box."""
        inp = self.query_one(CompletingInput)
        if self.focused is not inp:
            self.set_timer(0, inp.focus)

    async def on_completing_input_submitted(self, event: CompletingInput.Submitted) -> None:
        user_input = event.value.strip()

        inp = self.query_one(CompletingInput)
        inp.read_only = False  # restore immediately

        if not user_input or is_incomplete_command(user_input):
            return

        inp.load_text("")
        self.query_one(HintLine).set_hint("")

        self._processing = True
        dbg.log_event("tui_input", user_input)
        self._append_chat(Text.from_markup(f"\n[bold cyan]>>> {escape(user_input)}[/]"))

        spinner = self.query_one(SpinnerLine)
        spinner.start("Thinking" if self.agent.config.model.thinking else "Processing")

        # Run streaming in a Worker (separate asyncio Task) so the App's message
        # pump is freed immediately — this lets Textual process key events and
        # repaint the input widget while the model responds.
        self._agent_worker = self.run_worker(
            self._stream_agent_response(user_input), exclusive=True, name="agent"
        )

    async def _stream_agent_response(self, user_input: str) -> None:
        inp = self.query_one(CompletingInput)
        spinner = self.query_one(SpinnerLine)
        status_bar = self.query_one(StatusBar)

        thinking_buffer = ""
        content_buffer = ""
        thinking_widget: Static | None = None
        streaming_widget: Static | None = None
        got_first_output = False

        def _finalize_widgets() -> None:
            nonlocal thinking_buffer, content_buffer, thinking_widget, streaming_widget
            if content_buffer and streaming_widget:
                try:
                    streaming_widget.update(Markdown(content_buffer))
                except Exception:
                    pass
                content_buffer = ""
                streaming_widget = None
            if thinking_buffer and not thinking_widget:
                self._append_chat(Text(thinking_buffer.strip(), style="dim italic"))
            thinking_buffer = ""
            thinking_widget = None

        try:
            async for ev in self.agent.process_input(user_input):
                match ev.type:
                    case "content":
                        if not got_first_output:
                            got_first_output = True
                            spinner.stop()
                        content_buffer += ev.text
                        if streaming_widget is None:
                            streaming_widget = self._append_chat(content_buffer)
                        else:
                            streaming_widget.update(content_buffer)
                            streaming_widget.scroll_visible()

                    case "thinking":
                        if not got_first_output:
                            got_first_output = True
                            spinner.stop()
                        thinking_buffer += ev.text
                        if thinking_widget is None:
                            thinking_widget = self._append_chat(
                                Text(thinking_buffer, style="dim italic")
                            )
                        else:
                            thinking_widget.update(
                                Text(thinking_buffer, style="dim italic")
                            )
                            thinking_widget.scroll_visible()

                    case "tool_call":
                        if not got_first_output:
                            got_first_output = True
                        spinner.stop()
                        _finalize_widgets()
                        args_str = ", ".join(
                            f"{k}={repr(v)[:80]}" for k, v in ev.tool_args.items()
                        )
                        self._append_chat(Text.from_markup(
                            f"[bold yellow]> {escape(ev.tool_name)}[/]"
                            f"([dim]{escape(args_str)}[/])"
                        ))
                        spinner.start(f"Running {ev.tool_name}")

                    case "tool_result":
                        spinner.stop()
                        preview = ev.text[:500]
                        if len(ev.text) > 500:
                            preview += "..."
                        self._append_chat(Text.from_markup(
                            f"[dim green]{escape(preview)}[/]"
                        ))
                        spinner.start("Thinking" if self.agent.config.model.thinking else "Processing")

                    case "markup":
                        spinner.stop()
                        _finalize_widgets()
                        self._append_chat(ev.text, markup=True)

                    case "status":
                        _finalize_widgets()
                        if ev.text == "Conversation cleared.":
                            chat_log = self.query_one("#chat-log", VerticalScroll)
                            chat_log.remove_children()
                            self._show_welcome()
                        else:
                            self._append_chat(Text.from_markup(f"[bold]{escape(ev.text)}[/]"))
                        status_bar.refresh_status()

                    case "error":
                        spinner.stop()
                        _finalize_widgets()
                        self._append_chat(Text.from_markup(f"[bold red]{escape(ev.text)}[/]"))

                    case "done":
                        spinner.stop()
                        _finalize_widgets()
                        if ev.text == "quit":
                            self.exit()
                            return

                self._scroll_chat()

        except asyncio.CancelledError:
            spinner.stop()
            _finalize_widgets()
            self._append_chat(Text.from_markup("[dim]Interrupted.[/]"))

        except Exception as e:
            spinner.stop()
            dbg.log_error("tui_process_input", e)
            self._append_chat(Text.from_markup(f"[bold red]Error: {escape(str(e))}[/]"))

        finally:
            spinner.stop()
            _finalize_widgets()
            self._processing = False
            self._agent_worker = None
            inp.read_only = False
            status_bar.refresh_status()
            dbg.log_event("tui_turn_complete", "input processing finished")
            inp.focus()

    def action_interrupt(self) -> None:
        """Cancel the running agent worker on Escape."""
        if self._processing and self._agent_worker is not None:
            self._agent_worker.cancel()
            self._agent_worker = None


async def run_tui(agent: Agent) -> int:
    app = NanoHarnessApp(agent)
    await app.run_async()
    return 0
