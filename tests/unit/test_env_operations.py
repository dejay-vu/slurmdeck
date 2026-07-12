from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.env import (
    EnvBackend,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvOwnership,
)
from slurmdeck.services.env_execution import EnvironmentExecutorClient
from slurmdeck.services.env_lifecycle import EnvironmentLifecycleService
from slurmdeck.services.env_registry import EnvRegistryClient
from slurmdeck.storage.paths import RemoteLayout
from tests.unit.test_env_executors import _fake_conda, _prepare, _profile, _project


def _queued(fake_transport, remote, remote_root, project_dir, *, name: str = "ml"):
    project = _project(project_dir)
    assert project.env is not None
    project.env.name = name
    return _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=_profile("conda"),
        project=project,
    )


def _ready(fake_transport, remote, remote_root, project_dir):
    conda = _fake_conda(remote_root / "fake-conda")
    prepared = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=_profile(str(conda)),
        project=_project(project_dir),
    )
    attempt = prepared.record.attempts[-1]
    return (
        EnvironmentExecutorClient()
        .build(
            fake_transport,
            RemoteLayout(str(remote_root)),
            prepared.record.env_id,
            attempt.attempt_id,
        )
        .record
    )


def _run_reference(remote_root: Path, record: EnvironmentRecord, *, run_id: str = "run-1") -> None:
    run = remote_root / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "project-a",
                "project_display_name": "Research A",
                "run_id": run_id,
                "env_binding": {
                    "env_id": record.env_id,
                    "generation_id": record.active_generation or record.attempts[-1].generation_id,
                    "prefix": record.active_prefix or record.attempts[-1].prefix,
                    "attempt_id": record.current_attempt or record.attempts[-1].attempt_id,
                    "build_job_id": record.attempts[-1].job_id,
                    "wait_policy": "ready",
                },
            }
        ),
        encoding="utf-8",
    )


class TestEnvironmentScanning:
    def test_list_batches_registry_scheduler_accounting_and_references_in_one_helper_call(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        queued = _queued(fake_transport, remote, remote_root, project_dir).record
        attempt = queued.attempts[-1]
        _run_reference(remote_root, queued)
        fake_transport.squeue_output = f"{attempt.job_id}|RUNNING|Resources\n"
        fake_transport.sacct_output = ""
        fake_transport.calls.clear()

        views = EnvironmentLifecycleService().list(
            fake_transport,
            RemoteLayout(str(remote_root)),
            desired_env_id=queued.env_id,
        )

        assert len(fake_transport.calls) == 1
        assert fake_transport.calls[0].startswith("python3 - scan")
        assert len(views) == 1
        assert views[0].record.status is EnvironmentStatus.BUILDING
        assert views[0].record.attempts[-1].scheduler_state == "RUNNING"
        assert views[0].record.attempts[-1].scheduler_reason == "Resources"
        assert views[0].references == ["run:project-a/run-1"]
        assert views[0].reference_count == 1
        assert views[0].desired_by_project is True

    def test_accounting_terminal_state_is_visible_when_build_helper_did_not_finish(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        queued = _queued(fake_transport, remote, remote_root, project_dir).record
        attempt = queued.attempts[-1]
        fake_transport.squeue_output = ""
        fake_transport.sacct_output = f"{attempt.job_id}|FAILED|1:0\n"

        view = EnvironmentLifecycleService().status(
            fake_transport,
            RemoteLayout(str(remote_root)),
            queued.env_id,
        )

        assert view.record.status is EnvironmentStatus.FAILED
        assert view.record.attempts[-1].scheduler_state == "FAILED"
        assert view.record.attempts[-1].error_code == "ENV_BUILD_FAILED"

    def test_unknown_environment_is_not_reported_as_empty_success(self, fake_transport, remote_root):
        service = EnvironmentLifecycleService()
        layout = RemoteLayout(str(remote_root))
        with pytest.raises(UserError, match="not found"):
            service.show(fake_transport, layout, "missing-000000000000")
        with pytest.raises(UserError, match="not found"):
            service.remove(fake_transport, layout, "missing-000000000000")


class TestEnvironmentLogsAndCancellation:
    def test_failed_environment_logs_default_to_nonempty_stderr(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        conda = _fake_conda(remote_root / "fake-conda", create_error="solver exploded")
        prepared = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile(str(conda)),
            project=_project(project_dir),
        )
        attempt = prepared.record.attempts[-1]
        failed = (
            EnvironmentExecutorClient()
            .build(
                fake_transport,
                RemoteLayout(str(remote_root)),
                prepared.record.env_id,
                attempt.attempt_id,
            )
            .record
        )

        log = EnvironmentLifecycleService().logs(
            fake_transport,
            RemoteLayout(str(remote_root)),
            failed.env_id,
            lines=20,
        )

        assert log.stream == "stderr"
        assert log.attempt_id == attempt.attempt_id
        assert "solver exploded" in log.text
        assert log.path == attempt.stderr_path

    def test_cancel_persists_terminal_state_and_second_cancel_is_actionable(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        queued = _queued(fake_transport, remote, remote_root, project_dir).record
        service = EnvironmentLifecycleService()

        cancelled = service.cancel(fake_transport, RemoteLayout(str(remote_root)), queued.env_id)

        assert cancelled.status is EnvironmentStatus.CANCELLED
        assert cancelled.current_attempt is None
        assert cancelled.attempts[-1].status is EnvironmentStatus.CANCELLED
        assert cancelled.attempts[-1].scheduler_state == "CANCELLED"
        with pytest.raises(UserError, match="no active attempt"):
            service.cancel(fake_transport, RemoteLayout(str(remote_root)), queued.env_id)


class TestEnvironmentRemovalAndGc:
    def test_managed_remove_refuses_references_then_moves_to_trash_and_finishes_in_background(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        ready = _ready(fake_transport, remote, remote_root, project_dir)
        assert ready.active_prefix is not None
        prefix = Path(ready.active_prefix)
        _run_reference(remote_root, ready)
        service = EnvironmentLifecycleService()
        layout = RemoteLayout(str(remote_root))

        with pytest.raises(UserError, match="run:project-a/run-1"):
            service.remove(fake_transport, layout, ready.env_id)
        assert prefix.is_dir()

        removing = service.remove(fake_transport, layout, ready.env_id, force=True)
        assert removing.record.status is EnvironmentStatus.REMOVING
        assert not prefix.exists()

        deadline = time.monotonic() + 5
        status = removing.record.status
        while time.monotonic() < deadline and status is EnvironmentStatus.REMOVING:
            time.sleep(0.05)
            status = service.status(fake_transport, layout, ready.env_id).record.status
        assert status is EnvironmentStatus.REMOVED

    def test_external_remove_unregisters_without_deleting_user_prefix(self, fake_transport, remote_root):
        prefix = remote_root / "user-owned-prefix"
        prefix.mkdir(parents=True)
        full_hash = "e" * 64
        record = EnvironmentRecord(
            env_id=f"external-{full_hash[:12]}",
            full_hash=full_hash,
            backend=EnvBackend.EXISTING,
            ownership=EnvOwnership.EXTERNAL,
            status=EnvironmentStatus.READY,
            active_prefix=str(prefix),
            created_at="2026-07-11T00:00:00Z",
            updated_at="2026-07-11T00:00:00Z",
            verified_at="2026-07-11T00:00:00Z",
            provenance=EnvironmentProvenance(canonical_spec_hash=full_hash),
        )
        layout = RemoteLayout(str(remote_root))
        EnvRegistryClient().prepare(fake_transport, layout, record)

        removed = EnvironmentLifecycleService().remove(fake_transport, layout, record.env_id)

        assert removed.external_unregistered is True
        assert removed.record.status is EnvironmentStatus.REMOVED
        assert prefix.is_dir()
        assert EnvRegistryClient().inspect(fake_transport, layout) == []

    def test_gc_is_dry_run_by_default_and_never_touches_unknown_legacy_directories(
        self,
        fake_transport,
        remote_root,
    ):
        layout = RemoteLayout(str(remote_root))
        trash = Path(layout.env_trash_dir("old-aaaaaaaaaaaa")) / "gen-old"
        trash.mkdir(parents=True)
        (trash / "payload").write_text("obsolete", encoding="utf-8")
        legacy = remote_root / "envs" / "legacy-env" / "env.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("{}", encoding="utf-8")
        service = EnvironmentLifecycleService()

        preview = service.gc(fake_transport, layout)

        assert preview.dry_run is True
        assert any(candidate.kind == "trash" and "gen-old" in candidate.path for candidate in preview.candidates)
        assert trash.is_dir()
        applied = service.gc(fake_transport, layout, delete=True)
        assert any("gen-old" in path for path in applied.deleted)
        assert not trash.exists()
        assert legacy.is_file()

    def test_gc_deletes_unpublished_prefix_left_by_a_terminal_failed_attempt(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        layout = RemoteLayout(str(remote_root))
        record = _queued(fake_transport, remote, remote_root, project_dir).record
        attempt = record.attempts[-1]
        partial_prefix = Path(attempt.prefix)
        partial_prefix.mkdir(parents=True)
        (partial_prefix / "partial-package").write_text("incomplete", encoding="utf-8")
        attempt.status = EnvironmentStatus.FAILED
        attempt.error_code = "ENV_BUILD_FAILED"
        attempt.error_summary = "injected failure"
        record.status = EnvironmentStatus.FAILED
        record.current_attempt = None
        Path(layout.env_registry_record(record.env_id)).write_text(record.model_dump_json(), encoding="utf-8")
        service = EnvironmentLifecycleService()

        preview = service.gc(fake_transport, layout)

        assert any(
            candidate.kind == "failed_generation" and candidate.path == str(partial_prefix)
            for candidate in preview.candidates
        )
        assert partial_prefix.is_dir()

        applied = service.gc(fake_transport, layout, delete=True)

        assert str(partial_prefix) in applied.deleted
        assert not partial_prefix.exists()
