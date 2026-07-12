"""SVG visual contracts across supported TUI widths and themes."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from slurmdeck.tui.app import SlurmDeckApp
from tests.tui.svg_fixtures import normalize_svg_screenshot

FIXTURES = Path(__file__).parent / "fixtures"


async def _wait_for(condition, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _screenshot(ctx, monkeypatch: pytest.MonkeyPatch, *, width: int, theme: str) -> str:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("SLURMDECK_THEME", raising=False)
    app = SlurmDeckApp(ctx, theme_name=theme)
    async with app.run_test(size=(width, 30), notifications=False) as pilot:
        await _wait_for(lambda: not app.controller.refreshing)
        await pilot.pause()
        return normalize_svg_screenshot(app.export_screenshot(title="SlurmDeck UX fixture", simplify=True))


@pytest.mark.parametrize(
    ("width", "theme", "fixture"),
    [
        (80, "dark", "runs-80-dark.svg"),
        (100, "dark", "runs-100-dark.svg"),
        (120, "dark", "runs-120-dark.svg"),
        (100, "light", "runs-100-light.svg"),
        (100, "mono", "runs-100-mono.svg"),
    ],
)
async def test_tui_svg_contract(
    ctx,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    theme: str,
    fixture: str,
) -> None:
    actual = await _screenshot(ctx, monkeypatch, width=width, theme=theme)
    if candidate_root := os.environ.get("SLURMDECK_SVG_CANDIDATES"):
        destination_root = Path(candidate_root).resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        (destination_root / fixture).write_text(actual, encoding="utf-8")
        return
    assert actual == (FIXTURES / fixture).read_text(encoding="utf-8")


async def test_no_color_is_visually_identical_to_mono(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.user_store.set_ui_theme("light")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("SLURMDECK_THEME", "dark")
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(100, 30), notifications=False) as pilot:
        await _wait_for(lambda: not app.controller.refreshing)
        await pilot.pause()
        actual = normalize_svg_screenshot(app.export_screenshot(title="SlurmDeck UX fixture", simplify=True))
        assert app.theme_spec.name == "mono"
        assert actual == (FIXTURES / "runs-100-mono.svg").read_text(encoding="utf-8")
        assert ctx.user_store.ui_theme() == "light"
