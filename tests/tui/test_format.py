"""Pure presentation tests — no Textual app required."""

from __future__ import annotations

import pytest

from slurmdeck.models.resources import Resources
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.status import RunSummary, TaskStatusView
from slurmdeck.storage.repos import RunRow
from slurmdeck.tui import format as fmt
from slurmdeck.tui.filters import TaskFilter, run_matches, task_matches
from slurmdeck.tui.screens.run_detail import _cells

NOW = fmt.parse_utc("2026-07-03T12:00:00Z")
assert NOW is not None


class TestTimestamps:
    def test_parse_utc_roundtrip(self):
        assert fmt.parse_utc("2026-07-03T11:59:00Z") == NOW - 60

    def test_parse_rejects_garbage_and_empty(self):
        assert fmt.parse_utc("") is None
        assert fmt.parse_utc("yesterday") is None

    def test_age_buckets(self):
        assert fmt.age("2026-07-03T11:59:18Z", now=NOW) == "42s"
        assert fmt.age("2026-07-03T11:57:00Z", now=NOW) == "3m"
        assert fmt.age("2026-07-03T09:55:00Z", now=NOW) == "2h05m"
        assert fmt.age("2026-06-28T12:00:00Z", now=NOW) == "5d"
        assert fmt.age("", now=NOW) == "-"

    def test_age_never_negative(self):
        assert fmt.age("2026-07-03T12:01:00Z", now=NOW) == "0s"

    def test_duration_running_task_measures_against_now(self):
        assert fmt.duration("2026-07-03T11:58:00Z", "", now=NOW) == "2m"

    def test_duration_finished_task(self):
        assert fmt.duration("2026-07-03T11:00:00Z", "2026-07-03T11:30:00Z", now=NOW) == "30m"
        assert fmt.duration("", "", now=NOW) == "-"


class TestStateRendering:
    def test_known_states_are_styled(self):
        assert fmt.state_text("COMPLETED").style == "state.success"
        assert fmt.state_text("FAILED").style == "state.failure"
        assert fmt.state_text("RUNNING").style == "state.active"
        assert fmt.state_text("planned").style == "state.pending"

    def test_unknown_state_unstyled_and_empty_becomes_dash(self):
        assert fmt.state_text("WEIRD").plain == "WEIRD"
        assert fmt.state_text("").plain == "-"

    def test_summary_aggregates_into_groups(self):
        summary = RunSummary(total=8, counts={"COMPLETED": 4, "RUNNING": 2, "FAILED": 1, "CANCELLED": 1})
        assert fmt.summary_text(summary).plain == "✔4 ▶2 ✗1 ⊘1"

    def test_summary_unknown_states_bucketed_as_other(self):
        summary = RunSummary(total=2, counts={"UNKNOWN": 2})
        assert fmt.summary_text(summary).plain == "?2"

    def test_summary_empty(self):
        assert fmt.summary_text(RunSummary()).plain == "no tasks"

    def test_staleness(self):
        assert fmt.staleness(None) == "not refreshed yet"
        assert fmt.staleness(NOW - 8, now=NOW) == "last success 8s ago"

    def test_elapsed_feedback(self):
        assert fmt.elapsed(0.42) == "0.4s"
        assert fmt.elapsed(65) == "1m"


def _task(
    *,
    local_state: str = "UNKNOWN",
    scheduler_state: str = "",
    effective_state: str = "PENDING",
    scheduler_reason: str = "",
    failure_reason: str = "",
    display_reason: str | None = None,
    exit_code: int | str | None = None,
) -> TaskStatusView:
    return TaskStatusView(
        task_id="003",
        name="lr=0.1",
        local_state=local_state,
        scheduler_state=scheduler_state,
        effective_state=effective_state,
        scheduler_reason=scheduler_reason,
        failure_reason=failure_reason,
        display_reason=display_reason,
        exit_code=exit_code,
        observed_at=None,
        is_stale=False,
    )


class TestTaskFilter:
    def test_cycle_order(self):
        assert TaskFilter.ALL.next() is TaskFilter.ACTIVE
        assert TaskFilter.ACTIVE.next() is TaskFilter.FAILED
        assert TaskFilter.FAILED.next() is TaskFilter.ALL

    def test_failed_filter_uses_effective_state(self):
        assert not task_matches(
            _task(local_state="FAILED", scheduler_state="RUNNING", effective_state="RUNNING"),
            TaskFilter.FAILED,
        )
        assert not task_matches(_task(local_state="COMPLETED", effective_state="COMPLETED"), TaskFilter.FAILED)
        assert task_matches(_task(local_state="KILLED", effective_state="KILLED"), TaskFilter.FAILED)
        assert task_matches(_task(scheduler_state="TIMEOUT", effective_state="TIMEOUT"), TaskFilter.FAILED)

    def test_active_filter(self):
        assert task_matches(_task(scheduler_state="PENDING", effective_state="PENDING"), TaskFilter.ACTIVE)
        assert task_matches(_task(local_state="RUNNING", effective_state="RUNNING"), TaskFilter.ACTIVE)
        assert task_matches(_task(), TaskFilter.ACTIVE)
        assert not task_matches(_task(local_state="COMPLETED", effective_state="COMPLETED"), TaskFilter.ACTIVE)

    def test_search_matches_id_name_state_reason(self):
        row = _task(
            local_state="FAILED",
            effective_state="FAILED",
            failure_reason="exit code 3",
            display_reason="exit code 3",
        )
        assert task_matches(row, TaskFilter.ALL, "003")
        assert task_matches(row, TaskFilter.ALL, "LR=0.1")
        assert task_matches(row, TaskFilter.ALL, "exit code")
        assert not task_matches(row, TaskFilter.ALL, "nomatch")

    def test_run_detail_cells_display_view_reason_without_deriving_state(self):
        pending = _task(
            local_state="FAILED",
            scheduler_state="PENDING",
            effective_state="PENDING",
            scheduler_reason="Priority",
            failure_reason="old failure",
            display_reason="Priority",
        )
        failed = _task(
            local_state="FAILED",
            effective_state="FAILED",
            failure_reason="command exploded",
            display_reason="command exploded",
            exit_code=3,
        )

        pending_cells = _cells(pending)
        failed_cells = _cells(failed)
        assert pending_cells[2].plain == "PENDING"
        assert pending_cells[-1].plain == "Priority"
        assert failed_cells[2].plain == "FAILED"
        assert failed_cells[3].plain == "3"
        assert failed_cells[-1].plain == "command exploded"


def test_run_search_matches_fields(sample_run_row):
    assert run_matches(sample_run_row)
    assert run_matches(sample_run_row, sample_run_row.id[:6].upper())
    assert run_matches(sample_run_row, sample_run_row.state)
    assert not run_matches(sample_run_row, "zzz-no-match")


@pytest.fixture()
def sample_run_row() -> RunRow:
    return RunRow(
        id="demo-20260703-120000",
        project_id="project-1",
        project_display_name="Research Project",
        name="demo",
        remote="hpc",
        created_at="2026-07-03T12:00:00Z",
        state="submitted",
        slurm_job_id="424242",
        remote_root="/base/runs/demo-20260703-120000",
        snapshot_hash="abc",
        env_id="",
        env_generation_id="",
        env_prefix="",
        env_attempt_id="",
        env_build_job_id="",
        env_wait_policy="",
        env_dependency_state="",
        env_dependency_reason="",
        resources=Resources(),
        command=CommandTemplateSpec(argv=["python3", "train.py"]),
        sweep_file=None,
        retry_of=None,
        submission_token="",
        submission_phase="",
        submission_error_json="{}",
        status_refreshed_at=0.0,
        status_refresh_failed_at=0.0,
        status_refresh_error_json="{}",
        status_sources_json="[]",
        scan_watermark=0.0,
        summary=RunSummary(),
    )
