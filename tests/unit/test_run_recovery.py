from __future__ import annotations

import fcntl
import json
import shutil
from pathlib import Path

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.services.run_materialization import RunMaterializer
from slurmdeck.services.run_planning import RunPlanner
from slurmdeck.services.run_recovery import RecoveryReport, RunRecoveryService

COMMAND = CommandTemplateSpec(argv=["python3", "train.py"])


def _committed_plan(ctx, remote):
    plan = RunPlanner(ctx).plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote)
    RunMaterializer(ctx).commit(plan)
    return plan


def _stale_staging(paths, name: str) -> Path:
    wrapper = paths.run_staging_wrapper(name)
    (wrapper / "attempt-dead" / "run").mkdir(parents=True)
    lock = paths.run_materialization_lock(name)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    (wrapper / "attempt-dead" / "run" / "partial").write_text("partial", encoding="utf-8")
    return wrapper


def test_inspect_reports_every_recovery_state_without_changing_anything(ctx, remote):
    paths = ctx.require_project().paths
    stale = _stale_staging(paths, "stale-run")
    orphan = paths.run_dir("orphan-run")
    orphan.mkdir(parents=True)
    (orphan / "partial").write_text("orphan", encoding="utf-8")
    unmarked = _committed_plan(ctx, remote)
    paths.run_commit_marker(unmarked.run_id).unlink()
    missing = _committed_plan(ctx, remote)
    missing_bytes = paths.run_commit_marker(missing.run_id).read_bytes()
    for child in paths.run_dir(missing.run_id).iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    paths.run_dir(missing.run_id).rmdir()

    report = RunRecoveryService(ctx).inspect()

    assert report.stale_staging == ("stale-run",)
    assert report.active_staging == ()
    assert report.orphaned_run_dirs == ("orphan-run",)
    assert report.unmarked_committed_runs == (unmarked.run_id,)
    assert report.missing_run_dirs == (missing.run_id,)
    assert stale.is_dir()
    assert orphan.is_dir()
    assert not paths.run_commit_marker(unmarked.run_id).exists()
    assert not paths.run_dir(missing.run_id).exists()
    assert missing_bytes


def test_inspect_does_not_create_an_absent_database_or_repair_orphan(ctx):
    paths = ctx.require_project().paths
    paths.db_path.unlink()
    Path(f"{paths.db_path}-shm").unlink(missing_ok=True)
    Path(f"{paths.db_path}-wal").unlink(missing_ok=True)
    orphan = paths.run_dir("orphan-run")
    orphan.mkdir(parents=True)

    report = RunRecoveryService(ctx).inspect()

    assert report.orphaned_run_dirs == ("orphan-run",)
    assert orphan.is_dir()
    assert not paths.db_path.exists()


def test_reconcile_repairs_recoverable_states_and_is_idempotent(ctx, remote):
    paths = ctx.require_project().paths
    stale = _stale_staging(paths, "stale-run")
    orphan = paths.run_dir("orphan-run")
    orphan.mkdir(parents=True)
    unmarked = _committed_plan(ctx, remote)
    marker = paths.run_commit_marker(unmarked.run_id)
    marker.unlink()

    report = RunRecoveryService(ctx).reconcile()

    assert report.stale_staging == ("stale-run",)
    assert report.orphaned_run_dirs == ("orphan-run",)
    assert report.unmarked_committed_runs == (unmarked.run_id,)
    assert not stale.exists()
    assert not orphan.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["run_id"] == unmarked.run_id
    assert payload["committed_at"].endswith("Z")
    marker_bytes = marker.read_bytes()
    marker_mtime = marker.stat().st_mtime_ns

    assert RunRecoveryService(ctx).reconcile() == RecoveryReport()
    assert marker.read_bytes() == marker_bytes
    assert marker.stat().st_mtime_ns == marker_mtime


@pytest.mark.parametrize("marker_payload", ["{broken", '{"schema_version": 1, "run_id": "wrong"}'])
def test_reconcile_removes_orphaned_run_directories_with_invalid_markers(ctx, marker_payload):
    paths = ctx.require_project().paths
    orphan = paths.run_dir("orphan-run")
    orphan.mkdir(parents=True)
    paths.run_commit_marker("orphan-run").write_text(marker_payload, encoding="utf-8")

    report = RunRecoveryService(ctx).reconcile()

    assert report.orphaned_run_dirs == ("orphan-run",)
    assert not orphan.exists()


def test_reconcile_skips_an_actively_locked_staging_attempt(ctx):
    paths = ctx.require_project().paths
    wrapper = _stale_staging(paths, "active-run")
    with paths.run_materialization_lock("active-run").open("r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        inspected = RunRecoveryService(ctx).inspect()
        reconciled = RunRecoveryService(ctx).reconcile()

        assert inspected.active_staging == ("active-run",)
        assert inspected.stale_staging == ()
        assert reconciled.active_staging == ("active-run",)
        assert wrapper.is_dir()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    assert RunRecoveryService(ctx).reconcile().stale_staging == ("active-run",)
    assert not wrapper.exists()


def test_reconcile_does_not_remove_a_renamed_final_directory_while_its_attempt_is_locked(ctx):
    paths = ctx.require_project().paths
    _stale_staging(paths, "active-run")
    final_dir = paths.run_dir("active-run")
    final_dir.mkdir(parents=True)
    sentinel = final_dir / "already-renamed"
    sentinel.write_text("keep", encoding="utf-8")

    with paths.run_materialization_lock("active-run").open("r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        report = RunRecoveryService(ctx).reconcile()

        assert report.active_staging == ("active-run",)
        assert report.orphaned_run_dirs == ()
        assert sentinel.read_text(encoding="utf-8") == "keep"
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def test_reconcile_raises_structured_corruption_for_row_without_final_directory(ctx, remote):
    paths = ctx.require_project().paths
    plan = _committed_plan(ctx, remote)
    shutil.rmtree(paths.run_dir(plan.run_id))

    inspected = RunRecoveryService(ctx).inspect()
    assert inspected.missing_run_dirs == (plan.run_id,)

    with pytest.raises(UserError) as raised:
        RunRecoveryService(ctx).reconcile()

    assert raised.value.error.code == "run_state_corrupt"
    assert raised.value.error.context == {"run_ids": [plan.run_id]}
    assert not paths.run_dir(plan.run_id).exists()
