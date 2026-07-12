"""KeyedTable: a DataTable that syncs to keyed rows with minimal updates.

Periodic refreshes usually change a handful of cells; ``sync`` updates only
those, so the cursor, scroll position, and unchanged rows are untouched. Only
when rows appear, disappear, or reorder does it fall back to a rebuild — and
then it restores the cursor by key and the scroll offset.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable

CellValue = Text | str
Row = tuple[str, Sequence[CellValue]]


def row_signature(cells: Sequence[CellValue]) -> tuple[str, ...]:
    """A comparable fingerprint of a row's rendered content."""
    return tuple(cell.markup if isinstance(cell, Text) else str(cell) for cell in cells)


@dataclass(frozen=True)
class SyncPlan:
    """What ``sync`` must do to reconcile the table with the new rows."""

    rebuild: bool = False
    #: row key → column indices whose cells changed
    changed: dict[str, list[int]] = field(default_factory=dict)

    @property
    def noop(self) -> bool:
        return not self.rebuild and not self.changed


def plan_sync(old: dict[str, tuple[str, ...]], new: Sequence[tuple[str, tuple[str, ...]]]) -> SyncPlan:
    """Pure diff: in-place cell updates when the key sequence is unchanged,
    otherwise a rebuild."""
    if [key for key, _ in new] != list(old.keys()):
        return SyncPlan(rebuild=True)
    changed: dict[str, list[int]] = {}
    for key, signature in new:
        previous = old[key]
        columns = [index for index, value in enumerate(signature) if value != previous[index]]
        if columns:
            changed[key] = columns
    return SyncPlan(changed=changed)


class KeyedTable(DataTable[CellValue]):
    """DataTable synced from ``(key, cells)`` rows."""

    def __init__(self, columns: Sequence[str], *, id: str | None = None) -> None:
        super().__init__(id=id, cursor_type="row", zebra_stripes=True)
        self._labels = list(columns)
        self._column_keys = self.add_columns(*self._labels)
        self._synced: dict[str, tuple[str, ...]] = {}

    @property
    def selected_key(self) -> str | None:
        if self.row_count == 0 or self.cursor_row is None:
            return None
        key = self.coordinate_to_cell_key(Coordinate(self.cursor_row, 0)).row_key.value
        return str(key) if key is not None else None

    def sync(self, rows: Sequence[Row]) -> None:
        signatures = [(key, row_signature(cells)) for key, cells in rows]
        plan = plan_sync(self._synced, signatures)
        if plan.rebuild:
            self._rebuild(rows)
        else:
            for key, columns in plan.changed.items():
                cells = next(cells for row_key, cells in rows if row_key == key)
                for column in columns:
                    self.update_cell(key, self._column_keys[column], cells[column], update_width=True)
        self._synced = dict(signatures)

    def _rebuild(self, rows: Sequence[Row]) -> None:
        cursor_key = self.selected_key
        scroll_y = self.scroll_y
        self.clear()
        for key, cells in rows:
            self.add_row(*cells, key=key)
        if cursor_key is not None and any(key == cursor_key for key, _ in rows):
            self.move_cursor(row=self.get_row_index(cursor_key))
        self.scroll_y = min(scroll_y, max(0, self.row_count - 1))
