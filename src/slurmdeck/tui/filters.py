"""Task/run list filtering — pure logic, unit-testable without Textual."""

from __future__ import annotations

from enum import Enum

from slurmdeck.models.status import TaskStatusView
from slurmdeck.storage.repos import RunRow
from slurmdeck.tui.format import ACTIVE_TASK_STATES, FAILED_TASK_STATES


class TaskFilter(Enum):
    ALL = "all"
    ACTIVE = "active"
    FAILED = "failed"

    def next(self) -> TaskFilter:
        order = list(TaskFilter)
        return order[(order.index(self) + 1) % len(order)]

    @property
    def label(self) -> str:
        return self.value


def task_matches(row: TaskStatusView, task_filter: TaskFilter, needle: str = "") -> bool:
    state = row.effective_state
    if task_filter is TaskFilter.ACTIVE and state not in ACTIVE_TASK_STATES:
        return False
    if task_filter is TaskFilter.FAILED and state not in FAILED_TASK_STATES:
        return False
    return _contains(needle, row.task_id, row.name, state, row.display_reason or "")


def run_matches(row: RunRow, needle: str = "") -> bool:
    return _contains(needle, row.id, row.name, row.state, row.slurm_job_id, row.remote)


def _contains(needle: str, *haystacks: str) -> bool:
    if not needle:
        return True
    lowered = needle.lower()
    return any(lowered in value.lower() for value in haystacks if value)
