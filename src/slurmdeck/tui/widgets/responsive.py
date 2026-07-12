"""Shared responsive master-detail primitives."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Static

SPLIT_MIN_WIDTH = 100


def split_layout(width: int) -> bool:
    """Return whether *width* has room for persistent master and detail panes."""
    return width >= SPLIT_MIN_WIDTH


def _classes(required: str, supplied: str | None) -> str:
    return required if not supplied else f"{required} {supplied}"


class ResponsiveMasterDetail(Horizontal):
    """Horizontal at the split breakpoint; master-only below it."""

    def __init__(self, *children: Widget, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(*children, id=id, classes=_classes("responsive-master-detail", classes))


class MasterPane(Vertical):
    def __init__(self, *children: Widget, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(*children, id=id, classes=_classes("responsive-master", classes))


class DetailPane(Static):
    """Small record renderer used by each wide master-detail workflow."""

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=_classes("responsive-detail", classes))
        self._plain = ""

    @property
    def plain(self) -> str:
        return self._plain

    def show_record(self, title: str, fields: Sequence[tuple[str, str | Text]]) -> None:
        text = record_text(title, fields)
        self._plain = text.plain
        self.update(text)

    def show_empty(self, title: str, message: str) -> None:
        self.show_record(title, [("", message)])


def record_text(title: str, fields: Sequence[tuple[str, str | Text]]) -> Text:
    """Render an untruncated semantic record for inline and full-page details."""
    text = Text()
    text.append(title, style="bold")
    text.append("\n")
    for label, value in fields:
        if label:
            text.append(f"\n{label}\n", style="ui.muted")
        else:
            text.append("\n")
        if isinstance(value, Text):
            text.append_text(value)
        else:
            text.append(value or "-")
    return text
