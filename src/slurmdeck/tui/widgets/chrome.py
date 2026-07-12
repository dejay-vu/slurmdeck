"""Persistent chrome: top identity bar and bottom status bar.

Both render from the app's reactive state (see ``SlurmDeckApp``); the status
bar also ticks once a second so staleness and the spinner stay live.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from slurmdeck.tui.format import elapsed, staleness

if TYPE_CHECKING:
    from slurmdeck.tui.app import SlurmDeckApp

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_DOTS = {
    "ok": ("●", "state.success"),
    "error": ("●", "state.failure"),
    "unknown": ("○", "state.unknown"),
}


class TopBar(Widget):
    """One line: app name │ project │ remote + connection dot.

    Renders directly from app state instead of ``Static.update`` — updates
    issued while a mode's initial screen is still mounting paint blank on
    Textual 8, so the content must live in ``render``.
    """

    @property
    def deck(self) -> SlurmDeckApp:
        return cast("SlurmDeckApp", self.app)

    def on_mount(self) -> None:
        self.watch(self.deck, "remote_name", self._state_changed, init=False)
        self.watch(self.deck, "connection", self._state_changed, init=False)

    def _state_changed(self) -> None:
        self.refresh()

    def render(self) -> Text:
        app = self.deck
        dot, dot_style = _DOTS.get(app.connection, _DOTS["unknown"])
        text = Text(no_wrap=True)
        text.append(" SlurmDeck ", style="bold")
        text.append("│ ", style="dim")
        text.append(f"{app.project_label} ")
        text.append("│ ", style="dim")
        if app.remote_name:
            text.append(f"{app.remote_name} ")
            text.append(dot, style=dot_style)
        else:
            text.append("no remote", style="dim")
        return text


class ErrorPanel(Static):
    """Persistent error detail shared by every full-screen workflow."""

    def __init__(self) -> None:
        super().__init__(id="error-panel")
        self.display = False

    @property
    def deck(self) -> SlurmDeckApp:
        return cast("SlurmDeckApp", self.app)

    def on_mount(self) -> None:
        self.watch(self.deck, "error_title", self._state_changed, init=False)
        self.watch(self.deck, "error_text", self._state_changed, init=False)
        self._state_changed()

    def _state_changed(self) -> None:
        self.display = bool(self.deck.error_text)
        self.refresh(layout=True)

    def render(self) -> Text:
        if not self.deck.error_text:
            return Text()
        text = Text(" ✗ ", style="feedback.error")
        text.append(f"{self.deck.error_title}: ", style="bold")
        text.append(self.deck.error_text)
        text.append("   ctrl+x dismiss", style="ui.muted")
        return text


class StatusBar(Horizontal):
    """One line: refresh/staleness │ active operation │ run counts."""

    @property
    def deck(self) -> SlurmDeckApp:
        return cast("SlurmDeckApp", self.app)

    def __init__(self) -> None:
        super().__init__()
        self._rendered: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static(id="status-refresh")
        yield Static(id="status-operation")
        yield Static(id="status-counts")

    def on_mount(self) -> None:
        self.set_interval(0.25, self._render_content)
        for attribute in (
            "refreshing",
            "operation_text",
            "counts_text",
            "last_refresh_at",
            "auto_refresh_enabled",
            "status_stale",
        ):
            self.watch(self.deck, attribute, self._render_content, init=False)
        self._render_content()

    def _apply(self, widget_id: str, text: Text) -> None:
        """Update a segment only when its content actually changed."""
        markup = text.markup
        if self._rendered.get(widget_id) != markup:
            self._rendered[widget_id] = markup
            self.query_one(f"#{widget_id}", Static).update(text)

    def _render_content(self) -> None:
        app = self.deck
        frame = _SPINNER[int(time.time() * 10) % len(_SPINNER)]

        left = Text()
        if app.refreshing:
            left.append(f" {frame} refreshing…", style="state.active")
        else:
            if app.status_stale:
                left.append(" STALE", style="state.warning")
                left.append(f" · {staleness(app.last_refresh_at)}", style="ui.muted")
            else:
                left.append(f" {staleness(app.last_refresh_at)}", style="ui.muted")
            if not app.auto_refresh_enabled:
                left.append("  auto-refresh off", style="state.warning")
        self._apply("status-refresh", left)

        operation = Text()
        if app.operation_text:
            operation.append(
                f"{frame} {app.operation_text} · {elapsed(app.operation_elapsed_now())}",
                style="state.active",
            )
        self._apply("status-operation", operation)

        self._apply("status-counts", Text(f"{app.counts_text} ", style="dim"))
