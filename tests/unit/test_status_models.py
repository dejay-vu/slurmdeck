from __future__ import annotations

import pytest
from pydantic import ValidationError

from slurmdeck.models.status import (
    RunStatusSnapshot,
    RunSummary,
    SchedulerObservation,
    SchedulerSource,
    TaskStatusView,
)


def test_scheduler_observation_is_typed_and_strict() -> None:
    observation = SchedulerObservation(
        job_id="999",
        array_task_id="2",
        scheduler_state="PENDING",
        scheduler_reason="Priority",
        observed_at=123.5,
        source=SchedulerSource.SQUEUE,
    )

    assert observation.exit_code == "-"
    assert observation.source is SchedulerSource.SQUEUE
    with pytest.raises(ValidationError):
        SchedulerObservation.model_validate(observation.model_dump() | {"unexpected": True})


def test_run_status_snapshot_carries_freshness_and_structured_views() -> None:
    task = TaskStatusView(
        task_id="002",
        name="seed=2",
        local_state="FAILED",
        scheduler_state="",
        effective_state="FAILED",
        scheduler_reason="",
        failure_reason="command exited with code 3",
        display_reason="command exited with code 3",
        exit_code=3,
        observed_at=None,
        is_stale=True,
    )

    snapshot = RunStatusSnapshot(
        run_id="run-1",
        tasks=[task],
        summary=RunSummary(total=1, counts={"FAILED": 1}),
        refreshed_at=120.0,
        sources=[SchedulerSource.SACCT],
        is_stale=True,
        refresh_failed_at=130.0,
        refresh_error=None,
    )

    assert snapshot.tasks[0].display_reason == "command exited with code 3"
    assert snapshot.sources == [SchedulerSource.SACCT]
