"""Reusable TUI widgets."""

from slurmdeck.tui.widgets.chrome import ErrorPanel, StatusBar, TopBar
from slurmdeck.tui.widgets.empty import EmptyState
from slurmdeck.tui.widgets.filterbar import FilterBar
from slurmdeck.tui.widgets.modals import ConfirmModal, InputModal
from slurmdeck.tui.widgets.responsive import DetailPane, MasterPane, ResponsiveMasterDetail
from slurmdeck.tui.widgets.tables import KeyedTable

__all__ = [
    "ConfirmModal",
    "DetailPane",
    "EmptyState",
    "ErrorPanel",
    "FilterBar",
    "InputModal",
    "KeyedTable",
    "MasterPane",
    "ResponsiveMasterDetail",
    "StatusBar",
    "TopBar",
]
