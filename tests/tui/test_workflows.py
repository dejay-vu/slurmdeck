from __future__ import annotations

from slurmdeck.models.cluster import (
    BuildExecutor,
    ClusterProfile,
    InvalidDependencyPolicy,
    LoginBuildPolicy,
    SlurmClusterProfile,
)
from slurmdeck.models.env import (
    EnvBackend,
    EnvBuildAttempt,
    EnvGeneration,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvironmentView,
    EnvOwnership,
)
from slurmdeck.models.resources import Resources
from slurmdeck.tui.workflows import afterok_is_available


def _view(status: EnvironmentStatus, *, desired: bool = True) -> EnvironmentView:
    digest = "a" * 64
    attempt = EnvBuildAttempt(
        attempt_id="attempt-1",
        status=status,
        executor=BuildExecutor.SLURM,
        generation_id="gen-1",
        prefix="/base/envs/gen-1",
        job_id="42",
        resolved_resources=Resources(),
        created_at="2026-07-11T00:00:00Z",
    )
    generation = EnvGeneration(
        generation_id="gen-1",
        attempt_id=attempt.attempt_id,
        prefix=attempt.prefix,
        status=EnvironmentStatus.READY,
        created_at="2026-07-11T00:00:00Z",
        verified_at="2026-07-11T00:00:00Z",
        provenance=EnvironmentProvenance(canonical_spec_hash=digest),
    )
    ready = status is EnvironmentStatus.READY
    record = EnvironmentRecord(
        env_id=f"demo-{digest[:12]}",
        full_hash=digest,
        backend=EnvBackend.CONDA,
        ownership=EnvOwnership.MANAGED,
        status=status,
        created_at="2026-07-11T00:00:00Z",
        updated_at="2026-07-11T00:00:00Z",
        active_generation=generation.generation_id if ready else None,
        active_prefix=generation.prefix if ready else None,
        current_attempt=attempt.attempt_id,
        generations=[generation] if ready else [],
        attempts=[attempt],
        provenance=EnvironmentProvenance(canonical_spec_hash=digest),
    )
    return EnvironmentView(record=record, desired_by_project=desired)


def _profile(*, afterok: bool, policy: InvalidDependencyPolicy) -> ClusterProfile:
    return ClusterProfile(
        allowed_build_executors=[BuildExecutor.SLURM],
        default_build_executor=BuildExecutor.SLURM,
        login_build_policy=LoginBuildPolicy.FORBIDDEN,
        slurm=SlurmClusterProfile(
            afterok_dependency=afterok,
            kill_invalid_dependency=policy,
        ),
    )


def test_afterok_requires_active_desired_build_and_complete_capability_gate() -> None:
    safe = _profile(afterok=True, policy=InvalidDependencyPolicy.PER_JOB)
    unsafe = _profile(afterok=True, policy=InvalidDependencyPolicy.UNSUPPORTED)

    assert afterok_is_available(safe, [_view(EnvironmentStatus.QUEUED)]) is True
    assert afterok_is_available(safe, [_view(EnvironmentStatus.BUILDING)]) is True
    assert afterok_is_available(safe, [_view(EnvironmentStatus.READY)]) is False
    assert afterok_is_available(safe, [_view(EnvironmentStatus.QUEUED, desired=False)]) is False
    assert afterok_is_available(unsafe, [_view(EnvironmentStatus.QUEUED)]) is False
    assert afterok_is_available(None, [_view(EnvironmentStatus.QUEUED)]) is False
