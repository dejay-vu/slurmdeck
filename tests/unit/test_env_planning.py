from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from slurmdeck.models.cluster import (
    BuildExecutor,
    ClusterObservation,
    ClusterProfile,
    CondaProfile,
    LoginBuildPolicy,
    ModuleInitializationProfile,
    NetworkProfile,
    PlatformProfile,
    SharedFilesystemProfile,
    SlurmClusterProfile,
    ToolObservation,
)
from slurmdeck.models.env import (
    CondaChannelPriority,
    CondaEnvSpec,
    CondaSolver,
    EnvBackend,
    EnvBuildAttempt,
    EnvGeneration,
    EnvironmentPlanAction,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvOwnership,
    ExistingEnvSpec,
)
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.resources import ResourceOverrides, Resources
from slurmdeck.services.env_planning import EnvironmentPlanner, EnvironmentPlanningService
from slurmdeck.storage.paths import RemoteLayout


def _profile(*, network: NetworkProfile | None = None) -> ClusterProfile:
    return ClusterProfile(
        allowed_build_executors=[BuildExecutor.SLURM],
        default_build_executor=BuildExecutor.SLURM,
        login_build_policy=LoginBuildPolicy.FORBIDDEN,
        shared_filesystem=SharedFilesystemProfile(login_to_compute=True),
        module_initialization=ModuleInitializationProfile(
            strategy="commands",
            commands=["source /etc/profile.d/modules.sh"],
        ),
        conda=CondaProfile(executable="conda", modules=["Miniforge/24"]),
        network=network or NetworkProfile(compute_access="full", channel_access="direct"),
        slurm=SlurmClusterProfile(
            partition="short",
            afterok_dependency=True,
            kill_invalid_dependency="per_job",
        ),
        platform=PlatformProfile(system="Linux", machine="x86_64", conda_subdir="linux-64"),
    )


def _observation(*, max_time: str = "24:00:00", system: str = "Linux") -> ClusterObservation:
    return ClusterObservation(
        observed_at=123.0,
        python_version="3.11.9",
        tools={
            name: ToolObservation(available=True, path=f"/usr/bin/{name}", version="23.11")
            for name in ("sbatch", "squeue", "sacct", "scancel", "sinfo")
        },
        base_writable=True,
        default_partition="short",
        partitions=[{"name": "short", "is_default": True, "max_time": max_time}],
        system=system,
        machine="x86_64",
        conda_subdir="linux-64",
        conda_path="/usr/bin/conda",
        afterok_dependency_supported=True,
        kill_invalid_dependency_supported=True,
    )


def _write_environment(path: Path, *, channels: bool = True, python: str = "3.11") -> None:
    channel_text = "channels: [conda-forge, pytorch]\n" if channels else ""
    path.write_text(f"{channel_text}dependencies: [python={python}]\n", encoding="utf-8")


def _project(env: CondaEnvSpec | ExistingEnvSpec, *, resources: Resources | None = None) -> ProjectConfig:
    return ProjectConfig(
        project_id="project-1",
        display_name="research",
        resources=resources or Resources(partition="short"),
        env=env,
    )


def _plan(
    project_dir: Path,
    project: ProjectConfig,
    *,
    profile: ClusterProfile | None = None,
    observation: ClusterObservation | None = None,
    registry: list[EnvironmentRecord] | None = None,
    rebuild: bool = False,
):
    return EnvironmentPlanner().plan(
        project=project,
        project_dir=project_dir,
        layout=RemoteLayout("/remote/base"),
        profile=profile,
        observation=observation,
        registry=registry or [],
        rebuild=rebuild,
    )


def _record_for_plan(plan, *, status: EnvironmentStatus) -> EnvironmentRecord:
    provenance = EnvironmentProvenance(
        canonical_spec_hash=plan.full_hash,
        environment_file_hash=plan.environment_file_hash,
        channels=plan.channels,
        channel_priority=str(plan.channel_priority) if plan.channel_priority else None,
        solver=str(plan.solver) if plan.solver else None,
        platform=plan.platform,
    )
    if status is EnvironmentStatus.READY:
        generation = EnvGeneration(
            generation_id="gen-1",
            attempt_id="attempt-1",
            prefix=f"/remote/base/envs/generations/{plan.env_id}/gen-1",
            status=EnvironmentStatus.READY,
            created_at="2026-07-11T00:00:00Z",
            verified_at="2026-07-11T00:05:00Z",
            provenance=provenance,
        )
        return EnvironmentRecord(
            env_id=plan.env_id,
            full_hash=plan.full_hash,
            backend=plan.backend,
            ownership=plan.ownership,
            status=status,
            active_generation=generation.generation_id,
            active_prefix=generation.prefix,
            created_at="2026-07-11T00:00:00Z",
            updated_at="2026-07-11T00:05:00Z",
            verified_at="2026-07-11T00:05:00Z",
            generations=[generation],
            provenance=provenance,
        )
    attempt = EnvBuildAttempt(
        attempt_id="attempt-1",
        status=status,
        executor=BuildExecutor.SLURM,
        generation_id="gen-1",
        prefix=f"/remote/base/envs/generations/{plan.env_id}/gen-1",
        job_id="12345",
        created_at="2026-07-11T00:00:00Z",
    )
    return EnvironmentRecord(
        env_id=plan.env_id,
        full_hash=plan.full_hash,
        backend=plan.backend,
        ownership=plan.ownership,
        status=status,
        current_attempt=attempt.attempt_id,
        created_at="2026-07-11T00:00:00Z",
        updated_at="2026-07-11T00:00:00Z",
        attempts=[attempt],
        provenance=provenance,
    )


class TestCanonicalEnvironmentIdentity:
    def test_hash_covers_environment_bytes_and_policy_but_excludes_all_resources(self, tmp_path):
        _write_environment(tmp_path / "environment.yml")
        base = CondaEnvSpec(
            name="ML research / long name " + "x" * 80,
            modules=["cuda/12"],
            post_install=["pip install -e ."],
            smoke_test="python -c 'import torch'",
            channel_priority=CondaChannelPriority.STRICT,
            solver=CondaSolver.LIBMAMBA,
            build_resources=ResourceOverrides(time="01:00:00", cpus=2),
        )
        first = _plan(
            tmp_path,
            _project(base, resources=Resources(time="12:00:00", cpus=1, partition="short")),
            profile=_profile(),
            observation=_observation(),
        )
        changed_resources = base.model_copy(
            update={"build_resources": ResourceOverrides(time="04:00:00", cpus=16, mem="64G")}
        )
        second = _plan(
            tmp_path,
            _project(changed_resources, resources=Resources(time="48:00:00", cpus=8, partition="short")),
            profile=_profile(),
            observation=_observation(),
        )

        assert first.full_hash == second.full_hash
        assert first.env_id == second.env_id
        assert len(first.full_hash) == 64
        assert first.env_id.endswith("-" + first.full_hash[:12])
        assert len(first.env_id) <= 64
        assert first.resolved_resources != second.resolved_resources

        policy_change = _plan(
            tmp_path,
            _project(base.model_copy(update={"solver": CondaSolver.CLASSIC})),
            profile=_profile(),
            observation=_observation(),
        )
        assert policy_change.full_hash != first.full_hash

        (tmp_path / "environment.yml").write_text(
            "channels: [conda-forge, pytorch]\ndependencies: [python=3.11]\n# exact-byte change\n",
            encoding="utf-8",
        )
        byte_change = _plan(
            tmp_path,
            _project(base),
            profile=_profile(),
            observation=_observation(),
        )
        assert byte_change.full_hash != first.full_hash

    def test_environment_yaml_is_the_only_channel_declaration_source(self, tmp_path):
        _write_environment(tmp_path / "environment.yml")
        with pytest.raises(ValidationError, match="channels"):
            CondaEnvSpec.model_validate({"type": "conda", "name": "ml", "channels": ["defaults"]})

        plan = _plan(
            tmp_path,
            _project(CondaEnvSpec(name="ml")),
            profile=_profile(),
            observation=_observation(),
        )

        assert plan.channels == ["conda-forge", "pytorch"]
        assert plan.channel_priority is CondaChannelPriority.STRICT
        assert plan.solver is CondaSolver.LIBMAMBA


class TestEnvironmentPlanResolution:
    def test_build_resources_are_project_values_overlaid_field_by_field(self, tmp_path):
        _write_environment(tmp_path / "environment.yml")
        resources = Resources(
            time="12:00:00",
            cpus=1,
            mem="8G",
            gres="gpu:1",
            partition="short",
            account="science",
            qos="normal",
            constraint="a100",
            max_parallel=7,
        )
        spec = CondaEnvSpec(
            name="ml",
            modules=["ml-runtime"],
            build_resources=ResourceOverrides(time="02:00:00", cpus=6, mem="32G", constraint="h100"),
        )

        plan = _plan(tmp_path, _project(spec, resources=resources), profile=_profile(), observation=_observation())

        assert plan.resolved_resources == Resources(
            time="02:00:00",
            cpus=6,
            mem="32G",
            gres="gpu:1",
            partition="short",
            account="science",
            qos="normal",
            constraint="h100",
            max_parallel=7,
        )
        assert plan.modules == ["Miniforge/24", "ml-runtime"]

    def test_action_resolves_create_reuse_attach_and_rebuild(self, tmp_path):
        _write_environment(tmp_path / "environment.yml")
        project = _project(CondaEnvSpec(name="ml"))
        created = _plan(tmp_path, project, profile=_profile(), observation=_observation())
        assert created.action is EnvironmentPlanAction.CREATE

        ready = _record_for_plan(created, status=EnvironmentStatus.READY)
        reused = _plan(tmp_path, project, profile=_profile(), observation=_observation(), registry=[ready])
        rebuilt = _plan(
            tmp_path,
            project,
            profile=_profile(),
            observation=_observation(),
            registry=[ready],
            rebuild=True,
        )
        active = _record_for_plan(created, status=EnvironmentStatus.QUEUED)
        attached = _plan(tmp_path, project, profile=_profile(), observation=_observation(), registry=[active])

        assert reused.action is EnvironmentPlanAction.REUSE
        assert reused.prefix == ready.active_prefix
        assert rebuilt.action is EnvironmentPlanAction.REBUILD
        assert attached.action is EnvironmentPlanAction.ATTACH
        assert attached.current_attempt == "attempt-1"
        assert attached.build_job_id == "12345"

    def test_existing_prefix_can_be_planned_without_a_managed_cluster_contract(self, tmp_path):
        spec = ExistingEnvSpec(name="shared", prefix="/shared/conda/envs/research")

        plan = _plan(tmp_path, _project(spec))

        assert plan.action is EnvironmentPlanAction.VERIFY
        assert plan.backend is EnvBackend.EXISTING
        assert plan.ownership is EnvOwnership.EXTERNAL
        assert plan.executor is None
        assert plan.prefix == spec.prefix
        assert plan.complete is True

    def test_missing_managed_profile_is_reported_without_aborting_read_only_planning(self, tmp_path):
        _write_environment(tmp_path / "environment.yml")

        plan = _plan(tmp_path, _project(CondaEnvSpec(name="ml")))

        assert plan.action is EnvironmentPlanAction.CREATE
        assert plan.complete is False
        assert plan.missing == ["cluster_profile"]
        assert plan.conflicts == []

    def test_all_profile_file_channel_platform_and_time_conflicts_are_returned(self, tmp_path):
        _write_environment(tmp_path / "environment.yml", channels=False)
        project = _project(
            CondaEnvSpec(name="ml"),
            resources=Resources(time="02:00:00", partition="short"),
        )
        profile = _profile(network=NetworkProfile(compute_access="none", channel_access="none"))

        plan = _plan(
            tmp_path,
            project,
            profile=profile,
            observation=_observation(max_time="01:00:00", system="FreeBSD"),
        )

        assert plan.complete is False
        assert any("network access" in conflict for conflict in plan.conflicts)
        assert any("channel access" in conflict for conflict in plan.conflicts)
        assert any("platform.system" in conflict for conflict in plan.conflicts)
        assert any("declare channels" in conflict for conflict in plan.conflicts)
        assert any("exceeds" in conflict and "01:00:00" in conflict for conflict in plan.conflicts)

    def test_hash_prefix_collision_is_reported_instead_of_silently_reused(self, tmp_path):
        _write_environment(tmp_path / "environment.yml")
        project = _project(CondaEnvSpec(name="ml"))
        plan = _plan(tmp_path, project, profile=_profile(), observation=_observation())
        replacement = "0" if plan.full_hash[12] != "0" else "1"
        colliding_hash = plan.full_hash[:12] + replacement + plan.full_hash[13:]
        conflicting = EnvironmentRecord(
            env_id=plan.env_id,
            full_hash=colliding_hash,
            backend=EnvBackend.CONDA,
            ownership=EnvOwnership.MANAGED,
            status=EnvironmentStatus.PLANNED,
            created_at="2026-07-11T00:00:00Z",
            updated_at="2026-07-11T00:00:00Z",
            provenance=EnvironmentProvenance(canonical_spec_hash=colliding_hash),
        )

        collision = _plan(
            tmp_path,
            project,
            profile=_profile(),
            observation=_observation(),
            registry=[conflicting],
        )

        assert collision.complete is False
        assert any("hash collision" in conflict for conflict in collision.conflicts)


def _tree_content(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_planning_service_observes_and_scans_without_persistent_side_effects(
    project_dir,
    remote,
    remote_root,
    fake_transport,
    ctx,
):
    _write_environment(project_dir / "environment.yml")
    project = _project(CondaEnvSpec(name="ml"), resources=Resources(time="01:00:00", partition="short"))
    configured_remote = remote.model_copy(update={"cluster": _profile()})
    fake_transport._slurm_shims()
    local_before = _tree_content(ctx.require_project().paths.state_dir)
    config_before = _tree_content(ctx.user_paths.config_dir)
    remote_before = _tree_content(remote_root)
    fake_transport.calls.clear()

    plan = EnvironmentPlanningService().plan(
        transport=fake_transport,
        remote=configured_remote,
        layout=RemoteLayout(str(remote_root)),
        project=project,
        project_dir=project_dir,
    )

    assert plan.action is EnvironmentPlanAction.CREATE
    assert plan.complete is True
    assert len(fake_transport.calls) == 2
    assert fake_transport.calls[0].startswith("python3 - cluster-observe")
    assert fake_transport.calls[1].startswith("python3 - inspect")
    assert _tree_content(ctx.require_project().paths.state_dir) == local_before
    assert _tree_content(ctx.user_paths.config_dir) == config_before
    assert _tree_content(remote_root) == remote_before
