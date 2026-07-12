"""CLI output helpers: consoles, tables, JSON emission, confirmation guards."""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import typer
from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from slurmdeck.errors import UserError
from slurmdeck.operations import OperationEvent, OperationPhase, OperationSink
from slurmdeck.presentation import resolve_theme, state_style
from slurmdeck.structured_errors import StructuredError

JSON_SCHEMA_VERSION = 1
RecordLayout = Literal["table", "double", "key_value"]
_json_mode: ContextVar[bool] = ContextVar("slurmdeck_json_mode", default=False)


def _make_console(*, stderr: bool) -> Console:
    theme = resolve_theme(environ={})
    return Console(stderr=stderr, theme=theme.rich_theme(), no_color=theme.no_color)


console = _make_console(stderr=False)
err_console = _make_console(stderr=True)


def configure_output_theme(theme_name: str | None = None, *, persisted: str | None = None) -> None:
    """Apply saved, environment, or explicit theme selection to both CLI streams."""
    theme = resolve_theme(theme_name, persisted=persisted)
    console.push_theme(theme.rich_theme())
    err_console.push_theme(theme.rich_theme())
    console.no_color = theme.no_color
    err_console.no_color = theme.no_color


def styled_state(state: str) -> Text:
    style = state_style(state)
    return Text(state, style=style or "")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def set_json_output(enabled: bool, context: typer.Context) -> None:
    """Set output mode at the start of a parsed command invocation."""
    token: Token[bool] = _json_mode.set(enabled)
    context.call_on_close(lambda: _json_mode.reset(token))


def json_output_requested(argv: Sequence[str] | None = None) -> bool:
    """Return whether this invocation requested JSON, stopping at command ``--``."""
    if _json_mode.get():
        return True
    arguments = list(sys.argv[1:] if argv is None else argv)
    for argument in arguments:
        if argument == "--":
            break
        if argument == "--json":
            return True
    return False


def _cell_text(value: object) -> Text:
    if isinstance(value, Text):
        return value.copy()
    return Text(str(value))


@dataclass
class OutputManager:
    """Own CLI stream contracts and width-adaptive record rendering."""

    console: Console
    err_console: Console

    @property
    def record_layout(self) -> RecordLayout:
        if not self.console.is_terminal or self.console.width < 80:
            return "key_value"
        if self.console.width < 120:
            return "double"
        return "table"

    def _write_document(self, document: dict[str, object]) -> None:
        stream = self.console.file
        stream.write(json.dumps(document, indent=2, default=_json_default, ensure_ascii=False) + "\n")
        stream.flush()

    def emit_json(self, data: object, *, meta: dict[str, object] | None = None) -> None:
        self._write_document(
            {
                "schema_version": JSON_SCHEMA_VERSION,
                "ok": True,
                "data": data,
                "meta": meta or {},
                "error": None,
            }
        )

    def emit_error(self, error: StructuredError, *, meta: dict[str, object] | None = None) -> None:
        self._write_document(
            {
                "schema_version": JSON_SCHEMA_VERSION,
                "ok": False,
                "data": None,
                "meta": meta or {},
                "error": error,
            }
        )

    def records(
        self,
        title: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> None:
        materialized = [tuple(row) for row in rows]
        if not materialized:
            self.console.print(Text(f"{title}: nothing to show.", style="ui.muted"))
            return
        if self.record_layout == "table":
            self._table(title, columns, materialized)
        elif self.record_layout == "double":
            self._double(title, columns, materialized)
        else:
            self._key_value(title, columns, materialized)

    def details(self, title: str, pairs: Sequence[tuple[str, object]]) -> None:
        if not self.console.is_terminal:
            stream = self.console.file
            stream.write(f"{title}\n")
            for key, value in pairs:
                stream.write(f"{key}: {_cell_text(value).plain}\n")
            stream.flush()
            return
        table = Table(
            title=title,
            title_justify="left",
            title_style="table.title",
            show_header=False,
            box=None,
            pad_edge=False,
        )
        table.add_column(style="table.key", no_wrap=True)
        table.add_column(overflow="fold")
        for key, value in pairs:
            table.add_row(key, _cell_text(value))
        self.console.print(table)

    def _table(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
        table = Table(
            title=title,
            title_justify="left",
            title_style="table.title",
            header_style="table.header",
            border_style="table.border",
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=False,
            collapse_padding=True,
        )
        for column in columns:
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(*(_cell_text(value) for value in row))
        self.console.print(table)

    def _double(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
        self.console.print(Text(title, style="ui.accent"))
        midpoint = (len(columns) + 1) // 2
        groups = (range(0, midpoint), range(midpoint, len(columns)))
        for row_index, row in enumerate(rows):
            if row_index:
                self.console.print()
            for indices in groups:
                line = Text("  ")
                for field_index, column_index in enumerate(indices):
                    if field_index:
                        line.append("  │  ", style="ui.muted")
                    line.append(f"{columns[column_index]}: ", style="table.key")
                    line.append_text(_cell_text(row[column_index]))
                self.console.print(line, overflow="fold", crop=False)

    def _key_value(self, title: str, columns: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
        if not self.console.is_terminal:
            stream = self.console.file
            stream.write(f"{title}\n")
            for row_index, row in enumerate(rows):
                if row_index:
                    stream.write("\n")
                for column, value in zip(columns, row, strict=True):
                    stream.write(f"{column}: {_cell_text(value).plain}\n")
            stream.flush()
            return
        self.console.print(Text(title, style="ui.accent"), soft_wrap=not self.console.is_terminal, crop=False)
        for row_index, row in enumerate(rows):
            if row_index:
                self.console.print()
            for column, value in zip(columns, row, strict=True):
                line = Text()
                line.append(f"{column}: ", style="table.key")
                line.append_text(_cell_text(value))
                self.console.print(
                    line,
                    soft_wrap=not self.console.is_terminal,
                    overflow="fold",
                    crop=False,
                )


def _manager() -> OutputManager:
    return OutputManager(console, err_console)


def emit_json(payload: object, *, meta: dict[str, object] | None = None) -> None:
    _manager().emit_json(payload, meta=meta)


def emit_error_json(error: StructuredError, *, meta: dict[str, object] | None = None) -> None:
    _manager().emit_error(error, meta=meta)


def data_table(title: str, columns: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    _manager().records(title, columns, rows)


def kv_panel(title: str, pairs: Sequence[tuple[str, object]]) -> None:
    _manager().details(title, pairs)


def context_line(subject: str, pairs: Sequence[tuple[str, object]]) -> None:
    """Render one Rich context line to stderr without contaminating data stdout."""
    line = Text(subject, style="ui.accent")
    for key, value in pairs:
        line.append(f" · {key} ", style="ui.muted")
        line.append(str(value), style="bold")
    err_console.print(line)


def success(message: str) -> None:
    err_console.print(f"[feedback.success]✓[/feedback.success] {message}")


def warn(message: str) -> None:
    err_console.print(f"[feedback.warning]![/feedback.warning] {message}")


def error(message: str) -> None:
    err_console.print(f"[feedback.error]✗[/feedback.error] {message}")


def confirm_or_exit(action: str, *, yes: bool) -> None:
    """Destructive-action guard: interactive confirm, or --yes when non-interactive."""
    if yes:
        return
    if not sys.stdin.isatty():
        raise UserError(f"{action} requires --yes when not running interactively.")
    if not typer.confirm(f"{action}?", default=False):
        raise typer.Exit(1)


def _progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(style="state.active"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(style="state.pending", complete_style="state.success", finished_style="state.success"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


class _DelayedProgress:
    """A Rich progress display that remains invisible for short operations."""

    def __init__(
        self,
        console: Console,
        message: str,
        *,
        delay: float = 0.2,
        progress: Any | None = None,
        timer_factory: Any = threading.Timer,
    ) -> None:
        self._progress = progress or _progress(console)
        self._task_id = self._progress.add_task(message, total=None)
        self._lock = threading.Lock()
        self._visible = False
        self._closed = False
        self._timer = timer_factory(delay, self._show)
        self._timer.daemon = True
        self._timer.start()

    def _show(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._progress.start()
            self._visible = True

    def __call__(self, event: OperationEvent) -> None:
        with self._lock:
            if self._closed:
                return
            description = event.message or event.phase.value
            self._progress.update(
                self._task_id,
                description=description,
                completed=event.current,
                total=event.total,
                refresh=self._visible,
            )

    def close(self) -> None:
        self._timer.cancel()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._visible:
                self._progress.stop()


class _PhaseBoundarySink:
    """Stable non-TTY feedback: operation start plus one line per new phase."""

    def __init__(self, console: Console, message: str) -> None:
        self._console = console
        self._last_phase: OperationPhase | None = None
        self._console.print(f"operation: {message}")

    def __call__(self, event: OperationEvent) -> None:
        if event.phase == self._last_phase:
            return
        self._last_phase = event.phase
        self._console.print(f"{event.phase.value}: {event.message or event.phase.value}")


@contextmanager
def activity(message: str) -> Iterator[OperationSink]:
    """Delayed TTY progress or stable non-TTY phase-boundary feedback."""
    if json_output_requested():
        yield lambda _event: None
        return
    if err_console.is_terminal:
        renderer = _DelayedProgress(err_console, message)
        try:
            yield renderer
        finally:
            renderer.close()
    else:
        yield _PhaseBoundarySink(err_console, message)
