"""The SlurmDeck TUI application: modes, global keys, reactive chrome state."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import ClassVar, cast

from textual.app import App
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.reactive import reactive

from slurmdeck.errors import UserError
from slurmdeck.operations import OperationEvent
from slurmdeck.presentation import THEMES, ThemeSpec, resolve_theme, resolve_theme_selection
from slurmdeck.services.context import AppContext
from slurmdeck.tui.controller import DeckController
from slurmdeck.tui.messages import (
    OperationFinished,
    OperationProgressed,
    OperationStarted,
    RefreshFinished,
    RefreshStarted,
)
from slurmdeck.tui.screens import DeckScreen, EnvsScreen, HelpScreen, RemotesScreen, RunsScreen

QUIT_CONFIRM_SECONDS = 2.0


@dataclass
class _OperationDisplay:
    label: str
    mutation: bool
    started_at: float
    event: OperationEvent | None = None


class DeckCommandProvider(Provider):
    """Command palette entries (colon)."""

    @property
    def deck(self) -> SlurmDeckApp:
        return cast("SlurmDeckApp", self.app)

    def _commands(self) -> tuple[tuple[str, str, Callable[[], None]], ...]:
        deck = self.deck
        return (
            ("Go to runs", "Dashboard of all runs in this project", partial(deck.action_go, "runs")),
            ("Go to environments", "Environments prepared on the remote", partial(deck.action_go, "envs")),
            ("Go to remotes", "Configured remotes and doctor checks", partial(deck.action_go, "remotes")),
            ("Refresh status now", "Query the scheduler and task artifacts", deck.controller.refresh_now),
            ("Toggle auto-refresh", "Pause or resume the background status refresh", deck.action_toggle_auto),
            ("Help", "Keyboard reference and workflow overview", deck.action_help),
        )

    async def discover(self) -> Hits:
        for name, help_text, callback in self._commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._commands():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)


class SlurmDeckApp(App[None]):
    """Interactive control surface over the same services as the CLI."""

    TITLE = "SlurmDeck"
    CSS_PATH = "styles.tcss"
    COMMAND_PALETTE_BINDING = "colon"
    MODES: ClassVar = {"runs": RunsScreen, "envs": EnvsScreen, "remotes": RemotesScreen}
    COMMANDS: ClassVar = App.COMMANDS | {DeckCommandProvider}
    HORIZONTAL_BREAKPOINTS: list[tuple[int, str]] = [(0, "-compact"), (100, "-split")]  # noqa: RUF012
    BINDINGS: ClassVar = [
        Binding("1", "go('runs')", "Runs", show=False),
        Binding("2", "go('envs')", "Envs", show=False),
        Binding("3", "go('remotes')", "Remotes", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("colon", "command_palette", "Command palette", show=False, system=True),
        Binding("ctrl+x", "dismiss_error", "Dismiss error", show=False),
        Binding("ctrl+q", "ignore_legacy_quit", show=False, system=True),
        Binding("ctrl+c", "confirm_quit", show=False, system=True),
    ]

    # chrome state watched by TopBar/StatusBar
    refreshing: reactive[bool] = reactive(False)
    last_refresh_at: reactive[float | None] = reactive(None)
    connection: reactive[str] = reactive("unknown")
    operation_text: reactive[str] = reactive("")
    operation_started_at: reactive[float | None] = reactive(None)
    operation_elapsed: reactive[float] = reactive(0.0)
    counts_text: reactive[str] = reactive("")
    remote_name: reactive[str] = reactive("")
    auto_refresh_enabled: reactive[bool] = reactive(True)
    status_stale: reactive[bool] = reactive(False)
    error_title: reactive[str] = reactive("")
    error_text: reactive[str] = reactive("")
    afterok_eligible: reactive[bool] = reactive(False)

    def __init__(self, ctx: AppContext | None = None, *, theme_name: str | None = None) -> None:
        super().__init__()
        resolved = ctx or AppContext.create()
        self._theme_locked_by_no_color = "NO_COLOR" in os.environ
        self._theme_selection = resolve_theme_selection(theme_name, persisted=resolved.user_store.ui_theme())
        self.theme_spec: ThemeSpec = resolve_theme(self._theme_selection, environ={})
        self.controller = DeckController(self, resolved)
        self.project_label = resolved.project.paths.root.name if resolved.project else "no project"
        self._last_refresh_error = ""
        self._operations: dict[str, _OperationDisplay] = {}
        self._quit_armed_until: float | None = None

    @property
    def ctx(self) -> AppContext:
        return self.controller.ctx

    def on_mount(self) -> None:
        for spec in THEMES.values():
            self.register_theme(spec.textual_theme())
        self._apply_theme(self._theme_selection)
        self.update_identity()
        self.switch_mode("runs" if self.ctx.project is not None else "remotes")
        self.set_interval(5.0, self.controller.maybe_auto_refresh)
        if self.ctx.project is not None:
            self.controller.refresh_now()

    def update_identity(self) -> None:
        try:
            self.remote_name = self.ctx.resolve_remote().name
        except UserError:
            self.remote_name = ""

    # -- global actions ------------------------------------------------------------

    def action_go(self, mode: str) -> None:
        if mode != self.current_mode:
            self.switch_mode(mode)

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_toggle_auto(self) -> None:
        self.controller.auto_refresh = not self.controller.auto_refresh
        self.auto_refresh_enabled = self.controller.auto_refresh
        self.notify(f"Auto-refresh {'on' if self.auto_refresh_enabled else 'off'}.")

    def action_change_theme(self) -> None:
        """Offer only SlurmDeck themes so Rich and Textual roles stay aligned."""
        if self._theme_locked_by_no_color:
            self.notify("NO_COLOR is set; the UI theme is locked to mono.", severity="warning")
            return
        self.search_commands(
            [
                (
                    (
                        f"SlurmDeck {theme.name.removeprefix('slurmdeck-').title()}"
                        if theme.name.startswith("slurmdeck-")
                        else theme.name
                    ),
                    partial(self.select_theme, theme.name),
                    f"Use and save the {theme.name} theme",
                )
                for theme in sorted(self.available_themes.values(), key=lambda item: item.name)
            ],
            placeholder="Select a theme…",
        )

    def select_theme(self, name: str) -> None:
        """Apply and persist a theme selected interactively."""
        if self._theme_locked_by_no_color:
            self.notify("NO_COLOR is set; the UI theme is locked to mono.", severity="warning")
            return
        selection = resolve_theme_selection(name, environ={})
        try:
            self.ctx.user_store.set_ui_theme(selection)
        except OSError as exc:
            message = f"Could not save theme preference: {exc}"
            self._record_error("Theme was not saved", message)
            self.notify(message, title="Theme was not saved", severity="error", timeout=8)
            return
        self._apply_theme(selection)
        self.notify(f"Theme changed to {selection} and saved as the default.")

    def _apply_theme(self, selection: str) -> None:
        selection = resolve_theme_selection(selection, environ={})
        spec = resolve_theme(selection, environ={})
        self._theme_selection = selection
        self.theme_spec = spec
        textual_name = f"slurmdeck-{selection}" if selection in THEMES else selection
        if self.get_theme(textual_name) is None:
            self.register_theme(spec.textual_theme())
        self.theme = textual_name
        self.console.push_theme(spec.rich_theme())
        self.console.no_color = spec.no_color

    def action_dismiss_error(self) -> None:
        self.error_title = ""
        self.error_text = ""

    def action_confirm_quit(self) -> None:
        """Exit only after a second Ctrl+C inside a short confirmation window."""
        now = time.monotonic()
        if self._quit_armed_until is not None and now <= self._quit_armed_until:
            self._quit_armed_until = None
            self.exit()
            return
        self._quit_armed_until = now + QUIT_CONFIRM_SECONDS
        self.notify(
            "Press Ctrl+C again within 2 seconds to quit.",
            title="Confirm exit",
            severity="warning",
            timeout=QUIT_CONFIRM_SECONDS,
        )

    def action_ignore_legacy_quit(self) -> None:
        """Override Textual's default Ctrl+Q exit without consuming widget bindings."""

    def operation_elapsed_now(self) -> float:
        if self.operation_started_at is None:
            return 0.0
        return max(self.operation_elapsed, time.monotonic() - self.operation_started_at)

    def _record_error(self, title: str, message: str) -> None:
        self.error_title = title
        self.error_text = message

    # -- controller messages ----------------------------------------------------------

    def _reload_screens(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, DeckScreen):
                screen.reload()

    def on_refresh_started(self, _message: RefreshStarted) -> None:
        self.refreshing = True

    def on_refresh_finished(self, message: RefreshFinished) -> None:
        self.refreshing = False
        self.last_refresh_at = self.controller.last_refresh_at
        self.connection = self.controller.connection
        self.status_stale = message.stale
        if message.ok:
            if not message.stale:
                self._last_refresh_error = ""
            if message.changed:
                self._reload_screens()
            if message.stale and message.error:
                self._record_error("Status is stale", message.error)
        else:
            self._record_error("Refresh failed", message.error)
        if message.error and message.error != self._last_refresh_error:
            # throttle: repeated identical failures (e.g. cluster down) toast once
            self._last_refresh_error = message.error
            self.notify(
                message.error,
                title="Status is stale" if message.stale else "Refresh failed",
                severity="warning" if message.stale else "error",
                timeout=8,
            )

    def on_operation_started(self, message: OperationStarted) -> None:
        self._operations[message.operation_id] = _OperationDisplay(
            label=message.label,
            mutation=message.mutation,
            started_at=message.started_at,
        )
        self._sync_operation_display()

    def on_operation_progressed(self, message: OperationProgressed) -> None:
        display = self._operations.get(message.operation_id)
        if display is not None:
            display.event = message.event
            self._sync_operation_display()

    def _sync_operation_display(self) -> None:
        if not self._operations:
            self.operation_text = ""
            self.operation_started_at = None
            self.operation_elapsed = 0.0
            return
        display = max(self._operations.values(), key=lambda item: (item.mutation, item.started_at))
        detail = display.event.message if display.event is not None else ""
        self.operation_text = display.label if not detail or detail == display.label else f"{display.label} · {detail}"
        self.operation_started_at = display.started_at
        self.operation_elapsed = display.event.elapsed if display.event is not None else 0.0

    def on_operation_finished(self, message: OperationFinished) -> None:
        self._operations.pop(message.operation_id, None)
        self._sync_operation_display()
        self.connection = self.controller.connection
        if message.ok:
            if message.message:
                self.notify(message.message)
            self.update_identity()
            self._reload_screens()
        else:
            self._record_error(message.label, message.error)
            self.notify(message.error, title=message.label, severity="error", timeout=8)
