"""Configured remotes: select, connect, disconnect, and run doctor checks."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable

from slurmdeck.services.doctor import Check
from slurmdeck.services.remotes import RemoteInfo, RemoteService
from slurmdeck.tui.drafts import ProfileDraft, RemoteDraft
from slurmdeck.tui.format import state_text
from slurmdeck.tui.screens.base import DeckScreen
from slurmdeck.tui.screens.doctor import DoctorScreen
from slurmdeck.tui.screens.record_detail import RecordDetailScreen
from slurmdeck.tui.widgets import DetailPane, EmptyState, KeyedTable, MasterPane, ResponsiveMasterDetail
from slurmdeck.tui.widgets.forms import ProfileModal, RemoteFormModal
from slurmdeck.tui.widgets.responsive import split_layout

COLUMNS = (" ", "NAME", "DESTINATION", "BASE")
DOCTOR_COLUMNS = ("CHECK", "STATE", "DETAIL", "FIX")


class RemotesScreen(DeckScreen):
    AUTO_FOCUS = "#remotes-table"
    BINDINGS: ClassVar = [
        Binding("a", "add_remote", "Add"),
        Binding("e", "edit_profile", "Profile", show=False),
        Binding("r", "refresh", "Reload", show=False),
        Binding("u", "use_remote", "Use", show=False),
        Binding("c", "connect_remote", "Connect", show=False),
        Binding("x", "disconnect_remote", "Disconnect", show=False),
        Binding("d", "doctor", "Doctor", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._doctor_checks: list[Check] | None = None

    def compose_body(self) -> ComposeResult:
        with ResponsiveMasterDetail():
            with MasterPane():
                yield KeyedTable(COLUMNS, id="remotes-table")
                yield EmptyState(id="remotes-empty")
            with MasterPane(id="remote-detail", classes="responsive-detail"):
                yield DetailPane(id="remote-preview", classes="responsive-detail-content")
                doctor = KeyedTable(DOCTOR_COLUMNS, id="doctor-table")
                doctor.add_class("responsive-detail-content")
                doctor.display = False
                yield doctor

    def reload(self) -> None:
        table = self.query_one("#remotes-table", KeyedTable)
        empty = self.query_one("#remotes-empty", EmptyState)
        infos = RemoteService(self.ctx).list_remotes()
        table.sync([(info.name, _cells(info)) for info in infos])
        if not infos:
            table.display = False
            empty.show(
                "No remotes configured",
                "Add your cluster from the shell:",
                "slurmdeck remote add hpc --host user@login.example.com --base '$WORK/slurmdeck'",
                "then press c here to connect it.",
            )
        else:
            empty.hide()
            table.display = True
        if self._doctor_checks is None:
            self._show_preview(table.selected_key, clear_doctor=False)
        else:
            self._render_doctor()

    def _selected(self) -> str | None:
        name = self.query_one("#remotes-table", KeyedTable).selected_key
        if name is None:
            self.notify("No remote selected.", severity="warning")
        return name

    # -- actions ---------------------------------------------------------------

    def action_refresh(self) -> None:
        self.reload()

    def action_add_remote(self) -> None:
        def on_result(draft: RemoteDraft | None) -> None:
            if draft is not None:
                self.controller.add_remote(draft, after=self._remote_changed)

        self.app.push_screen(RemoteFormModal(), on_result)

    def action_edit_profile(self) -> None:
        if (name := self._selected()) is None:
            return
        remote = self.ctx.user_store.read_remote(name)

        def on_result(draft: ProfileDraft | None) -> None:
            if draft is not None:
                self.controller.save_profile(draft, after=self._remote_changed)

        self.app.push_screen(ProfileModal(name, remote.cluster), on_result)

    def _remote_changed(self) -> None:
        self.deck.update_identity()
        self.reload()

    def _use(self, name: str) -> None:
        RemoteService(self.ctx).use(name)
        self.deck.update_identity()
        self.notify(f"Now using remote {name}.")
        self.reload()

    def action_use_remote(self) -> None:
        if (name := self._selected()) is not None:
            self._use(name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "remotes-table" and event.row_key.value is not None:
            name = str(event.row_key.value)
            self._show_preview(name)
            if not split_layout(self.app.size.width):
                self.app.push_screen(RecordDetailScreen("Remote", self._detail_fields(name)))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "remotes-table" and event.row_key.value is not None:
            self._show_preview(str(event.row_key.value))

    def _detail_fields(self, name: str) -> list[tuple[str, str | Text]]:
        remote = self.ctx.user_store.read_remote(name)
        profile = remote.cluster
        executor = profile.default_build_executor.value if profile and profile.default_build_executor else "-"
        partition = profile.slurm.partition if profile and profile.slurm.partition else "-"
        return [
            ("Name", remote.name),
            ("Destination", remote.destination),
            ("Connection", "SSH alias" if remote.ssh_alias else "Host"),
            ("Configured base", remote.base),
            ("Resolved base", remote.resolved_base or "not connected"),
            ("Cluster profile", "configured" if profile is not None else "not configured"),
            ("Default build executor", executor),
            ("Partition", partition),
            ("Afterok", "supported" if profile and profile.slurm.afterok_dependency is True else "not declared"),
        ]

    def _show_preview(self, name: str | None, *, clear_doctor: bool = True) -> None:
        if clear_doctor:
            self._doctor_checks = None
        preview = self.query_one("#remote-preview", DetailPane)
        self.query_one("#doctor-table", KeyedTable).display = False
        preview.display = True
        if name is None:
            preview.show_empty("Remote", "Select a remote to inspect its destination and explicit cluster profile.")
        else:
            preview.show_record("Remote", self._detail_fields(name))

    def action_connect_remote(self) -> None:
        if (name := self._selected()) is not None:
            self.controller.connect_remote(name, after=self.reload)

    def action_disconnect_remote(self) -> None:
        if (name := self._selected()) is not None:
            self.controller.disconnect_remote(name)

    def action_doctor(self) -> None:
        # no selection is fine: doctor then checks the default remote
        name = self.query_one("#remotes-table", KeyedTable).selected_key
        if split_layout(self.app.size.width):
            self.controller.doctor(name, self._show_doctor)
        else:
            self.app.push_screen(DoctorScreen(name))

    def _show_doctor(self, checks: list[Check]) -> None:
        self._doctor_checks = checks
        self._render_doctor()

    def _render_doctor(self) -> None:
        table = self.query_one("#doctor-table", KeyedTable)
        self.query_one("#remote-preview", DetailPane).display = False
        table.display = True
        table.sync([(check.name, _doctor_cells(check)) for check in self._doctor_checks or []])


def _cells(info: RemoteInfo) -> tuple[Text, ...]:
    return (
        Text("▸" if info.current else "", style="ui.current"),
        Text(info.name, style="bold" if info.current else ""),
        Text(info.destination),
        Text(info.resolved_base or f"{info.base} (unresolved)", style="" if info.resolved_base else "dim"),
    )


def _doctor_cells(check: Check) -> tuple[Text, ...]:
    return (Text(check.name), state_text(check.state), Text(check.detail), Text(check.fix, style="dim"))
