"""Centered empty/guidance state shown instead of an empty table.

Render-based (not ``Static.update``): updates issued while a mode's initial
screen is still mounting paint blank on Textual 8.
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget


class EmptyState(Widget):
    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._title = ""
        self._lines: tuple[str, ...] = ()

    def set_content(self, title: str, *lines: str) -> None:
        self._title = title
        self._lines = lines
        self.refresh()

    def show(self, title: str, *lines: str) -> None:
        self.set_content(title, *lines)
        self.display = True

    def hide(self) -> None:
        self.display = False

    def render(self) -> Text:
        text = Text(justify="center")
        text.append(self._title, style="bold")
        for line in self._lines:
            text.append("\n")
            text.append(line, style="dim")
        return text
