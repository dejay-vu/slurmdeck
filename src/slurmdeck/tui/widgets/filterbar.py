"""Debounced filter input, hidden until the user presses ``/``."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Input

DEBOUNCE_SECONDS = 0.2


class FilterBar(Input):
    class Applied(Message):
        """Debounced filter value; ``done`` means the user left the bar
        (enter keeps the filter, escape cleared it) and focus should return
        to the list."""

        def __init__(self, value: str, *, done: bool = False) -> None:
            self.value = value
            self.done = done
            super().__init__()

    BINDINGS: ClassVar = [Binding("escape", "dismiss_filter", "Close filter", show=False)]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(placeholder="filter… (enter to apply, esc to clear)", id=id)
        self.display = False
        self._debounce: Timer | None = None

    def open(self) -> None:
        self.display = True
        self.focus()

    def action_dismiss_filter(self) -> None:
        self._cancel_debounce()
        self.value = ""
        self.display = False
        self.post_message(self.Applied("", done=True))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._cancel_debounce()
        if not self.value:
            self.display = False
        self.post_message(self.Applied(self.value, done=True))

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._cancel_debounce()
        self._debounce = self.set_timer(DEBOUNCE_SECONDS, self._emit_debounced)

    def _cancel_debounce(self) -> None:
        if self._debounce is not None:
            self._debounce.stop()
            self._debounce = None

    def _emit_debounced(self) -> None:
        self._debounce = None
        self.post_message(self.Applied(self.value))
