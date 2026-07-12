from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from slurmdeck.errors import SchemaVersionError, StructuredError, UserError
from slurmdeck.models.env import EnvBinding, EnvWaitPolicy
from slurmdeck.models.remote import Remote
from slurmdeck.models.resources import Resources
from slurmdeck.models.run import CommandTemplateSpec, TaskSpec
from slurmdeck.models.status import RunSummary, SchedulerObservation, SchedulerSource
from slurmdeck.storage.db import connect
from slurmdeck.storage.paths import ProjectPaths, RemoteLayout, UserPaths
from slurmdeck.storage.repos import PlannedTaskRecord, RunRepo, TaskRepo
from slurmdeck.storage.user_store import UserStore
from slurmdeck.storage.yamlio import dump_yaml_model


class TestRemoteLayout:
    def test_pure_path_math(self):
        layout = RemoteLayout("/base/")
        assert layout.run_root("r1") == "/base/runs/r1"
        assert layout.run_submission_receipt("abc") == "/base/receipts/run/abc.json"
        assert layout.run_submission_lock("abc") == "/base/locks/run/abc.lock"
        assert layout.snapshot_code_dir("abc") == "/base/snapshots/abc/code"
        assert layout.env_registry_record("e1") == "/base/envs/registry/e1.json"
        assert layout.env_generation_dir("e1", "g1") == "/base/envs/generations/e1/g1"
        assert layout.env_attempt_dir("e1", "a1") == "/base/envs/attempts/e1/a1"
        assert layout.env_inbox_dir("a1") == "/base/envs/inbox/a1"
        assert layout.env_trash_dir("e1") == "/base/envs/trash/e1"
        assert layout.env_receipt("a1") == "/base/receipts/env/a1.json"


class TestProjectPaths:
    def test_discover_walks_up(self, tmp_path):
        project = tmp_path / "proj"
        nested = project / "src" / "pkg"
        nested.mkdir(parents=True)
        assert ProjectPaths.discover(nested) is None
        ProjectPaths(project).config_path.parent.mkdir(parents=True)
        ProjectPaths(project).config_path.write_text("{}\n")
        found = ProjectPaths.discover(nested)
        assert found is not None and found.root == project

    def test_run_staging_and_commit_marker_paths(self, tmp_path):
        paths = ProjectPaths(tmp_path / "project")
        assert paths.run_staging_dir == paths.state_dir / "staging" / "runs"
        assert paths.run_staging_wrapper("run-1") == paths.run_staging_dir / "run-1"
        assert paths.run_commit_marker("run-1") == paths.run_dir("run-1") / ".committed.json"


class TestUserPaths:
    def test_long_runtime_root_uses_a_short_stable_ssh_socket_directory(self, tmp_path):
        long_runtime = tmp_path / ("nested-runtime-" * 10)
        paths = UserPaths(config_dir=tmp_path / "config", runtime_dir=long_runtime)

        socket_template = paths.ssh_control_dir / ("cm-" + "0" * 16 + "." + "x" * 16)

        assert paths.ssh_control_dir != long_runtime / "ssh"
        assert len(os.fsencode(socket_template)) <= 96
        assert (
            paths.ssh_control_dir
            == UserPaths(
                config_dir=tmp_path / "other-config",
                runtime_dir=long_runtime,
            ).ssh_control_dir
        )


class TestUserStore:
    def test_remote_creation_uses_private_config_permissions(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(mode=0o755)
        store = UserStore(UserPaths(config_dir=config_dir, runtime_dir=tmp_path / "runtime"))

        previous_umask = os.umask(0o022)
        try:
            store.add_remote(Remote(name="cluster", host="u@h", base="/base"))
        finally:
            os.umask(previous_umask)

        remote_path = config_dir / "remotes" / "cluster.yaml"
        assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(remote_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(remote_path.stat().st_mode) == 0o600

    def test_remote_crud_and_current(self, user_paths):
        store = UserStore(user_paths)
        store.add_remote(Remote(name="a", host="u@h", base="/x"))
        assert store.list_remote_names() == ["a"]
        with pytest.raises(UserError, match="already exists"):
            store.add_remote(Remote(name="a", host="u@h", base="/x"))
        store.set_current_remote("a")
        assert store.current_remote_name() == "a"
        store.remove_remote("a")
        assert store.current_remote_name() is None
        with pytest.raises(UserError, match="Unknown remote"):
            store.read_remote("a")

    def test_unknown_remote_lists_known(self, user_paths):
        store = UserStore(user_paths)
        store.add_remote(Remote(name="arc", host="u@h", base="/x"))
        with pytest.raises(UserError, match="arc"):
            store.read_remote("acr")

    def test_ui_theme_persists_without_overwriting_current_remote(self, user_paths):
        store = UserStore(user_paths)
        store.set_current_remote("cluster")

        store.set_ui_theme("monokai")

        assert store.ui_theme() == "monokai"
        assert store.current_remote_name() == "cluster"

        store.set_ui_theme(None)
        assert store.ui_theme() is None
        assert store.current_remote_name() == "cluster"

    def test_ui_theme_rejects_unsafe_values_and_ignores_corrupt_state(self, user_paths):
        store = UserStore(user_paths)
        with pytest.raises(UserError, match="Invalid UI theme"):
            store.set_ui_theme("../bad")

        user_paths.state_path.parent.mkdir(parents=True, exist_ok=True)
        user_paths.state_path.write_text("ui_theme: [light]\n", encoding="utf-8")
        assert store.ui_theme() is None

    def test_yaml_state_restricts_existing_directory_and_file_permissions(self, tmp_path):
        path = tmp_path / "config" / "remotes" / "cluster.yaml"
        path.parent.mkdir(parents=True, mode=0o755)
        path.write_text("name: stale\n", encoding="utf-8")
        path.chmod(0o644)

        previous_umask = os.umask(0o022)
        try:
            dump_yaml_model(path, Remote(name="cluster", host="u@h", base="/base"))
        finally:
            os.umask(previous_umask)

        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert UserStore(UserPaths(config_dir=tmp_path / "config")).read_remote("cluster").host == "u@h"


class TestDb:
    def test_database_and_sidecars_use_private_permissions(self, tmp_path):
        state_dir = tmp_path / ".slurmdeck"
        state_dir.mkdir(mode=0o755)
        path = state_dir / "slurmdeck.db"
        path.touch()
        path.chmod(0o644)

        previous_umask = os.umask(0o022)
        db: sqlite3.Connection | None = None
        try:
            db = connect(path)
            db.execute(
                "INSERT INTO runs "
                "(id, project_id, project_display_name, name, remote, created_at, state, resources_json, command_json) "
                "VALUES ('r1', 'p1', 'project', 'run', 'remote', '2026-01-01T00:00:00Z', 'planned', '{}', '{}')"
            )
            db.commit()
            sidecars = [
                candidate for suffix in ("-wal", "-shm") if (candidate := path.with_name(path.name + suffix)).exists()
            ]
            assert sidecars
            assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert all(stat.S_IMODE(candidate.stat().st_mode) == 0o600 for candidate in sidecars)
        finally:
            if db is not None:
                db.close()
            os.umask(previous_umask)

    def test_migration_sets_user_version(self, tmp_path):
        db = connect(tmp_path / "x.db")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1

    def test_fresh_schema_has_first_format_run_and_task_columns(self, tmp_path):
        db = connect(tmp_path / "x.db")
        run_info = {row["name"]: row for row in db.execute("PRAGMA table_info(runs)")}
        run_columns = set(run_info)
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}

        new_run_columns = {
            "project_id",
            "project_display_name",
            "env_generation_id",
            "env_prefix",
            "env_attempt_id",
            "env_build_job_id",
            "env_wait_policy",
            "env_dependency_state",
            "env_dependency_reason",
            "submission_token",
            "submission_phase",
            "submission_error_json",
            "status_refresh_failed_at",
            "status_refresh_error_json",
            "status_sources_json",
        }
        assert new_run_columns <= run_columns
        assert all(run_info[column]["notnull"] == 1 for column in new_run_columns)
        assert {
            "scheduler_job_id",
            "scheduler_array_task_id",
            "scheduler_state",
            "scheduler_exit",
            "scheduler_reason",
            "scheduler_observed_at",
            "scheduler_source",
            "artifact_state",
            "artifact_exit_code",
            "artifact_reason",
            "artifact_observed_at",
            "started_at",
            "ended_at",
            "updated_at",
        } <= task_columns
        assert {"slurm_state", "slurm_exit", "state", "exit_code", "reason"}.isdisjoint(task_columns)

        _insert_run(db)
        assert db.execute("SELECT status_sources_json FROM runs WHERE id = 'r1'").fetchone()[0] == "[]"

    def test_newer_schema_rejected(self, tmp_path):
        path = tmp_path / "x.db"
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA user_version = 99")
        raw.commit()
        raw.close()
        with pytest.raises(SchemaVersionError):
            connect(path)


def _insert_run(db, run_id="r1", *, transaction=None):
    RunRepo(db).insert(
        run_id=run_id,
        project_id="project-1",
        project_display_name="Research Project",
        name=run_id,
        remote="cluster",
        created_at=f"2026-01-01T00:00:0{run_id[-1]}Z",
        state="planned",
        remote_root=f"/base/runs/{run_id}",
        snapshot_hash="abc",
        env_id="",
        resources=Resources(),
        command=CommandTemplateSpec(argv=["x"]),
        sweep_file=None,
        retry_of=None,
        transaction=transaction,
    )


def _task(index, task_id):
    return PlannedTaskRecord(
        spec=TaskSpec(index=index, task_id=task_id, name=f"t{task_id}", argv=["x"], result_dir=f"results/{task_id}"),
        params={"seed": index},
        args_template=None,
        env_template={},
        arg_style="posix",
    )


class TestRepos:
    def test_submission_claim_and_updates_are_guarded_by_the_full_token(self, tmp_path):
        db = connect(tmp_path / "x.db")
        _insert_run(db)
        repo = RunRepo(db)

        assert repo.begin_submission("r1", token="a" * 64, phase="validate") is True
        assert repo.begin_submission("r1", token="b" * 64, phase="validate") is False
        claimed = repo.get("r1")
        assert claimed is not None
        assert claimed.state == "submitting"
        assert claimed.submission_token == "a" * 64
        assert repo.set_submission_phase("r1", token="b" * 64, phase="submit") is False
        assert repo.set_submission_phase("r1", token="a" * 64, phase="submit") is True

        error = StructuredError(code="run_submit_unknown", summary="unknown")
        assert (
            repo.record_submission_error(
                "r1",
                token="b" * 64,
                state="submit_unknown",
                phase="submit",
                error=error,
            )
            is False
        )
        assert repo.get("r1").state == "submitting"
        assert (
            repo.record_submission_error(
                "r1",
                token="a" * 64,
                state="submit_unknown",
                phase="submit",
                error=error,
            )
            is True
        )
        assert repo.get("r1").state == "submit_unknown"

    def test_planned_run_and_tasks_accept_one_rollbackable_outer_transaction(self, tmp_path):
        path = tmp_path / "x.db"
        writer = connect(path)
        reader = connect(path)
        writer.execute("BEGIN IMMEDIATE")
        _insert_run(writer, transaction=writer)
        TaskRepo(writer).insert_planned("r1", [_task(0, "000")], transaction=writer)

        assert RunRepo(writer).get("r1") is not None
        assert RunRepo(writer).get("r1").summary == RunSummary(total=1, counts={"PENDING": 1})
        assert RunRepo(reader).get("r1") is None

        writer.rollback()
        assert RunRepo(writer).get("r1") is None
        assert RunRepo(reader).get("r1") is None

    def test_run_round_trip_includes_first_format_fields(self, tmp_path):
        db = connect(tmp_path / "x.db")
        RunRepo(db).insert(
            run_id="r1",
            project_id="project-1",
            project_display_name="Research Project",
            name="r1",
            remote="cluster",
            created_at="2026-01-01T00:00:00Z",
            state="planned",
            remote_root="/base/runs/r1",
            snapshot_hash="abc",
            env_id="env-1",
            env_generation_id="generation-1",
            env_prefix="/base/envs/generations/env-1/generation-1",
            env_attempt_id="attempt-1",
            env_build_job_id="42",
            env_wait_policy="ready",
            env_dependency_state="ready",
            env_dependency_reason="environment available",
            submission_token="token-1",
            submission_phase="planned",
            submission_error_json='{"message":"none"}',
            status_refresh_failed_at=123.5,
            status_refresh_error_json='{"message":"none"}',
            status_sources_json='["sacct"]',
            resources=Resources(),
            command=CommandTemplateSpec(argv=["x"]),
            sweep_file=None,
            retry_of=None,
        )

        row = RunRepo(db).get("r1")

        assert row is not None
        assert row.project_id == "project-1"
        assert row.project_display_name == "Research Project"
        assert row.env_generation_id == "generation-1"
        assert row.env_prefix == "/base/envs/generations/env-1/generation-1"
        assert row.env_attempt_id == "attempt-1"
        assert row.env_build_job_id == "42"
        assert row.env_wait_policy == "ready"
        assert row.env_binding == EnvBinding(
            env_id="env-1",
            generation_id="generation-1",
            prefix="/base/envs/generations/env-1/generation-1",
            attempt_id="attempt-1",
            build_job_id="42",
            wait_policy=EnvWaitPolicy.READY,
        )
        assert row.env_dependency_state == "ready"
        assert row.env_dependency_reason == "environment available"
        assert row.submission_token == "token-1"
        assert row.submission_phase == "planned"
        assert row.submission_error_json == '{"message":"none"}'
        assert row.status_refresh_failed_at == 123.5
        assert row.status_refresh_error_json == '{"message":"none"}'
        assert row.status_sources_json == '["sacct"]'

    def test_run_round_trip_and_latest(self, tmp_path):
        db = connect(tmp_path / "x.db")
        _insert_run(db, "r1")
        _insert_run(db, "r2")
        repo = RunRepo(db)
        assert repo.latest().id == "r2"
        row = repo.get("r1")
        assert row.resources == Resources()
        assert row.command.argv == ["x"]
        assert [r.id for r in repo.list()] == ["r2", "r1"]

    def test_scheduler_observation_round_trip_is_complete_and_incremental(self, tmp_path):
        db = connect(tmp_path / "x.db")
        _insert_run(db)
        tasks = TaskRepo(db)
        tasks.insert_planned("r1", [_task(0, "000"), _task(1, "001")])

        observation = SchedulerObservation(
            job_id="999",
            array_task_id="0",
            scheduler_state="PENDING",
            scheduler_reason="Priority",
            exit_code="-",
            observed_at=123.5,
            source=SchedulerSource.SQUEUE,
        )
        changed = tasks.apply_scheduler("r1", {"000": observation})
        assert changed == 1
        assert tasks.apply_scheduler("r1", {"000": observation}) == 0

        stored = db.execute("SELECT * FROM tasks WHERE run_id = 'r1' AND task_id = '000'").fetchone()
        assert stored["scheduler_state"] == "PENDING"
        assert stored["scheduler_job_id"] == "999"
        assert stored["scheduler_array_task_id"] == "0"
        assert stored["scheduler_exit"] == "-"
        assert stored["scheduler_reason"] == "Priority"
        assert stored["scheduler_observed_at"] == 123.5
        assert stored["scheduler_source"] == "squeue"
        record = {row.task_id: row for row in tasks.status_records("r1")}["000"]
        assert record.scheduler_job_id == "999"
        assert record.scheduler_array_task_id == "0"

        records = [{"task_id": "000", "state": "COMPLETED", "exit_code": 0, "mtime": 125.0}]
        assert tasks.apply_artifact("r1", records) == 1
        assert tasks.apply_artifact("r1", records) == 0
        assert (
            db.execute("SELECT artifact_observed_at FROM tasks WHERE run_id = 'r1' AND task_id = '000'").fetchone()[0]
            == 125.0
        )

    def test_older_scheduler_and_artifact_observations_are_rejected(self, tmp_path):
        db = connect(tmp_path / "x.db")
        _insert_run(db)
        tasks = TaskRepo(db)
        tasks.insert_planned("r1", [_task(0, "000")])
        newest = SchedulerObservation(
            job_id="999",
            array_task_id="0",
            scheduler_state="COMPLETED",
            scheduler_reason="None",
            exit_code="0:0",
            observed_at=200.0,
            source=SchedulerSource.SACCT,
        )
        older = SchedulerObservation(
            job_id="999",
            array_task_id="0",
            scheduler_state="RUNNING",
            scheduler_reason="node01",
            observed_at=100.0,
            source=SchedulerSource.SQUEUE,
        )

        assert tasks.apply_scheduler("r1", {"000": newest}) == 1
        assert tasks.apply_scheduler("r1", {"000": older}) == 0
        assert (
            tasks.apply_artifact(
                "r1",
                [{"task_id": "000", "state": "COMPLETED", "exit_code": 0, "reason": "", "mtime": 200.0}],
            )
            == 1
        )
        assert (
            tasks.apply_artifact(
                "r1",
                [{"task_id": "000", "state": "RUNNING", "exit_code": None, "reason": "old", "mtime": 100.0}],
            )
            == 0
        )

        stored = db.execute("SELECT * FROM tasks WHERE run_id = 'r1' AND task_id = '000'").fetchone()
        assert stored["scheduler_state"] == "COMPLETED"
        assert stored["scheduler_observed_at"] == 200.0
        assert stored["artifact_state"] == "COMPLETED"
        assert stored["artifact_observed_at"] == 200.0

    def test_run_refresh_success_and_failure_metadata_round_trip(self, tmp_path):
        db = connect(tmp_path / "x.db")
        _insert_run(db)
        runs = RunRepo(db)
        summary = RunSummary(total=1, counts={"RUNNING": 1})

        runs.record_refresh_success(
            "r1",
            summary=summary,
            sources=[SchedulerSource.SQUEUE, SchedulerSource.SACCT],
            refreshed_at=123.0,
            scan_watermark=100.0,
        )
        successful = runs.get("r1")
        assert successful is not None
        assert successful.summary == summary
        assert successful.status_refreshed_at == 123.0
        assert successful.status_sources_json == '["squeue", "sacct"]'
        assert successful.status_refresh_failed_at == 0.0
        assert successful.status_refresh_error_json == "{}"

        error = UserError("scheduler unavailable").error
        runs.record_refresh_failure("r1", failed_at=130.0, error=error)
        failed = runs.get("r1")
        assert failed is not None
        assert failed.summary == summary
        assert failed.status_refreshed_at == 123.0
        assert failed.status_sources_json == '["squeue", "sacct"]'
        assert failed.status_refresh_failed_at == 130.0
        assert '"summary":"scheduler unavailable"' in failed.status_refresh_error_json

    def test_planned_records_round_trip(self, tmp_path):
        db = connect(tmp_path / "x.db")
        _insert_run(db)
        tasks = TaskRepo(db)
        tasks.insert_planned("r1", [_task(0, "000")])
        record = tasks.planned_records("r1")[0]
        assert record.params == {"seed": 0}
        assert record.spec.task_id == "000"
        assert record.arg_style == "posix"

    def test_status_records_return_stored_facts_without_deriving_state(self, tmp_path):
        db = connect(tmp_path / "x.db")
        _insert_run(db)
        tasks = TaskRepo(db)
        tasks.insert_planned("r1", [_task(0, "000"), _task(1, "001")])
        tasks.apply_artifact("r1", [{"task_id": "001", "state": "FAILED", "exit_code": 1}])
        records = {row.task_id: row for row in tasks.status_records("r1")}
        assert records["000"].artifact_state == "UNKNOWN"
        assert records["001"].artifact_state == "FAILED"
