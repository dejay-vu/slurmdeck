"""Task table for one run, with state-filter cycling and substring search."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, RichLog, Static

from slurmdeck.errors import UserError
from slurmdeck.models.status import RunSummary, TaskStatusView
from slurmdeck.services.logs import RunLog
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import StatusService
from slurmdeck.storage.repos import RunRow
from slurmdeck.tui.filters import TaskFilter, task_matches
from slurmdeck.tui.format import age, state_text, summary_text
from slurmdeck.tui.screens import _run_actions as actions
from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.screens.logs import LogsScreen
from slurmdeck.tui.widgets import EmptyState, FilterBar, KeyedTable, MasterPane, ResponsiveMasterDetail
from slurmdeck.tui.widgets.responsive import split_layout

COLUMNS = ("TASK", "NAME", "STATE", "EXIT", "STARTED", "TIME", "REASON")


class RunDetailScreen(DeckScreen):
    AUTO_FOCUS = "#tasks-table"
    BINDINGS: ClassVar = [
        Binding("r", "refresh", "Refresh", show=False),
        Binding("l", "open_logs", "Logs"),
        Binding("f", "cycle_filter", "All/Active/Failed", show=False),
        Binding("slash", "open_filter", "Search", show=False),
        Binding("c", "cancel_run", "Cancel", show=False),
        Binding("t", "retry_run", "Retry", show=False),
        Binding("p", "pull_run", "Pull", show=False),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self._filter = TaskFilter.ALL
        self._needle = ""
        self._inline_task_id = ""
        self.inline_log_text = ""
        self._inline_loading = False

    def compose_body(self) -> ComposeResult:
        with ResponsiveMasterDetail():
            with MasterPane():
                yield Static(id="run-meta")
                yield FilterBar(id="tasks-filter")
                yield KeyedTable(COLUMNS, id="tasks-table")
                yield EmptyState(id="tasks-empty")
            with MasterPane(id="task-log-detail", classes="responsive-detail"):
                yield Static(id="inline-log-title")
                yield RichLog(id="inline-log", wrap=False, highlight=False, markup=False, max_lines=5000)

    def reload(self) -> None:
        try:
            run = RunService(self.ctx).get(self.run_id)
        except UserError:
            self.app.pop_screen()  # run was cleaned while we were looking at it
            return
        status = StatusService(self.ctx)
        snapshot = status.snapshot(self.run_id)
        tasks = snapshot.tasks
        visible = [task for task in tasks if task_matches(task, self._filter, self._needle)]
        summary = snapshot.summary
        self.query_one("#run-meta", Static).update(_meta_text(run, summary, self._filter, self._needle))

        table = self.query_one("#tasks-table", KeyedTable)
        empty = self.query_one("#tasks-empty", EmptyState)
        table.sync([(task.task_id, _cells(task)) for task in visible])
        if not tasks:
            table.display = False
            empty.show("No tasks recorded", "This run has not been planned with any tasks.")
        elif not visible:
            table.display = False
            hint = f"filter: {self._filter.label}" + (f" · search: “{self._needle}”" if self._needle else "")
            empty.show("No matching tasks", hint + " — press f to cycle, / to search.")
        else:
            empty.hide()
            table.display = True
        self._select_inline_task(table.selected_key)

    # -- actions ---------------------------------------------------------------

    def action_refresh(self) -> None:
        self.controller.refresh_now([self.run_id])

    def action_cycle_filter(self) -> None:
        self._filter = self._filter.next()
        self.reload()

    def action_open_filter(self) -> None:
        self.query_one("#tasks-filter", FilterBar).open()

    def action_back(self) -> None:
        """Escape clears an active search first; a second escape leaves."""
        bar = self.query_one("#tasks-filter", FilterBar)
        if self._needle or bar.display:
            bar.action_dismiss_filter()
            return
        self.app.pop_screen()

    def on_filter_bar_applied(self, event: FilterBar.Applied) -> None:
        self._needle = event.value
        self.reload()
        if event.done:
            table = self.query_one("#tasks-table", KeyedTable)
            if table.display:
                table.focus()

    def _open_logs(self, task_id: str) -> None:
        self.app.push_screen(LogsScreen(self.run_id, task_id))

    def action_open_logs(self) -> None:
        key = self.query_one("#tasks-table", KeyedTable).selected_key
        if key is None:
            self.notify("No task selected.", severity="warning")
            return
        if split_layout(self.app.size.width):
            self._fetch_inline_log(key)
        else:
            self._open_logs(key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "tasks-table" or event.row_key.value is None:
            return
        task_id = str(event.row_key.value)
        if split_layout(self.app.size.width):
            self._fetch_inline_log(task_id)
        else:
            self._open_logs(task_id)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "tasks-table" and event.row_key.value is not None:
            self._select_inline_task(str(event.row_key.value))

    def _select_inline_task(self, task_id: str | None) -> None:
        selected = task_id or ""
        if selected == self._inline_task_id:
            return
        self._inline_task_id = selected
        self.inline_log_text = ""
        if self.is_mounted:
            self.query_one("#inline-log", RichLog).clear()
            self._update_inline_title("Press Enter or l to load this task's service-selected log.")

    def _update_inline_title(self, note: str = "") -> None:
        text = Text()
        text.append(f" Task {self._inline_task_id or '-'}", style="bold")
        if self._inline_loading:
            text.append(" · loading…", style="ui.muted")
        elif note:
            text.append(f" · {note}", style="ui.muted")
        self.query_one("#inline-log-title", Static).update(text)

    def _fetch_inline_log(self, task_id: str) -> None:
        if self._inline_loading:
            return
        self._select_inline_task(task_id)
        self._inline_loading = True
        self._update_inline_title()
        self.controller.fetch_log_view(
            self.run_id,
            task_id,
            stream=None,
            lines=500,
            on_result=self._on_inline_loaded,
            on_error=self._on_inline_error,
        )

    def _on_inline_loaded(self, log: RunLog) -> None:
        self._inline_loading = False
        self._inline_task_id = log.task_id
        self.inline_log_text = log.text
        widget = self.query_one("#inline-log", RichLog)
        widget.clear()
        for line in log.text.splitlines():
            widget.write(line)
        self._update_inline_title(f"{log.stream.value} · {log.path}")

    def _on_inline_error(self, message: str) -> None:
        self._inline_loading = False
        self._update_inline_title("load failed")
        self.notify(message, severity="error", timeout=8)

    def _run_row(self) -> RunRow | None:
        try:
            return RunService(self.ctx).get(self.run_id)
        except UserError as exc:
            self.notify(str(exc), severity="error")
            return None

    def action_cancel_run(self) -> None:
        if (row := self._run_row()) is not None:
            actions.confirm_cancel(self, row)

    def action_retry_run(self) -> None:
        if (row := self._run_row()) is not None:
            actions.confirm_retry(self, row)

    def action_pull_run(self) -> None:
        if (row := self._run_row()) is not None:
            actions.prompt_pull(self, row)


def _meta_text(run: RunRow, summary: RunSummary, task_filter: TaskFilter, needle: str) -> Text:
    text = Text()
    text.append(f" {run.id}", style="bold")
    text.append("  ")
    text.append_text(state_text(run.state))
    text.append("  ")
    text.append_text(summary_text(summary))
    text.append(
        f"\n job {run.slurm_job_id or '-'} · remote {run.remote} · created {age(run.created_at)} ago", style="dim"
    )
    resources = run.resources.model_dump(exclude_none=True)
    if resources:
        text.append(" · " + " ".join(f"{key}={value}" for key, value in resources.items()), style="dim")
    if run.retry_of:
        text.append(f" · retry of {run.retry_of}", style="dim")
    if task_filter is not TaskFilter.ALL or needle:
        text.append(f"\n showing: {task_filter.label}", style="state.warning")
        if needle:
            text.append(f" · search “{needle}”", style="state.warning")
    return text


def _cells(task: TaskStatusView) -> tuple[Text, ...]:
    exit_text = str(task.exit_code) if task.exit_code is not None else "-"
    return (
        Text(task.task_id),
        Text(task.name),
        state_text(task.effective_state),
        Text(exit_text),
        Text("-"),
        Text("-"),
        Text(task.display_reason or ""),
    )
