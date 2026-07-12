"""The runs dashboard — default screen of the app."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import InvalidDependencyPolicy
from slurmdeck.models.env import EnvironmentView
from slurmdeck.models.status import RunSummary
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import StatusService
from slurmdeck.storage.repos import RunRow
from slurmdeck.tui.drafts import NewRunDraft
from slurmdeck.tui.filters import run_matches
from slurmdeck.tui.format import age, state_text, summary_text
from slurmdeck.tui.screens import _run_actions as actions
from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.screens.run_detail import RunDetailScreen
from slurmdeck.tui.widgets import (
    DetailPane,
    EmptyState,
    FilterBar,
    KeyedTable,
    MasterPane,
    ResponsiveMasterDetail,
)
from slurmdeck.tui.widgets.forms import NewRunModal
from slurmdeck.tui.widgets.responsive import split_layout
from slurmdeck.tui.workflows import afterok_is_available

COLUMNS = ("RUN", "STATE", "TASKS", "JOB", "AGE")


class RunsScreen(DeckScreen):
    AUTO_FOCUS = "#runs-table"
    BINDINGS: ClassVar = [
        Binding("n", "new_run", "New run"),
        Binding("o", "open_run", "Open", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("s", "submit_run", "Submit", show=False),
        Binding("c", "cancel_run", "Cancel", show=False),
        Binding("t", "retry_run", "Retry", show=False),
        Binding("p", "pull_run", "Pull", show=False),
        Binding("d", "clean_run", "Clean", show=False),
        Binding("slash", "open_filter", "Filter", show=False),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._needle = ""
        self._rows: list[RunRow] = []

    def compose_body(self) -> ComposeResult:
        with ResponsiveMasterDetail():
            with MasterPane():
                yield FilterBar(id="runs-filter")
                yield KeyedTable(COLUMNS, id="runs-table")
                yield EmptyState(id="runs-empty")
            yield DetailPane(id="run-preview")

    def reload(self) -> None:
        table = self.query_one("#runs-table", KeyedTable)
        empty = self.query_one("#runs-empty", EmptyState)
        if self.ctx.project is None:
            table.display = False
            self.deck.counts_text = ""
            empty.show(
                "Not inside a slurmdeck project",
                "cd into your project and run:  slurmdeck init",
                "Press 3 to manage remotes, ? for help.",
            )
            return
        rows = RunService(self.ctx).list_views()
        self._rows = rows
        visible = [row for row in rows if run_matches(row, self._needle)]
        status = StatusService(self.ctx)
        table.sync([(row.id, _cells(row, self._summary(status, row))) for row in visible])
        active = sum(1 for row in rows if row.state in {"submitted", "WAITING_FOR_ENV"})
        self.deck.counts_text = f"{len(rows)} run(s) · {active} active"
        if not rows:
            table.display = False
            empty.show(
                "No runs yet",
                "Submit one from your shell:",
                "slurmdeck submit --time 01:00:00 -- python train.py",
            )
        elif not visible:
            table.display = False
            empty.show("No runs match the filter", f"“{self._needle}” — press / to edit, esc to clear.")
        else:
            empty.hide()
            table.display = True
        self._show_preview(table.selected_key)

    @staticmethod
    def _summary(status: StatusService, row: RunRow) -> RunSummary:
        """The cached summary is empty until the first refresh; fall back to
        the live task table so planned runs show their task count."""
        if row.summary.total:
            return row.summary
        return status.summary(row.id)

    # -- selection ---------------------------------------------------------------

    def _selected(self) -> RunRow | None:
        key = self.query_one("#runs-table", KeyedTable).selected_key
        if key is None:
            self.notify("No run selected.", severity="warning")
            return None
        try:
            return RunService(self.ctx).get(key)
        except UserError as exc:
            self.notify(str(exc), severity="error")
            return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            run_id = str(event.row_key.value)
            if split_layout(self.app.size.width):
                self._show_preview(run_id)
            else:
                self.app.push_screen(RunDetailScreen(run_id))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self._show_preview(str(event.row_key.value))

    def _show_preview(self, run_id: str | None) -> None:
        detail = self.query_one("#run-preview", DetailPane)
        row = next((item for item in self._rows if item.id == run_id), None)
        if row is None:
            detail.show_empty("Run", "Select a run to inspect its identity, resources, and status.")
            return
        summary = self._summary(StatusService(self.ctx), row)
        resources = " ".join(f"{key}={value}" for key, value in row.resources.model_dump(exclude_none=True).items())
        command = " ".join(row.command.argv) if row.command.argv is not None else row.command.shell or ""
        detail.show_record(
            "Run",
            [
                ("ID", row.id),
                ("Name", row.name),
                ("State", state_text(row.state)),
                ("Tasks", summary_text(summary)),
                ("Job", row.slurm_job_id or "-"),
                ("Remote", row.remote),
                ("Environment", row.env_id or "none"),
                ("Resources", resources),
                ("Command", command),
            ],
        )

    # -- actions ---------------------------------------------------------------

    def action_refresh(self) -> None:
        self.controller.refresh_now()

    def action_new_run(self) -> None:
        if self.ctx.project is None:
            self.notify("Creating a run needs a project (run `slurmdeck init` first).", severity="warning")
            return
        try:
            remote = self.ctx.resolve_remote()
        except UserError as exc:
            self.notify(str(exc), severity="warning")
            return
        profile = remote.cluster
        dependency_capable = (
            self.ctx.project.config.env is not None
            and profile is not None
            and profile.slurm.afterok_dependency is True
            and profile.slurm.kill_invalid_dependency
            in {InvalidDependencyPolicy.PER_JOB, InvalidDependencyPolicy.SITE_WIDE}
        )
        if dependency_capable:
            self.controller.list_envs(self._open_new_run_for_envs)
        else:
            self._open_new_run(False)

    def _open_new_run_for_envs(self, records: list[EnvironmentView]) -> None:
        profile = self.ctx.resolve_remote().cluster
        self._open_new_run(afterok_is_available(profile, records))

    def _open_new_run(self, afterok_eligible: bool) -> None:
        self.deck.afterok_eligible = afterok_eligible

        def on_result(draft: NewRunDraft | None) -> None:
            if draft is not None:
                self.controller.create_run(draft)

        project = self.ctx.require_project()
        self.app.push_screen(
            NewRunModal(
                afterok_eligible=afterok_eligible,
                resources=project.config.resources,
                project_root=project.paths.root,
            ),
            on_result,
        )

    def action_open_run(self) -> None:
        key = self.query_one("#runs-table", KeyedTable).selected_key
        if key is not None:
            self.app.push_screen(RunDetailScreen(key))

    def action_open_filter(self) -> None:
        self.query_one("#runs-filter", FilterBar).open()

    def action_clear_filter(self) -> None:
        bar = self.query_one("#runs-filter", FilterBar)
        if self._needle or bar.display:
            bar.action_dismiss_filter()

    def on_filter_bar_applied(self, event: FilterBar.Applied) -> None:
        self._needle = event.value
        self.reload()
        if event.done:
            table = self.query_one("#runs-table", KeyedTable)
            if table.display:
                table.focus()

    def action_submit_run(self) -> None:
        if (row := self._selected()) is not None:
            actions.confirm_submit(self, row)

    def action_cancel_run(self) -> None:
        if (row := self._selected()) is not None:
            actions.confirm_cancel(self, row)

    def action_retry_run(self) -> None:
        if (row := self._selected()) is not None:
            actions.confirm_retry(self, row)

    def action_pull_run(self) -> None:
        if (row := self._selected()) is not None:
            actions.prompt_pull(self, row)

    def action_clean_run(self) -> None:
        if (row := self._selected()) is not None:
            actions.confirm_clean(self, row)


def _cells(row: RunRow, summary: RunSummary) -> tuple[Text, ...]:
    return (
        Text(row.id),
        state_text(row.state),
        summary_text(summary),
        Text(row.slurm_job_id or "-"),
        Text(age(row.created_at)),
    )
