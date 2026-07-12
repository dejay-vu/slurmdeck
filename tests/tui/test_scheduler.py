"""Auto-refresh policy tests (pure — no app, no network)."""

from __future__ import annotations

from slurmdeck.models.resources import Resources
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.status import RunSummary
from slurmdeck.storage.repos import RunRow
from slurmdeck.tui.controller import FAST_INTERVAL, SLOW_INTERVAL, refresh_interval


def _run(state: str, counts: dict[str, int] | None = None) -> RunRow:
    counts = counts or {}
    return RunRow(
        id=f"r-{state}",
        project_id="project-1",
        project_display_name="Research Project",
        name="r",
        remote="hpc",
        created_at="2026-07-03T12:00:00Z",
        state=state,
        slurm_job_id="1",
        remote_root="/base/runs/r",
        snapshot_hash="",
        env_id="",
        env_generation_id="",
        env_prefix="",
        env_attempt_id="",
        env_build_job_id="",
        env_wait_policy="",
        env_dependency_state="",
        env_dependency_reason="",
        resources=Resources(),
        command=CommandTemplateSpec(argv=["true"]),
        sweep_file=None,
        retry_of=None,
        submission_token="",
        submission_phase="",
        submission_error_json="{}",
        status_refreshed_at=0.0,
        status_refresh_failed_at=0.0,
        status_refresh_error_json="{}",
        status_sources_json="{}",
        scan_watermark=0.0,
        summary=RunSummary(total=sum(counts.values()), counts=counts),
    )


def test_fast_while_any_run_is_submitted():
    runs = [_run("terminal"), _run("submitted"), _run("planned")]
    assert refresh_interval(runs) == FAST_INTERVAL


def test_slow_while_cancelled_run_still_settling():
    # tasks not yet scanned (no counts) → artifacts may still arrive
    assert refresh_interval([_run("cancelled")]) == SLOW_INTERVAL
    # a task still marked RUNNING → keep polling slowly
    assert refresh_interval([_run("cancelled", {"RUNNING": 1, "KILLED": 3})]) == SLOW_INTERVAL


def test_paused_when_cancelled_run_settled():
    assert refresh_interval([_run("cancelled", {"KILLED": 2, "COMPLETED": 2})]) is None


def test_paused_when_nothing_refreshable():
    assert refresh_interval([]) is None
    assert refresh_interval([_run("planned"), _run("terminal"), _run("submit_failed")]) is None
