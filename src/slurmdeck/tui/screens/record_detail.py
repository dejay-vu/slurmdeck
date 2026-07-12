"""Compact full-page details used when a split pane would be too narrow."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.widgets.responsive import record_text


class RecordDetailScreen(DeckScreen):
    BINDINGS: ClassVar = [Binding("escape", "back", "Back")]

    def __init__(self, title: str, fields: Sequence[tuple[str, str | Text]]) -> None:
        super().__init__()
        self._text = record_text(title, fields)

    @property
    def plain(self) -> str:
        return self._text.plain

    def compose_body(self) -> ComposeResult:
        yield Static(self._text, id="record-detail-page")

    def action_back(self) -> None:
        self.app.pop_screen()
