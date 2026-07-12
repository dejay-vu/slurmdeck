"""Environment attempt logs with bounded tail and optional follow mode."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import RichLog, Static

from slurmdeck.errors import UserError
from slurmdeck.services.env_lifecycle import EnvironmentLog
from slurmdeck.transport import StreamHandle, TransportError
from slurmdeck.tui.screens.base import DeckScreen

TAIL_LINES = 500
MAX_KEPT_LINES = 5000


class EnvLogsScreen(DeckScreen):
    AUTO_FOCUS = "#log-view"
    BINDINGS: ClassVar = [
        Binding("f", "toggle_follow", "Follow"),
        Binding("tab", "switch_stream", "stdout/stderr", show=False),
        Binding("w", "toggle_wrap", "Wrap", show=False),
        Binding("r", "reload_log", "Reload", show=False),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, env_id: str) -> None:
        super().__init__()
        self.env_id = env_id
        self.attempt_id = ""
        self.stream: str | None = None
        self.path = ""
        self._handle: StreamHandle | None = None
        self._content = ""
        self._loading = False

    def compose_body(self) -> ComposeResult:
        yield Static(id="log-title")
        yield RichLog(id="log-view", wrap=False, highlight=False, markup=False, max_lines=MAX_KEPT_LINES)

    def on_mount(self) -> None:
        self._update_title()
        self._fetch()

    def on_unmount(self) -> None:
        self._stop_follow()

    def reload(self) -> None:
        self._update_title()

    def _log(self) -> RichLog:
        return self.query_one("#log-view", RichLog)

    def _update_title(self, note: str = "") -> None:
        text = Text()
        text.append(f" {self.env_id}", style="bold")
        if self.attempt_id:
            text.append(f" · {self.attempt_id}", style="dim")
        if self.stream:
            text.append(f" · {self.stream}", style="ui.stdout" if self.stream == "stdout" else "ui.stderr")
        if self._handle is not None:
            text.append(" · following", style="state.success")
        if self._loading:
            text.append(" · loading…", style="dim")
        if note:
            text.append(f" · {note}", style="dim")
        self.query_one("#log-title", Static).update(text)

    def _render_content(self) -> None:
        log = self._log()
        log.clear()
        for line in self._content.splitlines():
            log.write(line)

    def _fetch(self) -> None:
        if self._loading:
            return
        self._loading = True
        self._update_title()
        self.controller.fetch_env_log(
            self.env_id,
            stream=self.stream,
            lines=TAIL_LINES,
            on_result=self._on_loaded,
            on_error=self._on_error,
        )

    def _on_loaded(self, log: EnvironmentLog) -> None:
        self._loading = False
        self.attempt_id = log.attempt_id
        self.stream = log.stream
        self.path = log.path
        self._content = log.text
        self._render_content()
        self._update_title()

    def _on_error(self, message: str) -> None:
        self._loading = False
        self._update_title(note="load failed")
        self.notify(message, severity="error", timeout=8)

    def action_reload_log(self) -> None:
        self._stop_follow()
        self._fetch()

    def action_switch_stream(self) -> None:
        self._stop_follow()
        self.stream = "stderr" if self.stream == "stdout" else "stdout"
        self._content = ""
        self._render_content()
        self._fetch()

    def action_toggle_wrap(self) -> None:
        log = self._log()
        log.wrap = not log.wrap
        self._render_content()

    def action_toggle_follow(self) -> None:
        if self._handle is not None:
            self._stop_follow()
            self._update_title()
            return
        widget = self._log()

        def on_line(line: str) -> None:
            self.app.call_from_thread(widget.write, line)

        try:
            log = self.controller.follow_env_log(self.env_id, stream=self.stream, on_line=on_line)
        except (UserError, TransportError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.attempt_id = log.attempt_id
        self.stream = log.stream
        self.path = log.path
        self._handle = log.handle
        widget.clear()
        self._update_title()

    def action_back(self) -> None:
        self._stop_follow()
        self.app.pop_screen()

    def _stop_follow(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
