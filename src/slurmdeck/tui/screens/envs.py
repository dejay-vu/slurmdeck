"""Environments prepared on the current remote."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable

from slurmdeck.errors import UserError
from slurmdeck.models.env import EnvironmentStatus, EnvironmentView
from slurmdeck.models.remote import Remote
from slurmdeck.services.env_lifecycle import EnvironmentGcReport
from slurmdeck.tui.format import age, state_text
from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.screens.env_logs import EnvLogsScreen
from slurmdeck.tui.screens.record_detail import RecordDetailScreen
from slurmdeck.tui.widgets import DetailPane, EmptyState, KeyedTable, MasterPane, ResponsiveMasterDetail
from slurmdeck.tui.widgets.responsive import split_layout
from slurmdeck.tui.workflows import afterok_is_available

COLUMNS = ("", "ENV", "STATUS", "PREFIX", "AGE")
_ACTIVE = {
    EnvironmentStatus.STAGING,
    EnvironmentStatus.QUEUED,
    EnvironmentStatus.BUILDING,
    EnvironmentStatus.VERIFYING,
    EnvironmentStatus.BUILD_UNKNOWN,
}


class EnvsScreen(DeckScreen):
    AUTO_FOCUS = "#envs-table"
    BINDINGS: ClassVar = [
        Binding("r", "refresh", "Reload", show=False),
        Binding("p", "prepare_env", "Prepare"),
        Binding("a", "attach_env", "Attach", show=False),
        Binding("l", "logs", "Logs", show=False),
        Binding("c", "cancel_env", "Cancel", show=False),
        Binding("b", "rebuild_env", "Rebuild", show=False),
        Binding("d", "remove_env", "Remove", show=False),
        Binding("g", "gc_envs", "GC", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._records: list[EnvironmentView] | None = None  # None = never loaded

    def compose_body(self) -> ComposeResult:
        with ResponsiveMasterDetail():
            with MasterPane():
                yield KeyedTable(COLUMNS, id="envs-table")
                yield EmptyState(id="envs-empty")
            yield DetailPane(id="env-preview")

    def on_mount(self) -> None:
        self.reload()
        if self._remote() is not None:
            self.action_refresh()

    def _remote(self) -> Remote | None:
        try:
            return self.ctx.resolve_remote()
        except UserError:
            return None

    def reload(self) -> None:
        table = self.query_one("#envs-table", KeyedTable)
        empty = self.query_one("#envs-empty", EmptyState)
        self._show_preview(table.selected_key)
        remote = self._remote()
        if remote is None:
            table.display = False
            empty.show(
                "No remote selected",
                "Add one first:  slurmdeck remote add <name> --host user@host --base PATH",
                "Press 3 to manage remotes.",
            )
            return
        if self._records is None:
            table.display = False
            empty.show(f"Environments on {remote.name}", "Loading… (press r to reload)")
            return
        table.sync([(view.record.env_id, _cells(view)) for view in self._records])
        if not self._records:
            table.display = False
            empty.show(
                f"No environments on {remote.name}",
                "Press p to prepare the environment configured in .slurmdeck/project.yaml.",
            )
            return
        empty.hide()
        table.display = True
        self._show_preview(table.selected_key)

    # -- actions ---------------------------------------------------------------

    def action_refresh(self) -> None:
        if self._remote() is None:
            self.notify("No remote selected.", severity="warning")
            return
        self.controller.list_envs(self._loaded)

    def _loaded(self, records: list[EnvironmentView]) -> None:
        self._records = records
        remote = self._remote()
        self.deck.afterok_eligible = afterok_is_available(remote.cluster if remote else None, records)
        self.reload()

    def _selected(self) -> EnvironmentView | None:
        env_id = self.query_one("#envs-table", KeyedTable).selected_key
        view = next(
            (item for item in self._records or [] if item.record.env_id == env_id),
            None,
        )
        if view is None:
            self.notify("No environment selected.", severity="warning")
        return view

    def _show_preview(self, env_id: str | None) -> None:
        detail = self.query_one("#env-preview", DetailPane)
        view = next((item for item in self._records or [] if item.record.env_id == env_id), None)
        if view is None:
            detail.show_empty("Environment", "Select an environment to inspect its immutable identity and state.")
            return
        detail.show_record("Environment", _detail_fields(view))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self._show_preview(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return
        env_id = str(event.row_key.value)
        self._show_preview(env_id)
        if not split_layout(self.app.size.width):
            view = next(item for item in self._records or [] if item.record.env_id == env_id)
            self.app.push_screen(RecordDetailScreen("Environment", _detail_fields(view)))

    def action_prepare_env(self) -> None:
        if self.ctx.project is None:
            self.notify("Preparing needs a project (run `slurmdeck init` first).", severity="warning")
            return
        remote = self._remote()
        if remote is None:
            self.notify("No remote selected.", severity="warning")
            return
        self.confirm(
            f"Prepare the project environment on {remote.name}?",
            lambda: self.controller.prepare_env(after=self.action_refresh),
            detail="Uses the env spec from .slurmdeck/project.yaml; a build job may be submitted.",
        )

    def action_attach_env(self) -> None:
        view = self._selected()
        if view is None:
            return
        if not view.desired_by_project or view.record.status not in _ACTIVE:
            self.notify(
                "Attach is available only for the project's active environment build.",
                severity="warning",
            )
            return
        self.controller.prepare_env(after=self.action_refresh)

    def action_logs(self) -> None:
        if (view := self._selected()) is not None:
            self.app.push_screen(EnvLogsScreen(view.record.env_id))

    def action_cancel_env(self) -> None:
        view = self._selected()
        if view is None:
            return
        if view.record.status not in _ACTIVE:
            self.notify(f"{view.record.env_id} has no active build to cancel.", severity="warning")
            return
        self.confirm(
            f"Cancel the active build for {view.record.env_id}?",
            lambda: self.controller.cancel_env(view.record.env_id, after=self.action_refresh),
            detail="Only this environment attempt is cancelled; runs and shared environment records remain.",
        )

    def action_rebuild_env(self) -> None:
        if self.ctx.project is None or self.ctx.project.config.env is None:
            self.notify("Rebuilding needs a project environment configuration.", severity="warning")
            return
        remote = self._remote()
        if remote is None:
            self.notify("No remote selected.", severity="warning")
            return
        self.confirm(
            f"Build a new immutable environment generation on {remote.name}?",
            lambda: self.controller.prepare_env(rebuild=True, after=self.action_refresh),
            detail="Existing runs keep their exact generation and prefix.",
        )

    def action_remove_env(self) -> None:
        view = self._selected()
        if view is None:
            return
        env_id = view.record.env_id
        self.confirm(
            f"Remove {env_id}?",
            lambda: self.controller.remove_env(env_id, after=self.action_refresh),
            detail=(
                "Managed generations move to trash; external prefixes are only unregistered. References block removal."
            ),
        )

    def action_gc_envs(self) -> None:
        if self._remote() is None:
            self.notify("No remote selected.", severity="warning")
            return
        self.controller.gc_envs(delete=False, on_result=self._preview_gc)

    def _preview_gc(self, report: EnvironmentGcReport) -> None:
        if not report.candidates:
            self.notify("Environment GC found no safe candidates.")
            return
        total_bytes = sum(candidate.size_bytes for candidate in report.candidates)
        self.confirm(
            f"Delete {len(report.candidates)} safe environment GC candidate(s)?",
            lambda: self.controller.gc_envs(delete=True, on_result=lambda _report: self.action_refresh()),
            detail=f"Dry-run found {total_bytes} bytes. Paths outside the schema-v1 layout are never included.",
        )


def _cells(view: EnvironmentView) -> tuple[Text, ...]:
    record = view.record
    return (
        Text("*" if view.desired_by_project else ""),
        Text(record.env_id),
        state_text(record.status),
        Text(record.active_prefix or "-"),
        Text(age(record.created_at)),
    )


def _detail_fields(view: EnvironmentView) -> list[tuple[str, str | Text]]:
    record = view.record
    resources = view.resolved_resources
    return [
        ("ID", record.env_id),
        ("Status", state_text(record.status)),
        ("Backend / ownership", f"{record.backend.value} / {record.ownership.value}"),
        ("Full hash", record.full_hash),
        ("Generation", record.active_generation or "-"),
        ("Prefix", record.active_prefix or "-"),
        ("Current attempt", record.current_attempt or "-"),
        ("Build job / reason", view.job_reason),
        ("Resources", resources.model_dump_json(exclude_none=True) if resources is not None else "-"),
        ("References", "\n".join(view.references) or "none"),
        ("stdout", view.stdout_path or "-"),
        ("stderr", view.stderr_path or "-"),
        ("Last error", record.last_error.summary if record.last_error is not None else "none"),
    ]
