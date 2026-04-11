"""Textual TUI for NanoHarness."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.worker import Worker
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.message import Message
from textual.widgets import Static, TextArea, Markdown as MarkdownWidget
from rich.text import Text
from rich.markup import escape

from .completion import complete_line, hint_for_input, is_incomplete_command, command_send_error
from .tools import format_confirm_preview, _count_lines
from . import logging as dbg, BANNER as _BANNER, __version__
from .config import WARN_SAFETY_NONE, WARN_DEBUG_ON, WARN_FLASH_ATTENTION, TOOL_NAMES, write_config_toml, flash_attention_enabled
from .history import InputHistory

if TYPE_CHECKING:
    from .agent import Agent

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_UI_PREVIEW_LIMIT = 500


def _ui_clip_notice(ui_shown: int, ui_clipped: bool, model_shown: int, lines_total: int) -> str:
    """Build the notice appended to tool output. Returns '' when no line metadata available."""
    if model_shown == 0 and lines_total == 0:
        return ""  # char-clipped or no info
    if model_shown > 0 and lines_total > 0 and model_shown < lines_total:
        # Backend truncated — its notice is already embedded in the text.
        # Only add UI notice when UI also clipped (to report the extra cut).
        return f"[{ui_shown} lines shown · model: {model_shown}/{lines_total}]" if ui_clipped else ""
    # Model saw all lines (no backend truncation), or read_file (lines_total == 0).
    n = lines_total if lines_total > 0 else model_shown
    return f"[{ui_shown}/{n} lines shown · model: all]" if ui_clipped else f"[{n} lines · all]"


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

    def __init__(self, agent: Agent, history: InputHistory | None = None, **kwargs: object) -> None:
        super().__init__(show_line_numbers=False, soft_wrap=True, **kwargs)
        self._agent = agent
        self._history = history
        self._tab_matches: list[str] = []
        self._tab_index: int = -1
        self._tab_prefix: str = ""

    def _on_key(self, event: Key) -> None:
        if event.key == "up":
            row = self.cursor_location[0]
            if row == 0:
                event.prevent_default()
                event.stop()
                if self._history is not None:
                    result = self._history.navigate_up(self.text)
                    if result is not None:
                        self.load_text(result)
                        self.cursor_location = (0, 0)
                        self._reset_tab_state()
                return
        if event.key == "down":
            lines = self.text.split("\n")
            row = self.cursor_location[0]
            if row >= len(lines) - 1:
                event.prevent_default()
                event.stop()
                if self._history is not None:
                    result = self._history.navigate_down()
                    if result is not None:
                        self.load_text(result)
                        new_lines = result.split("\n")
                        last_row = len(new_lines) - 1
                        self.cursor_location = (last_row, len(new_lines[last_row]))
                        self._reset_tab_state()
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
        if event.key == "shift+tab":
            event.prevent_default()
            event.stop()
            self._do_tab_complete(reverse=True)
            return
        self._reset_tab_state()

    def _reset_tab_state(self) -> None:
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

    def _do_tab_complete(self, reverse: bool = False) -> None:
        row, _col = self.cursor_location
        lines = self.text.split("\n")
        current_line = lines[row] if row < len(lines) else ""

        if self._tab_matches and self._tab_index >= 0:
            step = -1 if reverse else 1
            self._tab_index = (self._tab_index + step) % len(self._tab_matches)
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
        if (stripped.lower().startswith("/workspace ")
                or stripped.lower().startswith("/think ")
                or stripped.lower().startswith("/update ")
                or stripped.lower().startswith("/config ")
                or stripped.lower().startswith("/info ")):
            # Full-line replacements: no prefix needed.
            prefix = ""
        elif (not stripped.startswith("/")
              and matches
              and all(m.startswith("/think") for m in matches)):
            # Embedded /think after regular text: preserve everything up to
            # the "/" that started the command token.
            slash_pos = current_line.rfind("/")
            prefix = current_line[:slash_pos] if slash_pos >= 0 else ""
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
        self.update(f"{frame} {self._label}... [dim](press ESC to interrupt)[/dim]")


class StatusBar(Static):
    """Bottom status bar showing model info."""

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self.agent = agent
        self._net = ""  # "↑" sending, "↓" receiving, "" idle
        self._update_text()

    def set_net(self, indicator: str) -> None:
        """Update the network activity indicator and redraw."""
        self._net = indicator
        self._update_text()

    def _update_text(self) -> None:
        import os
        cfg = self.agent.config

        # --- model ---
        model_val = cfg.model.name
        if self._net == "↑":
            net_markup = "[red]↑[/]"
        elif self._net == "↓":
            net_markup = "[green]↓[/]"
        else:
            net_markup = " "

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

        sep = " │ "

        # Dynamic column widths: wide enough for both the value and its label.
        # w0: model column — (model_val + space + net indicator) vs "model" label
        w0 = max(len(model_val) + 2, len("model"))
        w1 = max(len(ctx_val), len("context"))
        w2 = max(len(think_val), len("think"))

        # Workspace column width: wide enough for both the path and the safety label
        safety_label = f"safety: {cfg.safety.level}"
        w3 = max(len(ws), len(safety_label))

        # Row 1 — values (all bold, uniform colour)
        # model column: name padded to (w0-2), then a space and the 1-char net indicator
        todo_val = f"Next: {next_task}" if next_task else ""
        val_row = (
            f" [bold]{model_val:<{w0 - 2}}[/] {net_markup}{sep}"
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


class ConfirmModal(ModalScreen):
    """Modal dialog for confirm-mode tool call approval."""

    CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-box {
        width: 70;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("enter", "allow", show=False),
        Binding("escape", "deny", show=False),
        Binding("n", "deny", show=False),
    ]

    def __init__(self, preview: str, future: "asyncio.Future[bool]") -> None:
        super().__init__()
        self._preview = preview
        self._future = future

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(
                Text.from_markup(
                    f"[bold yellow]Allow tool call?[/]\n\n"
                    f"[dim]{escape(self._preview)}[/]\n\n"
                    f"  [bold][Enter][/] Allow   [bold][Esc / n][/] Deny"
                )
            )

    def action_allow(self) -> None:
        try:
            self._future.set_result(True)
        except asyncio.InvalidStateError:
            pass
        self.app.pop_screen()

    def action_deny(self) -> None:
        try:
            self._future.set_result(False)
        except asyncio.InvalidStateError:
            pass
        self.app.pop_screen()


class ToolsModal(ModalScreen):
    """Interactive per-tool enable/disable editor."""

    CSS = """
    ToolsModal {
        align: center middle;
    }
    #tools-box {
        width: 56;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("up",    "move_up",    show=False),
        Binding("down",  "move_down",  show=False),
        Binding("left",  "move_left",  show=False),
        Binding("right", "move_right", show=False),
        Binding("space", "toggle",     show=False),
        Binding("escape","close",      show=False),
    ]

    def __init__(self, agent: "Agent") -> None:
        super().__init__()
        self._agent = agent
        states = agent.tools.get_tool_states(agent.config.tools)
        self._global: dict[str, bool] = {n: s["global"] for n, s in states.items()}
        self._workspace: dict[str, bool | None] = {n: s["workspace"] for n, s in states.items()}
        self._row: int = 0
        self._col: int = 0  # 0 = global, 1 = workspace

    def compose(self) -> ComposeResult:
        with Vertical(id="tools-box"):
            yield Static(id="tools-content")

    def on_mount(self) -> None:
        self._update_display()

    def _update_display(self) -> None:
        lines: list[str] = [
            "[bold cyan]Tool Configuration[/]",
            "",
            f"  [dim]{'Global':<8}  {'Workspace':<12}  Tool[/]",
            f"  [dim]{'──────':<8}  {'─────────':<12}  ────────────────[/]",
        ]
        for i, name in enumerate(TOOL_NAMES):
            g_val = self._global[name]
            w_val = self._workspace[name]

            g_str = "[green]✓[/]" if g_val else "[red]✗[/]"
            if w_val is None:
                w_str = "[dim]–[/]"
            elif w_val:
                w_str = "[green]✓[/]"
            else:
                w_str = "[red]✗[/]"

            if i == self._row:
                g_disp = f"[reverse]{g_str}[/]" if self._col == 0 else g_str
                w_disp = f"[reverse]{w_str}[/]" if self._col == 1 else w_str
                name_disp = f"[bold]{escape(name)}[/]"
            else:
                g_disp = g_str
                w_disp = w_str
                name_disp = escape(name)

            lines.append(f"  {g_disp}         {w_disp}             {name_disp}")

        lines += [
            "",
            f"  [green]✓[/] on   [red]✗[/] off   [dim]–[/] inherit global [dim](workspace only)[/]",
            "",
            "  [dim]↑↓ row  ←→ col  Space toggle  Esc save & close[/]",
        ]
        self.query_one("#tools-content", Static).update(Text.from_markup("\n".join(lines)))

    def action_move_up(self) -> None:
        self._row = (self._row - 1) % len(TOOL_NAMES)
        self._update_display()

    def action_move_down(self) -> None:
        self._row = (self._row + 1) % len(TOOL_NAMES)
        self._update_display()

    def action_move_left(self) -> None:
        self._col = 0
        self._update_display()

    def action_move_right(self) -> None:
        self._col = 1
        self._update_display()

    def action_toggle(self) -> None:
        name = TOOL_NAMES[self._row]
        if self._col == 0:
            self._global[name] = not self._global[name]
        else:
            cur = self._workspace[name]
            if cur is None:
                self._workspace[name] = True
            elif cur:
                self._workspace[name] = False
            else:
                self._workspace[name] = None
        self._update_display()

    def action_close(self) -> None:
        cfg = self._agent.config.tools
        for name in TOOL_NAMES:
            setattr(cfg, name, self._global[name])
        write_config_toml(self._agent.config)
        self._agent.tools.set_workspace_tools(self._workspace)
        self.app.pop_screen()


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
    Markdown.chat-msg {
        padding: 0;
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
        background: #f0f4f8;
    }
    App.-dark-mode HintLine {
        background: #0d2137;
    }
    CompletingInput {
        height: auto;
        min-height: 1;
        max-height: 8;
        border: none;
        padding: 1 1;
        background: #f0f4f8;
        color: $text;
    }
    App.-dark-mode CompletingInput {
        background: #0d2137;
        color: white;
    }
    CompletingInput > .text-area--cursor-line {
        background: #f0f4f8;
    }
    App.-dark-mode CompletingInput > .text-area--cursor-line {
        background: #0d2137;
    }
    #bottom-panel {
        dock: bottom;
        height: auto;
        background: #f0f4f8;
    }
    App.-dark-mode #bottom-panel {
        background: #0d2137;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("escape", "interrupt", "Interrupt", show=True),
    ]

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self.agent = agent
        self.title = "NanoHarness"
        self._processing = False
        self._agent_worker: Worker | None = None
        self._history = InputHistory(agent.config.workspace / ".nanoharness" / "history")

    def copy_to_clipboard(self, text: str) -> None:
        """Copy text to clipboard, using pbcopy on macOS for Terminal.app compatibility."""
        super().copy_to_clipboard(text)
        if sys.platform == "darwin":
            import subprocess
            try:
                subprocess.Popen(
                    ["pbcopy"], stdin=subprocess.PIPE,
                ).communicate(text.encode("utf-8"))
            except OSError:
                pass

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat-log")
        with Vertical(id="bottom-panel"):
            yield SpinnerLine()
            yield HintLine()
            yield CompletingInput(agent=self.agent, history=self._history)
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

    def _show_welcome(self, ollama_version: str = "") -> None:
        cfg = self.agent.config
        self._append_chat(Text.from_markup(f"[bold green]{_BANNER}[/]\n"))
        subtitle = f"[dim]v{__version__} — {cfg.model.name} — {cfg.workspace}[/]"
        if ollama_version:
            subtitle += f"\n[dim]Ollama {ollama_version} — {cfg.ollama.base_url}[/]"
        subtitle += "\n[dim]Type /help for commands[/]"
        self._append_chat(Text.from_markup(subtitle))
        if cfg.debug:
            self._append_chat(Text.from_markup(f"[yellow]{escape(WARN_DEBUG_ON)}[/]"))
        if cfg.safety.level == "none":
            self._append_chat(Text.from_markup(
                f"[bold red]WARNING:[/] [yellow]{escape(WARN_SAFETY_NONE[len('WARNING: '):])}[/]"
            ))
        if not flash_attention_enabled():
            self._append_chat(Text.from_markup(
                f"[bold yellow]WARNING:[/] [yellow]{escape(WARN_FLASH_ATTENTION[len('WARNING: '):])}[/]"
            ))

    async def on_mount(self) -> None:
        if self.agent.config.ui.theme == "light":
            self.theme = "textual-light"
        ollama_version = await self.agent.client.get_version()
        self._show_welcome(ollama_version)
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

        if not user_input:
            return
        if is_incomplete_command(user_input):
            err = command_send_error(user_input)
            if err:
                self.query_one(HintLine).set_hint(err)
            return

        # Intercept /config tools (bare) to show interactive modal
        if user_input.lower() == "/config tools":
            self._history.add(user_input)
            inp.load_text("")
            self.query_one(HintLine).set_hint("")
            self.push_screen(ToolsModal(self.agent))
            return

        self._history.add(user_input)
        inp.load_text("")
        self.query_one(HintLine).set_hint("")

        self._processing = True
        dbg.log_event("tui_input", user_input)
        self._append_chat(Text.from_markup(f"\n[bold cyan]>>> {escape(user_input)}[/]"))

        spinner = self.query_one(SpinnerLine)
        spinner.start("Thinking" if self.agent.config.model.thinking else "Processing")
        self.query_one(StatusBar).set_net("↑")

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
        streaming_widget: MarkdownWidget | None = None
        progress_widget: Static | None = None
        got_first_output = False
        last_md_len = 0
        chat_log = self.query_one("#chat-log", VerticalScroll)

        async def _finalize_widgets() -> None:
            nonlocal thinking_buffer, content_buffer, thinking_widget, streaming_widget, progress_widget, last_md_len
            if streaming_widget is not None and content_buffer:
                await streaming_widget.update(content_buffer)
            progress_widget = None  # reset reference; last line remains visible in chat
            content_buffer = ""
            streaming_widget = None
            last_md_len = 0
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
                            status_bar.set_net("↓")
                        content_buffer += ev.text
                        if streaming_widget is None:
                            streaming_widget = MarkdownWidget(content_buffer, classes="chat-msg")
                            await chat_log.mount(streaming_widget)
                            last_md_len = len(content_buffer)
                        elif len(content_buffer) - last_md_len >= 80:
                            await streaming_widget.update(content_buffer)
                            last_md_len = len(content_buffer)
                        streaming_widget.scroll_visible()

                    case "thinking":
                        if not got_first_output:
                            got_first_output = True
                            spinner.stop()
                            status_bar.set_net("↓")
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

                    case "progress":
                        if not got_first_output:
                            got_first_output = True
                            spinner.stop()
                            status_bar.set_net("↓")
                        if progress_widget is None:
                            progress_widget = self._append_chat(
                                Text(ev.text, style="dim")
                            )
                        else:
                            progress_widget.update(Text(ev.text, style="dim"))
                            progress_widget.scroll_visible()

                    case "tool_call":
                        if not got_first_output:
                            got_first_output = True
                        spinner.stop()
                        status_bar.set_net("")
                        await _finalize_widgets()
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
                        status_bar.set_net("↑")
                        got_first_output = False
                        raw = ev.text
                        if len(raw) <= _UI_PREVIEW_LIMIT:
                            ui_shown = _count_lines(raw)
                            notice = _ui_clip_notice(ui_shown, False, ev.lines_shown, ev.lines_total)
                            display = raw + (f"\n{notice}" if notice else "")
                        else:
                            cut = raw.rfind('\n', 0, _UI_PREVIEW_LIMIT)
                            if cut == -1:
                                cut = _UI_PREVIEW_LIMIT
                            preview = raw[:cut]
                            ui_shown = _count_lines(preview)
                            notice = _ui_clip_notice(ui_shown, True, ev.lines_shown, ev.lines_total)
                            display = preview + (f"\n{notice}" if notice else "")
                        self._append_chat(Text.from_markup(
                            f"[dim green]{escape(display)}[/]\n"
                        ))
                        spinner.start("Thinking" if self.agent.config.model.thinking else "Processing")

                    case "markdown":
                        spinner.stop()
                        await _finalize_widgets()
                        md_widget = MarkdownWidget(ev.text, classes="chat-msg")
                        await chat_log.mount(md_widget)
                        md_widget.scroll_visible()

                    case "theme":
                        new_theme = "textual-light" if self.agent.config.ui.theme == "light" else "textual-dark"
                        if self.theme != new_theme:
                            self.theme = new_theme

                    case "status":
                        await _finalize_widgets()
                        if ev.text == "Conversation cleared.":
                            chat_log.remove_children()
                            self._show_welcome()
                        else:
                            self._append_chat(Text.from_markup(f"[bold]{escape(ev.text)}[/]"))
                        status_bar.refresh_status()

                    case "error":
                        spinner.stop()
                        await _finalize_widgets()
                        self._append_chat(Text.from_markup(f"[bold red]{escape(ev.text)}[/]"))

                    case "done":
                        spinner.stop()
                        await _finalize_widgets()
                        if ev.text == "quit":
                            self.exit()
                            return

                self._scroll_chat()

        except asyncio.CancelledError:
            spinner.stop()
            await _finalize_widgets()
            self._append_chat(Text.from_markup("[dim]Interrupted.[/]"))

        except Exception as e:
            spinner.stop()
            dbg.log_error("tui_process_input", e)
            self._append_chat(Text.from_markup(f"[bold red]Error: {escape(str(e))}[/]"))

        finally:
            spinner.stop()
            await _finalize_widgets()
            self._processing = False
            self._agent_worker = None
            inp.read_only = False
            status_bar.set_net("")
            status_bar.refresh_status()
            dbg.log_event("tui_turn_complete", "input processing finished")
            inp.focus()

    def action_interrupt(self) -> None:
        """Cancel the running agent worker on Escape."""
        if self._processing and self._agent_worker is not None:
            self._agent_worker.cancel()
            self._agent_worker = None

    def _show_confirm_prompt(self, tool_name: str, args: dict, future: "asyncio.Future[bool]") -> None:
        """Push a ConfirmModal so the user can allow or deny a tool call."""
        self.push_screen(ConfirmModal(format_confirm_preview(tool_name, args), future))


async def run_tui(agent: Agent) -> int:
    app = NanoHarnessApp(agent)

    async def tui_confirm(tool_name: str, args: dict) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        app.call_later(app._show_confirm_prompt, tool_name, args, future)
        return await future

    agent.tools.confirm_fn = tui_confirm
    await app.run_async()
    return 0
