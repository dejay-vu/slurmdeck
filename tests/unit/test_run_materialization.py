from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime

import pytest

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.services import run_materialization
from slurmdeck.services.run_materialization import RunMaterializer
from slurmdeck.services.run_planning import RunPlanner
from slurmdeck.storage.repos import RunRepo, TaskRepo

COMMAND = CommandTemplateSpec(argv=["python3", "train.py"])


def _plan(ctx, remote):
    return RunPlanner(ctx).plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote)


def _assert_absent(ctx, run_id: str) -> None:
    paths = ctx.require_project().paths
    assert RunRepo(ctx.db()).get(run_id) is None
    assert not paths.run_dir(run_id).exists()
    assert not paths.run_commit_marker(run_id).exists()
    assert not paths.run_staging_dir.exists()


def test_commit_atomically_persists_files_rows_summary_and_marker(ctx, remote):
    plan = _plan(ctx, remote)

    row = RunMaterializer(ctx).commit(plan)

    paths = ctx.require_project().paths
    run_dir = paths.run_dir(plan.run_id)
    assert row.id == plan.run_id
    assert row.state == "planned"
    assert row.summary.total == len(plan.tasks)
    assert row.summary.counts == {"PENDING": len(plan.tasks)}
    assert TaskRepo(ctx.db()).planned_records(plan.run_id) == [task.record for task in plan.tasks]
    for relative_path, expected in plan.rendered_files.items():
        assert (run_dir / relative_path).read_bytes() == expected
    marker = json.loads(paths.run_commit_marker(plan.run_id).read_text(encoding="utf-8"))
    assert marker == {
        "schema_version": 1,
        "run_id": plan.run_id,
        "committed_at": marker["committed_at"],
    }
    assert marker["committed_at"].endswith("Z")
    assert datetime.fromisoformat(marker["committed_at"].replace("Z", "+00:00")).utcoffset() is not None
    final_files = {path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()}
    assert final_files == {*plan.rendered_files, ".committed.json"}
    assert not paths.run_staging_dir.exists()


def test_commit_keeps_a_stable_unlocked_materialization_lock(ctx, remote):
    plan = _plan(ctx, remote)
    paths = ctx.require_project().paths

    RunMaterializer(ctx).commit(plan)

    lock_path = paths.run_materialization_lock(plan.run_id)
    assert lock_path.is_file()
    with lock_path.open("r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def test_marker_is_written_only_after_final_files_and_database_commit_are_visible(ctx, remote, monkeypatch):
    plan = _plan(ctx, remote)
    paths = ctx.require_project().paths
    observed = []
    real_write_marker = run_materialization._write_commit_marker

    def observe_marker(marker_path, run_id):
        assert paths.run_dir(run_id).is_dir()
        reader = sqlite3.connect(paths.db_path)
        try:
            assert reader.execute("SELECT count(*) FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == 1
            assert reader.execute("SELECT count(*) FROM tasks WHERE run_id = ?", (run_id,)).fetchone()[0] == len(
                plan.tasks
            )
        finally:
            reader.close()
        observed.append(run_id)
        real_write_marker(marker_path, run_id)

    monkeypatch.setattr(run_materialization, "_write_commit_marker", observe_marker)

    RunMaterializer(ctx).commit(plan)

    assert observed == [plan.run_id]


@pytest.mark.parametrize(
    "fault",
    ["staging_write", "run_insert", "task_insert", "rename", "db_commit", "marker_write"],
)
def test_caught_commit_failures_compensate_to_no_partial_run(ctx, remote, monkeypatch, fault):
    plan = _plan(ctx, remote)

    def boom(*_args, **_kwargs):
        raise OSError(f"injected {fault} failure")

    if fault == "staging_write":
        monkeypatch.setattr(run_materialization, "_write_file", boom)
    elif fault == "run_insert":
        monkeypatch.setattr(RunRepo, "insert", boom)
    elif fault == "task_insert":
        monkeypatch.setattr(TaskRepo, "insert_planned", boom)
    elif fault == "rename":
        monkeypatch.setattr(run_materialization, "_replace_run_directory", boom)
    elif fault == "db_commit":
        monkeypatch.setattr(run_materialization, "_commit_transaction", boom)
    else:
        monkeypatch.setattr(run_materialization, "_write_commit_marker", boom)

    with pytest.raises(OSError, match=f"injected {fault} failure"):
        RunMaterializer(ctx).commit(plan)

    _assert_absent(ctx, plan.run_id)


def test_staging_write_failure_happens_before_sqlite_is_opened(ctx, remote, monkeypatch):
    paths = ctx.require_project().paths
    paths.db_path.unlink()
    plan = _plan(ctx, remote)
    writes = 0
    real_write = run_materialization._write_file

    def fail_mid_payload(path, payload):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("injected mid-payload write failure")
        real_write(path, payload)

    monkeypatch.setattr(run_materialization, "_write_file", fail_mid_payload)

    with pytest.raises(OSError, match="mid-payload"):
        RunMaterializer(ctx).commit(plan)

    assert writes == 3
    assert not paths.db_path.exists()
    assert not paths.run_dir(plan.run_id).exists()
    assert not paths.run_staging_dir.exists()


def test_commit_never_deletes_or_replaces_a_preexisting_final_directory(ctx, remote):
    plan = _plan(ctx, remote)
    paths = ctx.require_project().paths
    final_dir = paths.run_dir(plan.run_id)
    final_dir.mkdir(parents=True)
    sentinel = final_dir / "belongs-to-another-attempt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(UserError, match="already exists"):
        RunMaterializer(ctx).commit(plan)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert RunRepo(ctx.db()).get(plan.run_id) is None
    assert not paths.run_commit_marker(plan.run_id).exists()
    assert not paths.run_staging_dir.exists()


def test_commit_revalidates_rendered_paths_before_creating_staging(ctx, remote):
    plan = _plan(ctx, remote)
    malicious = replace(plan, rendered_files={"../outside": b"escape"})
    paths = ctx.require_project().paths

    with pytest.raises(UserError, match="Unsafe rendered run path"):
        RunMaterializer(ctx).commit(malicious)

    assert not (paths.state_dir / "outside").exists()
    assert not paths.run_staging_dir.exists()
    assert RunRepo(ctx.db()).get(plan.run_id) is None


def test_staging_name_collision_never_removes_the_preexisting_attempt(ctx, remote, monkeypatch):
    plan = _plan(ctx, remote)
    paths = ctx.require_project().paths
    existing_attempt = paths.run_staging_wrapper(plan.run_id) / "attempt-collision"
    existing_attempt.mkdir(parents=True)
    sentinel = existing_attempt / "belongs-to-another-attempt"
    sentinel.write_text("keep", encoding="utf-8")

    class CollisionId:
        hex = "collision"

    monkeypatch.setattr(run_materialization.uuid, "uuid4", CollisionId)

    with pytest.raises(FileExistsError):
        RunMaterializer(ctx).commit(plan)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert RunRepo(ctx.db()).get(plan.run_id) is None
    assert not paths.run_dir(plan.run_id).exists()


def test_concurrent_commits_for_one_run_id_have_one_winner_and_never_overwrite(ctx, remote):
    first = _plan(ctx, remote)
    second_manifest = first.manifest.model_copy(
        update={"name": "second-winner", "command": CommandTemplateSpec(argv=["python3", "second.py"])}
    )
    second_record = replace(
        first.tasks[0].record,
        spec=first.tasks[0].record.spec.model_copy(update={"argv": ["python3", "second.py"]}),
    )
    second_task = replace(first.tasks[0], record=second_record)
    second_payload = dict(first.rendered_files)
    second_payload[f"{protocol.LOGS_DIR}/.keep"] = b"second-attempt"
    second_payload[protocol.RUN_MANIFEST_FILE] = (second_manifest.model_dump_json(indent=2) + "\n").encode()
    second_payload[protocol.TASKS_FILE] = (second_record.spec.model_dump_json(exclude_none=True) + "\n").encode()
    second = replace(first, manifest=second_manifest, tasks=(second_task,), rendered_files=second_payload)
    barrier = threading.Barrier(2)
    successes = []
    failures = []

    def commit(plan) -> None:
        barrier.wait(timeout=5)
        try:
            successes.append(RunMaterializer(ctx).commit(plan))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=commit, args=(plan,)) for plan in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], (UserError, OSError))
    paths = ctx.require_project().paths
    row = RunRepo(ctx.db()).get(first.run_id)
    assert row is not None
    stored_task = TaskRepo(ctx.db()).planned_records(first.run_id)[0]
    stored_manifest = json.loads((paths.run_dir(first.run_id) / protocol.RUN_MANIFEST_FILE).read_text())
    sentinel = (paths.run_dir(first.run_id) / f"{protocol.LOGS_DIR}/.keep").read_bytes()
    if row.name == "second-winner":
        assert row.command.argv == ["python3", "second.py"]
        assert stored_task.spec.argv == ["python3", "second.py"]
        assert stored_manifest["name"] == "second-winner"
        assert sentinel == b"second-attempt"
    else:
        assert row.name == first.manifest.name
        assert row.command == first.manifest.command
        assert stored_task.spec.argv == first.tasks[0].record.spec.argv
        assert stored_manifest["name"] == first.manifest.name
        assert sentinel == b""
    assert paths.run_commit_marker(first.run_id).is_file()
    assert not paths.run_staging_dir.exists()
