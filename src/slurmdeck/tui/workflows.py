"""Pure capability gates shared by TUI workflow surfaces."""

from __future__ import annotations

from collections.abc import Sequence

from slurmdeck.models.cluster import BuildExecutor, ClusterProfile, InvalidDependencyPolicy
from slurmdeck.models.env import EnvironmentStatus, EnvironmentView

_ACTIVE_BUILD_STATES = {
    EnvironmentStatus.STAGING,
    EnvironmentStatus.QUEUED,
    EnvironmentStatus.BUILDING,
    EnvironmentStatus.VERIFYING,
}


def afterok_is_available(profile: ClusterProfile | None, views: Sequence[EnvironmentView]) -> bool:
    if (
        profile is None
        or profile.slurm.afterok_dependency is not True
        or profile.slurm.kill_invalid_dependency
        not in {InvalidDependencyPolicy.PER_JOB, InvalidDependencyPolicy.SITE_WIDE}
    ):
        return False
    return any(
        view.desired_by_project
        and view.record.status in _ACTIVE_BUILD_STATES
        and view.latest_attempt is not None
        and view.latest_attempt.executor is BuildExecutor.SLURM
        and view.latest_attempt.job_id.isdigit()
        for view in views
    )
