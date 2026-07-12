"""Persisted advisory inputs for low-round-trip environment preparation.

The remote registry remains authoritative.  Cache records are identity-bound
to one resolved remote base and are ignored when missing, stale, or corrupt.
Only mutating workflows write this cache; read-only planning and Doctor may
consume it without changing local state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import ClusterObservation
from slurmdeck.models.common import NameStr, StrictModel
from slurmdeck.models.env import EnvironmentRecord
from slurmdeck.models.remote import Remote
from slurmdeck.storage.paths import UserPaths
from slurmdeck.storage.permissions import ensure_private_directory
from slurmdeck.storage.yamlio import dump_yaml_model_atomic, load_yaml_mapping

ENVIRONMENT_OBSERVATION_TTL_SECONDS = 24 * 60 * 60
_CACHE_LOCK = threading.RLock()


class EnvironmentCacheEntry(StrictModel):
    schema_version: Literal[1] = 1
    remote_name: NameStr
    resolved_base: str
    observation: ClusterObservation | None = None
    observation_cached_at: float | None = None
    registry: list[EnvironmentRecord] = Field(default_factory=list)
    registry_cached_at: float | None = None


class EnvironmentCache:
    """Best-effort local cache; failures never replace remote truth."""

    def __init__(self, paths: UserPaths, *, clock: Callable[[], float] = time.time) -> None:
        self._paths = paths
        self._clock = clock

    def load(self, remote: Remote) -> EnvironmentCacheEntry | None:
        path = self._path(remote)
        if not path.is_file():
            return None
        try:
            data = load_yaml_mapping(path)
            entry = EnvironmentCacheEntry.model_validate(data)
        except (OSError, UserError, ValidationError, ValueError):
            return None
        if entry.remote_name != remote.name or entry.resolved_base != self._identity_base(remote):
            return None
        return entry

    def observation(
        self,
        remote: Remote,
        *,
        max_age_seconds: float = ENVIRONMENT_OBSERVATION_TTL_SECONDS,
    ) -> ClusterObservation | None:
        entry = self.load(remote)
        if entry is None or entry.observation is None or entry.observation_cached_at is None:
            return None
        age = max(0.0, self._clock() - entry.observation_cached_at)
        return entry.observation if age <= max_age_seconds else None

    def registry(self, remote: Remote) -> list[EnvironmentRecord]:
        entry = self.load(remote)
        return list(entry.registry) if entry is not None else []

    def remember_observation(self, remote: Remote, observation: ClusterObservation) -> None:
        with _CACHE_LOCK:
            entry = self._current_or_empty(remote).model_copy(
                update={"observation": observation, "observation_cached_at": self._clock()}
            )
            self._write(remote, entry)

    def remember_registry(self, remote: Remote, records: list[EnvironmentRecord]) -> None:
        with _CACHE_LOCK:
            entry = self._current_or_empty(remote).model_copy(
                update={"registry": records, "registry_cached_at": self._clock()}
            )
            self._write(remote, entry)

    def remember_record(self, remote: Remote, record: EnvironmentRecord) -> None:
        with _CACHE_LOCK:
            entry = self._current_or_empty(remote)
            records = [item for item in entry.registry if item.env_id != record.env_id]
            records.append(record)
            records.sort(key=lambda item: item.env_id)
            self._write(
                remote,
                entry.model_copy(update={"registry": records, "registry_cached_at": self._clock()}),
            )

    def _current_or_empty(self, remote: Remote) -> EnvironmentCacheEntry:
        return self.load(remote) or EnvironmentCacheEntry(
            remote_name=remote.name,
            resolved_base=self._identity_base(remote),
        )

    def _write(self, remote: Remote, entry: EnvironmentCacheEntry) -> None:
        try:
            ensure_private_directory(self._paths.config_dir)
            dump_yaml_model_atomic(self._path(remote), entry)
        except OSError:
            # This state is only an optimization.  A read-only or full config
            # directory must not make an otherwise valid remote operation fail.
            return

    def _path(self, remote: Remote) -> Path:
        return self._paths.environment_cache_dir / f"{remote.name}.yaml"

    @staticmethod
    def _identity_base(remote: Remote) -> str:
        return (remote.resolved_base or remote.base).rstrip("/") or "/"
