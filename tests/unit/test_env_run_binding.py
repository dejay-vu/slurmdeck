from __future__ import annotations

import json
from pathlib import Path

import pytest

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.cluster import InvalidDependencyPolicy
from slurmdeck.models.env import EnvironmentStatus, EnvWaitPolicy
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.env_binding import EnvironmentRunBindingService
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_execution import EnvironmentExecutorClient
from slurmdeck.services.env_registry import EnvRegistryClient
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import StatusService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.storage.yamlio import dump_yaml_model
from tests.unit.test_env_executors import _fake_conda, _prepare, _profile, _project

COMMAND = CommandTemplateSpec(argv=["/usr/bin/true"])


def _configured(
    ctx,
    remote,
    remote_root,
    project_dir,
    fake_transport,
    *,
    ready: bool,
    invalid_policy: InvalidDependencyPolicy = InvalidDependencyPolicy.PER_JOB,
):
    conda = _fake_conda(remote_root / "fake-conda")
    project = _project(project_dir)
    profile = _profile(str(conda))
    profile.slurm.kill_invalid_dependency = invalid_policy
    configured_remote = remote.model_copy(update={"cluster": profile})
    dump_yaml_model(ctx.user_paths.remotes_dir / "cluster.yaml", configured_remote)
    assert ctx.project is not None
    ctx.project.config = project
    prepared = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=profile,
        project=project,
    )
    record = prepared.record
    if ready:
        attempt = record.attempts[-1]
        record = (
            EnvironmentExecutorClient()
            .build(
                fake_transport,
                RemoteLayout(str(remote_root)),
                record.env_id,
                attempt.attempt_id,
            )
            .record
        )
    return configured_remote, project, record


def _binding(fake_transport, remote_root, configured_remote, project, project_dir, policy):
    return EnvironmentRunBindingService().resolve(
        transport=fake_transport,
        remote=configured_remote,
        layout=RemoteLayout(str(remote_root)),
        project=project,
        project_dir=project_dir,
        wait_policy=policy,
    )


class TestReadyEnvironmentBinding:
    def test_run_manifest_db_activation_and_submission_pin_exact_ready_generation(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
    ):
        configured_remote, project, record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=True,
        )
        binding = _binding(
            fake_transport,
            remote_root,
            configured_remote,
            project,
            project_dir,
            EnvWaitPolicy.READY,
        )
        assert binding is not None
        assert binding.env_id == record.env_id
        assert binding.generation_id == record.active_generation
        assert binding.prefix == record.active_prefix
        assert binding.wait_policy is EnvWaitPolicy.READY

        runs = RunService(ctx)
        row = runs.plan(
            command=COMMAND,
            overrides=ResourceOverrides(),
            remote=configured_remote,
            env_binding=binding,
        )
        local_manifest = json.loads(
            (ctx.require_project().paths.run_dir(row.id) / protocol.RUN_MANIFEST_FILE).read_text(encoding="utf-8")
        )

        assert row.env_binding == binding
        assert local_manifest["env_binding"] == binding.model_dump(mode="json")
        assert "env_id" not in local_manifest
        activation = (ctx.require_project().paths.run_dir(row.id) / protocol.ACTIVATION_FILE).read_text(
            encoding="utf-8"
        )
        assert binding.prefix in activation
        assert "<generation_id>" not in activation

        fake_transport.calls.clear()
        submitted = runs.submit(fake_transport, row.id)

        assert submitted.state == "submitted"
        assert submitted.env_binding == binding
        binding_call = next(call for call in fake_transport.calls if call.startswith("python3 - binding-check"))
        assert f"--snapshot-hash {row.snapshot_hash}" in binding_call
        assert not any(call.startswith("test -f ") and "/snapshots/" in call for call in fake_transport.calls)
        sbatch_args = (remote_root / ".shims" / "sbatch.args").read_text(encoding="utf-8")
        run_line = sbatch_args.splitlines()[-1]
        assert "--dependency" not in run_line
        assert "--kill-on-invalid-dep" not in run_line

    def test_missing_bound_prefix_fails_before_snapshot_or_run_upload(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
    ):
        configured_remote, project, _record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=True,
        )
        binding = _binding(
            fake_transport,
            remote_root,
            configured_remote,
            project,
            project_dir,
            EnvWaitPolicy.READY,
        )
        assert binding is not None
        row = RunService(ctx).plan(command=COMMAND, remote=configured_remote, env_binding=binding)
        Path(binding.prefix).rename(Path(binding.prefix + ".missing"))
        fake_transport.uploads.clear()

        with pytest.raises(UserError, match="prefix"):
            RunService(ctx).submit(fake_transport, row.id)

        assert fake_transport.uploads == []
        assert not Path(row.remote_root).exists()

    def test_cached_ready_binding_plans_without_remote_round_trips(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
    ):
        configured_remote, project, record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=True,
        )
        cache = EnvironmentCache(ctx.user_paths)
        cache.remember_observation(
            configured_remote,
            ClusterCapabilityService().observe(fake_transport, configured_remote),
        )
        cache.remember_registry(configured_remote, [record])
        fake_transport.reset_metrics()

        binding = EnvironmentRunBindingService(cache=cache).resolve(
            transport=fake_transport,
            remote=configured_remote,
            layout=RemoteLayout(str(remote_root)),
            project=project,
            project_dir=project_dir,
            wait_policy=EnvWaitPolicy.READY,
        )

        assert binding is not None
        assert binding.generation_id == record.active_generation
        assert fake_transport.call_counts == {}


class TestAfterokEnvironmentBinding:
    @pytest.mark.parametrize(
        ("invalid_policy", "expect_kill_flag"),
        [
            (InvalidDependencyPolicy.PER_JOB, True),
            (InvalidDependencyPolicy.SITE_WIDE, False),
        ],
    )
    def test_active_slurm_build_submits_scheduler_ordered_run_with_profile_policy(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
        invalid_policy,
        expect_kill_flag,
    ):
        configured_remote, project, record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=False,
            invalid_policy=invalid_policy,
        )
        binding = _binding(
            fake_transport,
            remote_root,
            configured_remote,
            project,
            project_dir,
            EnvWaitPolicy.AFTEROK,
        )
        assert binding is not None
        attempt = record.attempts[-1]
        assert binding.generation_id == attempt.generation_id
        assert binding.attempt_id == attempt.attempt_id
        assert binding.build_job_id == attempt.job_id
        assert binding.wait_policy is EnvWaitPolicy.AFTEROK

        fake_transport.simulate_execution = False
        row = RunService(ctx).plan(command=COMMAND, remote=configured_remote, env_binding=binding)
        submitted = RunService(ctx).submit(fake_transport, row.id)

        assert submitted.env_binding == binding
        assert submitted.env_dependency_state == "waiting"
        args = (remote_root / ".shims" / "sbatch.args").read_text(encoding="utf-8").splitlines()[-1]
        assert f"--dependency=afterok:{attempt.job_id}" in args
        assert ("--kill-on-invalid-dep=yes" in args) is expect_kill_flag

        fake_transport.squeue_output = f"{submitted.slurm_job_id}_0|PENDING|Dependency\n"
        fake_transport.sacct_output = ""
        StatusService(ctx).refresh(fake_transport, RemoteLayout(str(remote_root)), [submitted.id])
        snapshot = StatusService(ctx).snapshot(submitted.id)
        assert snapshot.tasks[0].effective_state == "WAITING_FOR_ENV"
        assert (
            snapshot.tasks[0].display_reason == f"Waiting for environment {binding.env_id} build {binding.build_job_id}"
        )
        assert snapshot.summary.counts == {"WAITING_FOR_ENV": 1}

    def test_afterok_is_rejected_if_current_profile_no_longer_guarantees_invalid_dependency_termination(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
    ):
        configured_remote, project, _record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=False,
        )
        configured_remote.cluster.slurm.kill_invalid_dependency = InvalidDependencyPolicy.UNSUPPORTED

        with pytest.raises(UserError, match="afterok"):
            _binding(
                fake_transport,
                remote_root,
                configured_remote,
                project,
                project_dir,
                EnvWaitPolicy.AFTEROK,
            )

    @pytest.mark.parametrize(
        ("env_status", "expected"),
        [
            (EnvironmentStatus.FAILED, "ENV_BUILD_FAILED"),
            (EnvironmentStatus.CANCELLED, "ENV_BUILD_CANCELLED"),
        ],
    )
    def test_dependency_terminal_state_prevents_tasks_and_converges_run_status(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
        env_status,
        expected,
    ):
        configured_remote, project, _record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=False,
        )
        binding = _binding(
            fake_transport,
            remote_root,
            configured_remote,
            project,
            project_dir,
            EnvWaitPolicy.AFTEROK,
        )
        assert binding is not None
        fake_transport.simulate_execution = False
        row = RunService(ctx).plan(command=COMMAND, remote=configured_remote, env_binding=binding)
        row = RunService(ctx).submit(fake_transport, row.id)

        stored = EnvRegistryClient().inspect(fake_transport, RemoteLayout(str(remote_root)))[0]
        attempt = stored.attempts[-1]
        attempt.status = env_status
        attempt.error_code = expected
        attempt.error_summary = expected
        stored.status = env_status
        stored.current_attempt = None
        Path(RemoteLayout(str(remote_root)).env_registry_record(stored.env_id)).write_text(
            stored.model_dump_json(),
            encoding="utf-8",
        )
        fake_transport.squeue_output = f"{row.slurm_job_id}_0|PENDING|DependencyNeverSatisfied\n"
        fake_transport.sacct_output = ""

        StatusService(ctx).refresh(fake_transport, RemoteLayout(str(remote_root)), [row.id])
        refreshed = RunService(ctx).get(row.id)
        snapshot = StatusService(ctx).snapshot(row.id)

        assert refreshed.state == "terminal"
        assert refreshed.env_dependency_state == expected
        assert snapshot.tasks[0].effective_state == expected
        assert snapshot.summary.counts == {expected: 1}
        result_dir = Path(row.remote_root) / "results" / "000"
        assert not (result_dir / "status.json").exists()

    @pytest.mark.parametrize(
        ("scheduler_state", "expected"),
        [
            ("FAILED", "ENV_BUILD_FAILED"),
            ("TIMEOUT", "ENV_BUILD_FAILED"),
            ("CANCELLED", "ENV_BUILD_CANCELLED"),
        ],
    )
    def test_refresh_converges_from_build_scheduler_state_without_prior_registry_reconcile(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
        scheduler_state,
        expected,
    ):
        configured_remote, project, record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=False,
        )
        binding = _binding(
            fake_transport,
            remote_root,
            configured_remote,
            project,
            project_dir,
            EnvWaitPolicy.AFTEROK,
        )
        assert binding is not None
        fake_transport.simulate_execution = False
        row = RunService(ctx).plan(command=COMMAND, remote=configured_remote, env_binding=binding)
        row = RunService(ctx).submit(fake_transport, row.id)
        assert record.status is EnvironmentStatus.QUEUED

        fake_transport.squeue_output = f"{row.slurm_job_id}_0|PENDING|DependencyNeverSatisfied\n"
        fake_transport.sacct_output = f"{binding.build_job_id}|{scheduler_state}|1:0|test failure\n"
        fake_transport.calls.clear()

        StatusService(ctx).refresh(fake_transport, RemoteLayout(str(remote_root)), [row.id])
        snapshot = StatusService(ctx).snapshot(row.id)

        assert snapshot.env_dependency_state == expected
        assert snapshot.summary.counts == {expected: 1}
        scan_call = next(call for call in fake_transport.calls if call.startswith("python3 - scan"))
        assert binding.build_job_id in scan_call
        assert row.slurm_job_id in scan_call

    def test_cancelling_dependent_run_never_cancels_shared_environment_build(
        self,
        ctx,
        remote,
        remote_root,
        project_dir,
        fake_transport,
    ):
        configured_remote, project, record = _configured(
            ctx,
            remote,
            remote_root,
            project_dir,
            fake_transport,
            ready=False,
        )
        binding = _binding(
            fake_transport,
            remote_root,
            configured_remote,
            project,
            project_dir,
            EnvWaitPolicy.AFTEROK,
        )
        assert binding is not None
        fake_transport.simulate_execution = False
        row = RunService(ctx).plan(command=COMMAND, remote=configured_remote, env_binding=binding)
        row = RunService(ctx).submit(fake_transport, row.id)

        RunService(ctx).cancel(fake_transport, row.id)
        env = EnvRegistryClient().inspect(fake_transport, RemoteLayout(str(remote_root)))[0]

        assert env.env_id == record.env_id
        assert env.status is EnvironmentStatus.QUEUED
        assert env.current_attempt == binding.attempt_id
