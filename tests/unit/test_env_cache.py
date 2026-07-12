from __future__ import annotations

from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.env_cache import EnvironmentCache


def test_environment_cache_is_identity_bound_and_expires_observations(
    user_paths,
    remote,
    fake_transport,
) -> None:
    now = 1_000_000.0
    cache = EnvironmentCache(user_paths, clock=lambda: now)
    observation = ClusterCapabilityService().observe(fake_transport, remote)
    cache.remember_observation(remote, observation)

    assert cache.observation(remote) == observation
    assert EnvironmentCache(user_paths, clock=lambda: now + 86_401).observation(remote) is None
    changed = remote.model_copy(update={"resolved_base": remote.resolved_base + "-changed"})
    assert cache.load(changed) is None


def test_environment_cache_ignores_corrupt_files(user_paths, remote) -> None:
    path = user_paths.environment_cache_dir / f"{remote.name}.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("registry: [not: valid", encoding="utf-8")

    assert EnvironmentCache(user_paths).load(remote) is None
