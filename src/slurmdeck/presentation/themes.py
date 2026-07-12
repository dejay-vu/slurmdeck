"""The only SlurmDeck module that owns concrete color values."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from rich.style import Style
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.theme import BUILTIN_THEMES
from textual.theme import Theme as TextualTheme

from slurmdeck.errors import UserError

ThemeName = Literal["dark", "light", "mono"]
_THEME_NAMES = frozenset({"dark", "light", "mono"})


@dataclass(frozen=True)
class ThemeSpec:
    """One palette rendered into both Rich and Textual theme contracts."""

    name: ThemeName
    rich_styles: Mapping[str, str]
    textual_colors: Mapping[str, str]
    dark: bool
    no_color: bool = False

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self.rich_styles)

    def rich_theme(self) -> RichTheme:
        return RichTheme(self.rich_styles)

    def textual_theme(self) -> TextualTheme:
        return TextualTheme(
            name=f"slurmdeck-{self.name}",
            primary=self.textual_colors["primary"],
            secondary=self.textual_colors["secondary"],
            warning=self.textual_colors["warning"],
            error=self.textual_colors["error"],
            success=self.textual_colors["success"],
            accent=self.textual_colors["accent"],
            foreground=self.textual_colors["foreground"],
            background=self.textual_colors["background"],
            surface=self.textual_colors["surface"],
            panel=self.textual_colors["panel"],
            boost=self.textual_colors["boost"],
            dark=self.dark,
        )

    def style(self, role: str) -> Style:
        try:
            definition = self.rich_styles[role]
        except KeyError as exc:
            raise KeyError(f"unknown SlurmDeck presentation role: {role}") from exc
        return Style.parse(definition)

    def text(self, value: str, role: str) -> Text:
        return Text(value, style=self.style(role))


def _roles(
    *,
    success: str,
    active: str,
    pending: str,
    failure: str,
    unknown: str,
    muted: str,
    accent: str,
) -> dict[str, str]:
    return {
        "state.success": f"bold {success}",
        "state.active": f"bold {active}",
        "state.pending": pending,
        "state.failure": f"bold {failure}",
        "state.warning": pending,
        "state.unknown": unknown,
        "state.muted": f"dim {muted}",
        "feedback.success": f"bold {success}",
        "feedback.info": active,
        "feedback.warning": f"bold {pending}",
        "feedback.error": f"bold {failure}",
        "ui.accent": f"bold {accent}",
        "ui.current": f"bold {success}",
        "ui.muted": f"dim {muted}",
        "ui.stdout": active,
        "ui.stderr": pending,
        "table.title": f"bold {accent}",
        "table.header": f"bold {active}",
        "table.border": muted,
        "table.key": active,
    }


def _mono_roles() -> dict[str, str]:
    return {
        "state.success": "bold",
        "state.active": "bold underline",
        "state.pending": "italic",
        "state.failure": "bold reverse",
        "state.warning": "bold italic",
        "state.unknown": "underline",
        "state.muted": "dim",
        "feedback.success": "bold",
        "feedback.info": "underline",
        "feedback.warning": "bold italic",
        "feedback.error": "bold reverse",
        "ui.accent": "bold underline",
        "ui.current": "bold",
        "ui.muted": "dim",
        "ui.stdout": "underline",
        "ui.stderr": "italic",
        "table.title": "bold underline",
        "table.header": "bold",
        "table.border": "dim",
        "table.key": "underline",
    }


THEMES: dict[str, ThemeSpec] = {
    "dark": ThemeSpec(
        name="dark",
        rich_styles=_roles(
            success="#4ade80",
            active="#38bdf8",
            pending="#facc15",
            failure="#fb7185",
            unknown="#c084fc",
            muted="#94a3b8",
            accent="#22d3ee",
        ),
        textual_colors={
            "primary": "#38bdf8",
            "secondary": "#a78bfa",
            "warning": "#facc15",
            "error": "#fb7185",
            "success": "#4ade80",
            "accent": "#22d3ee",
            "foreground": "#e5e7eb",
            "background": "#0f172a",
            "surface": "#1e293b",
            "panel": "#172033",
            "boost": "#334155",
        },
        dark=True,
    ),
    "light": ThemeSpec(
        name="light",
        rich_styles=_roles(
            success="#15803d",
            active="#0369a1",
            pending="#a16207",
            failure="#be123c",
            unknown="#7e22ce",
            muted="#64748b",
            accent="#0e7490",
        ),
        textual_colors={
            "primary": "#0369a1",
            "secondary": "#6d28d9",
            "warning": "#a16207",
            "error": "#be123c",
            "success": "#15803d",
            "accent": "#0e7490",
            "foreground": "#172554",
            "background": "#f8fafc",
            "surface": "#e2e8f0",
            "panel": "#f1f5f9",
            "boost": "#cbd5e1",
        },
        dark=False,
    ),
    "mono": ThemeSpec(
        name="mono",
        rich_styles=_mono_roles(),
        textual_colors={
            "primary": "white",
            "secondary": "white",
            "warning": "white",
            "error": "white",
            "success": "white",
            "accent": "white",
            "foreground": "white",
            "background": "black",
            "surface": "#111111",
            "panel": "#222222",
            "boost": "#333333",
        },
        dark=True,
        no_color=True,
    ),
}


def _normalize_theme_selection(name: str) -> str:
    normalized = name.lower()
    if normalized.startswith("slurmdeck-") and normalized.removeprefix("slurmdeck-") in _THEME_NAMES:
        return normalized.removeprefix("slurmdeck-")
    return normalized


def _known_theme_selection(name: str) -> bool:
    return name in _THEME_NAMES or name in BUILTIN_THEMES


def resolve_theme_selection(
    override: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    persisted: str | None = None,
) -> str:
    """Resolve the exact TUI theme, ignoring a saved theme no longer available."""
    env = os.environ if environ is None else environ
    if "NO_COLOR" in env:
        return "mono"
    explicit = override or env.get("SLURMDECK_THEME")
    candidate = _normalize_theme_selection(explicit or persisted or "dark")
    if not _known_theme_selection(candidate):
        if explicit is None:
            return "dark"
        raise UserError(
            f"Unknown SlurmDeck theme {candidate!r}.",
            hint="Choose dark, light, mono, or a theme listed by the TUI Theme command.",
        )
    return candidate


def resolve_theme_name(
    override: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    persisted: str | None = None,
) -> ThemeName:
    """Resolve a Rich semantic palette for a SlurmDeck or Textual theme."""
    selection = resolve_theme_selection(override, environ=environ, persisted=persisted)
    if selection in _THEME_NAMES:
        return cast(ThemeName, selection)
    return "dark" if BUILTIN_THEMES[selection].dark else "light"


def resolve_theme(
    override: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    persisted: str | None = None,
) -> ThemeSpec:
    return THEMES[resolve_theme_name(override, environ=environ, persisted=persisted)]
