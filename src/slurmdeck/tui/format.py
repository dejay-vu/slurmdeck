"""Pure presentation helpers for the TUI — no Textual imports, fully unit-testable."""

from __future__ import annotations

import calendar
import time

from rich.text import Text

from slurmdeck.models.status import RunSummary
from slurmdeck.presentation import state_style
from slurmdeck.slurm import ACTIVE_STATES, failed_states

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

FAILED_TASK_STATES = frozenset(failed_states() | {"FAILED", "KILLED"})
ACTIVE_TASK_STATES = frozenset(ACTIVE_STATES | {"UNKNOWN", ""})

#: Display order and styling for aggregated task counts.
_SUMMARY_GROUPS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("✔", "state.success", frozenset({"COMPLETED"})),
    ("▶", "state.active", frozenset({"RUNNING", "COMPLETING", "CONFIGURING", "STAGE_OUT"})),
    ("·", "state.pending", frozenset({"PENDING", "SUSPENDED", "REQUEUED", "RESIZING"})),
    ("✗", "state.failure", FAILED_TASK_STATES - {"CANCELLED"}),
    ("⊘", "state.failure", frozenset({"CANCELLED"})),
)
_OTHER_GLYPH, _OTHER_STYLE = "?", "state.unknown"


def parse_utc(stamp: str) -> float | None:
    """Parse a slurmdeck UTC timestamp (``2026-07-03T12:00:00Z``) to an epoch."""
    if not stamp:
        return None
    try:
        return float(calendar.timegm(time.strptime(stamp, UTC_FORMAT)))
    except ValueError:
        return None


def _compact(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        hours, minutes = divmod(int(seconds // 60), 60)
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    days, hours = divmod(int(seconds // 3600), 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def age(stamp: str, *, now: float | None = None) -> str:
    """Compact age of a UTC timestamp: ``42s``, ``3m``, ``2h05m``, ``5d``."""
    epoch = parse_utc(stamp)
    if epoch is None:
        return "-"
    return _compact((now if now is not None else time.time()) - epoch)


def duration(started_at: str, ended_at: str, *, now: float | None = None) -> str:
    """Elapsed time of a task; still-running tasks measure against *now*."""
    start = parse_utc(started_at)
    if start is None:
        return "-"
    end = parse_utc(ended_at)
    if end is None:
        end = now if now is not None else time.time()
    return _compact(end - start)


def state_text(state: str) -> Text:
    return Text(state or "-", style=state_style(state) or "")


def summary_text(summary: RunSummary) -> Text:
    """Aggregate task counts into a compact colored strip: ``✔4 ▶2 ✗1``."""
    if summary.total == 0:
        return Text("no tasks", style="dim")
    parts: list[tuple[str, str, int]] = []
    remaining = dict(summary.counts)
    for glyph, style, states in _SUMMARY_GROUPS:
        count = sum(remaining.pop(state, 0) for state in states)
        if count:
            parts.append((glyph, style, count))
    other = sum(remaining.values())
    if other:
        parts.append((_OTHER_GLYPH, _OTHER_STYLE, other))
    text = Text()
    for index, (glyph, style, count) in enumerate(parts):
        if index:
            text.append(" ")
        text.append(f"{glyph}{count}", style=style)
    return text


def staleness(last_success: float | None, *, now: float | None = None) -> str:
    if last_success is None:
        return "not refreshed yet"
    elapsed = (now if now is not None else time.time()) - last_success
    return f"last success {_compact(elapsed)} ago"


def elapsed(seconds: float) -> str:
    return f"{max(0.0, seconds):.1f}s" if seconds < 60 else _compact(seconds)
