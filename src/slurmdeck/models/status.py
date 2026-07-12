"""Typed scheduler observations and service-owned status views."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from slurmdeck.models.common import StrictModel
from slurmdeck.structured_errors import StructuredError


class SchedulerSource(StrEnum):
    SQUEUE = "squeue"
    SACCT = "sacct"


class SchedulerObservation(StrictModel):
    job_id: str
    array_task_id: str | None = None
    scheduler_state: str
    scheduler_reason: str = ""
    exit_code: str = "-"
    observed_at: float
    source: SchedulerSource


class RunSummary(StrictModel):
    total: int = 0
    counts: dict[str, int] = Field(default_factory=dict)

    def format_counts(self) -> str:
        return " ".join(f"{state}={count}" for state, count in sorted(self.counts.items())) or "no tasks"


class TaskStatusView(StrictModel):
    task_id: str
    name: str
    local_state: str
    scheduler_state: str
    effective_state: str
    scheduler_reason: str
    failure_reason: str
    display_reason: str | None
    exit_code: int | str | None
    observed_at: float | None
    is_stale: bool


class RunStatusSnapshot(StrictModel):
    run_id: str
    tasks: list[TaskStatusView]
    summary: RunSummary
    refreshed_at: float | None
    sources: list[SchedulerSource]
    is_stale: bool
    refresh_failed_at: float | None
    refresh_error: StructuredError | None
    env_dependency_state: str = ""
    env_dependency_reason: str = ""
