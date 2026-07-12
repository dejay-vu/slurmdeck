"""Shared semantic presentation vocabulary for CLI and TUI surfaces."""

from slurmdeck.presentation.states import STATE_STYLES, state_style
from slurmdeck.presentation.themes import (
    THEMES,
    ThemeName,
    ThemeSpec,
    resolve_theme,
    resolve_theme_name,
    resolve_theme_selection,
)

__all__ = [
    "STATE_STYLES",
    "THEMES",
    "ThemeName",
    "ThemeSpec",
    "resolve_theme",
    "resolve_theme_name",
    "resolve_theme_selection",
    "state_style",
]
