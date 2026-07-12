from __future__ import annotations

import re
from pathlib import Path

import pytest
from rich.console import Console
from rich.style import Style

from slurmdeck.errors import UserError
from slurmdeck.presentation import THEMES, resolve_theme, resolve_theme_name, state_style


def test_state_styles_are_semantic_roles() -> None:
    assert state_style("COMPLETED") == "state.success"
    assert state_style("RUNNING") == "state.active"
    assert state_style("PENDING") == "state.pending"
    assert state_style("FAILED") == "state.failure"
    assert state_style("UNKNOWN") == "state.unknown"
    assert state_style("not-a-state") is None


def test_theme_resolution_order_and_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_theme_name(environ={}) == "dark"
    assert resolve_theme_name(environ={}, persisted="light") == "light"
    assert resolve_theme_name(environ={}, persisted="monokai") == "dark"
    assert resolve_theme_name(environ={}, persisted="catppuccin-latte") == "light"
    assert resolve_theme_name(environ={}, persisted="removed-theme") == "dark"
    assert resolve_theme_name(environ={"SLURMDECK_THEME": "light"}) == "light"
    assert resolve_theme_name(environ={"SLURMDECK_THEME": "dark"}, persisted="light") == "dark"
    assert resolve_theme_name("dark", environ={"SLURMDECK_THEME": "light"}) == "dark"
    assert resolve_theme_name("light", environ={"NO_COLOR": "1"}, persisted="dark") == "mono"

    monkeypatch.setenv("SLURMDECK_THEME", "mono")
    assert resolve_theme().name == "mono"


def test_invalid_theme_is_an_actionable_user_error() -> None:
    with pytest.raises(UserError, match="dark, light, mono"):
        resolve_theme_name("solarized", environ={})


@pytest.mark.parametrize("name", ["dark", "light", "mono"])
def test_theme_spec_builds_rich_text_and_textual_theme(name: str) -> None:
    spec = THEMES[name]
    console = Console(theme=spec.rich_theme(), no_color=spec.no_color)

    style = console.get_style("state.success")
    concrete = spec.style("state.success")
    text = spec.text("ready", "state.success")
    textual = spec.textual_theme()

    assert isinstance(style, Style)
    assert concrete == style
    assert text.plain == "ready"
    assert text.style == concrete
    assert textual.name == f"slurmdeck-{name}"


def test_mono_theme_contains_no_rich_colors() -> None:
    spec = THEMES["mono"]
    for role in spec.roles:
        assert spec.style(role).color is None
        assert spec.style(role).bgcolor is None


def test_literal_colors_are_confined_to_theme_module() -> None:
    source_root = Path(__file__).parents[1] / "src" / "slurmdeck"
    pattern = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:red|green|yellow|cyan|magenta|blue|white|black)\b")
    offenders: list[str] = []
    for path in source_root.rglob("*"):
        if path.suffix not in {".py", ".tcss"} or path.name == "themes.py":
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(source_root)}:{line_number}: {line.strip()}")
    assert offenders == []
