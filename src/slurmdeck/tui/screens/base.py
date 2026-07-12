"""DeckScreen: chrome + reload plumbing shared by every full screen."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer

from slurmdeck.tui.widgets import ConfirmModal, ErrorPanel, StatusBar, TopBar

if TYPE_CHECKING:
    from slurmdeck.services.context import AppContext
    from slurmdeck.tui.app import SlurmDeckApp
    from slurmdeck.tui.controller import DeckController


class DeckScreen(Screen[None]):
    """A full screen framed by TopBar/StatusBar/Footer.

    Subclasses yield their body from ``compose_body`` and re-render local
    state in ``reload`` — which runs on mount, on resume, and whenever the
    app broadcasts that data changed. ``reload`` must stay cheap (SQLite or
    in-memory only); anything remote goes through ``self.controller``.
    """

    @property
    def deck(self) -> SlurmDeckApp:
        return cast("SlurmDeckApp", self.app)

    @property
    def ctx(self) -> AppContext:
        return self.deck.controller.ctx

    @property
    def controller(self) -> DeckController:
        return self.deck.controller

    def compose(self) -> ComposeResult:
        yield TopBar()
        yield from self.compose_body()
        yield ErrorPanel()
        yield StatusBar()
        yield Footer()

    def compose_body(self) -> ComposeResult:
        yield from ()

    def reload(self) -> None:
        """Re-read local state and re-render. Default: nothing."""

    def on_mount(self) -> None:
        self.reload()

    def on_screen_resume(self) -> None:
        self.reload()

    def confirm(self, message: str, action: Callable[[], None], *, detail: str = "") -> None:
        """Push a confirmation modal; run *action* only on an explicit Yes."""

        def on_result(confirmed: bool | None) -> None:
            if confirmed:
                action()

        self.app.push_screen(ConfirmModal(message, detail=detail), on_result)
