from __future__ import annotations

import json
import runpy
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from slurmdeck.models.env import EnvironmentRecord, EnvironmentStatus
from slurmdeck.services.env_execution import EnvironmentExecutorClient, EnvironmentPreparationService
from slurmdeck.services.env_planning import EnvironmentPlanningService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import RemoteTimeout, TransportError, parse_json_lines
from tests.unit.test_env_executors import _fake_conda, _prepare, _profile, _project
from tests.unit.test_env_registry import _record

ENV_AGENT = Path("src/slurmdeck/agent/env_agent.py").resolve()


def test_upload_failure_leaves_no_registry_attempt_or_job(
    fake_transport,
    remote,
    remote_root,
    project_dir,
) -> None:
    fake_transport.script_call("upload", TransportError("injected staging upload failure"))

    with pytest.raises(TransportError, match="staging upload"):
        _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile("conda"),
            project=_project(project_dir),
        )

    assert not (remote_root / "envs" / "registry").exists()
    assert not (remote_root / "envs" / "attempts").exists()
    assert not (remote_root / "envs" / "inbox").exists()
    assert not (remote_root / ".shims" / "sbatch.count").exists()


def test_lock_acquisition_failure_never_creates_a_registry_record(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    namespace = runpy.run_path(str(ENV_AGENT))
    command = namespace["cmd_prepare"]

    def fail_lock(*_args, **_kwargs):
        raise OSError("injected lock acquisition failure")

    monkeypatch.setattr(command.__globals__["fcntl"], "flock", fail_lock)
    result = command(SimpleNamespace(base=str(tmp_path), record_json=_record().model_dump_json()))

    payload = parse_json_lines(capsys.readouterr().out)[-1]
    assert result == 0
    assert payload["ok"] is False
    assert "lock acquisition" in payload["error"]
    assert not Path(RemoteLayout(str(tmp_path)).env_registry_record(_record().env_id)).exists()


def test_receipt_failure_is_reconciled_to_retryable_staging_without_duplicate_sbatch(
    fake_transport,
    remote,
    remote_root,
    project_dir,
    monkeypatch,
    capsys,
) -> None:
    configured = remote.model_copy(update={"cluster": _profile("conda")})
    project = _project(project_dir)
    layout = RemoteLayout(str(remote_root))
    plan = EnvironmentPlanningService().plan(
        transport=fake_transport,
        remote=configured,
        layout=layout,
        project=project,
        project_dir=project_dir,
    )
    request = EnvironmentPreparationService._request(plan, rebuild=False)
    EnvironmentPreparationService._upload_inbox(fake_transport, layout, plan, request)
    namespace = runpy.run_path(str(ENV_AGENT))
    command = namespace["cmd_prepare_build"]
    real_receipt = command.__globals__["_receipt"]

    def fail_receipt(*_args, **_kwargs):
        raise OSError("injected attempt receipt failure")

    monkeypatch.setitem(command.__globals__, "_receipt", fail_receipt)
    result = command(SimpleNamespace(base=str(remote_root), request_json=request.model_dump_json()))
    payload = parse_json_lines(capsys.readouterr().out)[-1]
    assert result == 0
    assert payload["ok"] is False
    assert "receipt failure" in payload["error"]
    staged = EnvironmentRecord.model_validate_json(
        Path(layout.env_registry_record(plan.env_id)).read_text(encoding="utf-8")
    )
    assert staged.status is EnvironmentStatus.STAGING
    assert not (remote_root / ".shims" / "sbatch.count").exists()

    monkeypatch.setitem(command.__globals__, "_receipt", real_receipt)
    candidate = EnvironmentExecutorClient().check_candidate(
        fake_transport,
        layout,
        plan.env_id,
        plan.full_hash,
    )

    assert candidate.action == "retry"
    assert candidate.record is not None
    assert candidate.record.status is EnvironmentStatus.FAILED
    assert candidate.record.last_error is not None
    assert candidate.record.last_error.code == "ENV_STAGING_INTERRUPTED"

    retried = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=_profile("conda"),
        project=project,
    )
    assert retried.record.status is EnvironmentStatus.QUEUED
    assert len(retried.record.attempts) == 2
    assert (remote_root / ".shims" / "sbatch.count").read_text(encoding="utf-8").strip() == "1"


def test_lost_prepare_response_reattaches_and_never_resubmits(
    fake_transport,
    remote,
    remote_root,
    project_dir,
    monkeypatch,
) -> None:
    real_exec_python = fake_transport.exec_python

    def lose_response(script, args=(), *, timeout=60.0, check=True):
        result = real_exec_python(script, args, timeout=timeout, check=check)
        if args and args[0] == "prepare-build":
            raise RemoteTimeout("lost environment prepare response")
        return result

    monkeypatch.setattr(fake_transport, "exec_python", lose_response)
    with pytest.raises(RemoteTimeout, match="lost environment prepare"):
        _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile("conda"),
            project=_project(project_dir),
        )

    monkeypatch.setattr(fake_transport, "exec_python", real_exec_python)
    attached = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=_profile("conda"),
        project=_project(project_dir),
    )

    assert attached.action.value == "attach"
    assert len(attached.record.attempts) == 1
    assert (remote_root / ".shims" / "sbatch.count").read_text(encoding="utf-8").strip() == "1"


def test_sbatch_rejection_is_persisted_in_registry_and_receipt(
    fake_transport,
    remote,
    remote_root,
    project_dir,
) -> None:
    fake_transport.sbatch_returncode = 9
    fake_transport.sbatch_stderr = "injected scheduler rejection\n"

    failed = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=_profile("conda"),
        project=_project(project_dir),
    )

    attempt = failed.record.attempts[-1]
    assert failed.record.status is EnvironmentStatus.FAILED
    assert failed.record.last_error is not None
    assert failed.record.last_error.code == "ENV_SUBMIT_FAILED"
    receipt = json.loads(Path(RemoteLayout(str(remote_root)).env_receipt(attempt.attempt_id)).read_text())
    assert receipt["state"] == "failed"
    assert receipt["error_code"] == "ENV_SUBMIT_FAILED"
    assert (remote_root / ".shims" / "sbatch.count").read_text(encoding="utf-8").strip() == "1"


def test_smoke_failure_never_publishes_a_generation(
    fake_transport,
    remote,
    remote_root,
    project_dir,
) -> None:
    conda = _fake_conda(remote_root / "fake-conda")
    prepared = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=_profile(str(conda)),
        project=_project(project_dir, smoke_test="false"),
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

    assert failed.status is EnvironmentStatus.FAILED
    assert failed.active_generation is None
    assert failed.generations == []
    assert failed.last_error is not None
    assert failed.last_error.code == "ENV_BUILD_FAILED"


def test_promotion_write_failure_never_changes_the_active_generation(
    fake_transport,
    remote,
    remote_root,
    project_dir,
    monkeypatch,
    capsys,
) -> None:
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
    layout = RemoteLayout(str(remote_root))
    registry_path = Path(layout.env_registry_record(prepared.record.env_id))
    namespace = runpy.run_path(str(ENV_AGENT))
    command = namespace["cmd_build"]
    real_atomic_write = command.__globals__["_atomic_write"]

    def fail_promotion(path, payload):
        if Path(path) == registry_path and payload.get("status") == "READY":
            raise OSError("injected promotion write failure")
        real_atomic_write(path, payload)

    monkeypatch.setitem(command.__globals__, "_atomic_write", fail_promotion)
    result = command(
        SimpleNamespace(
            base=str(remote_root),
            env_id=prepared.record.env_id,
            attempt_id=attempt.attempt_id,
        )
    )
    payload = parse_json_lines(capsys.readouterr().out)[-1]
    stored = EnvironmentRecord.model_validate_json(registry_path.read_text(encoding="utf-8"))

    assert result == 1
    assert payload["ok"] is False
    assert "promotion write" in payload["error"]
    assert stored.status is EnvironmentStatus.VERIFYING
    assert stored.active_generation is None
    assert stored.generations == []
    assert Path(attempt.prefix).is_dir()


def test_failed_login_executor_persists_terminal_state_without_sbatch(
    fake_transport,
    remote,
    remote_root,
    project_dir,
) -> None:
    conda = _fake_conda(remote_root / "fake-conda", create_error="injected login build failure")
    profile = _profile(
        str(conda),
        default="login",
        allowed=["login"],
        login_policy="allowed",
    )
    prepared = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=profile,
        project=_project(project_dir),
    )
    deadline = time.monotonic() + 10
    record = prepared.record
    while time.monotonic() < deadline and record.status not in {
        EnvironmentStatus.FAILED,
        EnvironmentStatus.CANCELLED,
    }:
        time.sleep(0.05)
        record = (
            EnvironmentExecutorClient()
            .reconcile(
                fake_transport,
                RemoteLayout(str(remote_root)),
                record.env_id,
            )
            .record
        )

    assert record.status is EnvironmentStatus.FAILED
    assert record.last_error is not None
    assert record.last_error.code == "ENV_BUILD_FAILED"
    assert not (remote_root / ".shims" / "sbatch.count").exists()


def test_remove_move_failure_is_recorded_as_unknown_without_deleting_source(
    fake_transport,
    remote,
    remote_root,
    project_dir,
    monkeypatch,
    capsys,
) -> None:
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
    ready = (
        EnvironmentExecutorClient()
        .build(
            fake_transport,
            RemoteLayout(str(remote_root)),
            prepared.record.env_id,
            attempt.attempt_id,
        )
        .record
    )
    prefix = Path(ready.active_prefix or "")
    namespace = runpy.run_path(str(ENV_AGENT))
    command = namespace["cmd_remove"]
    real_replace = command.__globals__["os"].replace

    def fail_generation_move(source, destination):
        if Path(source) == prefix:
            raise OSError("injected trash move failure")
        real_replace(source, destination)

    monkeypatch.setattr(command.__globals__["os"], "replace", fail_generation_move)
    assert command(SimpleNamespace(base=str(remote_root), env_id=ready.env_id, force=False)) == 0
    payload = parse_json_lines(capsys.readouterr().out)[-1]

    assert payload["ok"] is False
    assert "trash move failure" in payload["error"]
    stored = EnvironmentRecord.model_validate_json(
        Path(RemoteLayout(str(remote_root)).env_registry_record(ready.env_id)).read_text(encoding="utf-8")
    )
    assert stored.status is EnvironmentStatus.REMOVE_UNKNOWN
    assert stored.last_error is not None
    assert stored.last_error.code == "REMOVE_UNKNOWN"
    assert prefix.is_dir()
