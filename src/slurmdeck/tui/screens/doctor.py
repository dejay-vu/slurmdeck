"""Compact, read-only Doctor results page."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding

from slurmdeck.services.doctor import Check
from slurmdeck.tui.format import state_text
from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.widgets import EmptyState, KeyedTable

DOCTOR_COLUMNS = ("CHECK", "STATE", "DETAIL", "FIX")


class DoctorScreen(DeckScreen):
    AUTO_FOCUS = "#doctor-page-table"
    BINDINGS: ClassVar = [Binding("escape", "back", "Back")]

    def __init__(self, remote_name: str | None) -> None:
        super().__init__()
        self.remote_name = remote_name

    def compose_body(self) -> ComposeResult:
        yield KeyedTable(DOCTOR_COLUMNS, id="doctor-page-table")
        empty = EmptyState(id="doctor-page-empty")
        empty.show("Running read-only diagnosis…", "No settings will be inferred or saved.")
        yield empty

    def on_mount(self) -> None:
        self.controller.doctor(self.remote_name, self._show)

    def _show(self, checks: list[Check]) -> None:
        table = self.query_one("#doctor-page-table", KeyedTable)
        table.sync([(check.name, _cells(check)) for check in checks])
        empty = self.query_one("#doctor-page-empty", EmptyState)
        if checks:
            empty.hide()
            table.display = True
        else:
            table.display = False
            empty.show("Doctor returned no checks", "The diagnosis was read-only and made no changes.")

    def action_back(self) -> None:
        self.app.pop_screen()


def _cells(check: Check) -> tuple[Text, ...]:
    return (Text(check.name), state_text(check.state), Text(check.detail), Text(check.fix, style="ui.muted"))
