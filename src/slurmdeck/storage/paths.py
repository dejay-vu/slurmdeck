"""Path layout: user config, project state, and the remote directory tree.

``RemoteLayout`` is pure string math (POSIX joins) — it never touches the
network, so every remote path used anywhere in slurmdeck is derived from one
place.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import platformdirs

APP_NAME = "slurmdeck"
PROJECT_STATE_DIR = ".slurmdeck"
RUN_COMMIT_MARKER = ".committed.json"
_CONTROL_SOCKET_BUDGET = 96
_CONTROL_SOCKET_TEMPLATE = "cm-" + "0" * 16 + "." + "x" * 16


@dataclass(frozen=True)
class UserPaths:
    """User-level directories (XDG-style via platformdirs)."""

    config_dir: Path = field(default_factory=lambda: Path(platformdirs.user_config_dir(APP_NAME)))
    runtime_dir: Path = field(default_factory=lambda: Path(platformdirs.user_runtime_dir(APP_NAME)))

    @property
    def remotes_dir(self) -> Path:
        return self.config_dir / "remotes"

    @property
    def state_path(self) -> Path:
        return self.config_dir / "state.yaml"

    @property
    def environment_cache_dir(self) -> Path:
        """Persisted, advisory inputs for low-latency environment preparation."""
        return self.config_dir / "cache" / "environments"

    @property
    def ssh_control_dir(self) -> Path:
        candidate = self.runtime_dir / "ssh"
        if len(os.fsencode(candidate / _CONTROL_SOCKET_TEMPLATE)) <= _CONTROL_SOCKET_BUDGET:
            return candidate
        uid = os.getuid() if hasattr(os, "getuid") else 0
        system_runtime = Path(f"/run/user/{uid}")
        short_root = system_runtime if system_runtime.is_dir() else Path(tempfile.gettempdir()) / f"{APP_NAME}-{uid}"
        return short_root / APP_NAME / "ssh"


@dataclass(frozen=True)
class ProjectPaths:
    """Layout under ``<project>/.slurmdeck``."""

    root: Path

    @property
    def state_dir(self) -> Path:
        return self.root / PROJECT_STATE_DIR

    @property
    def config_path(self) -> Path:
        return self.state_dir / "project.yaml"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "slurmdeck.db"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def run_staging_dir(self) -> Path:
        return self.state_dir / "staging" / "runs"

    def run_staging_wrapper(self, run_id: str) -> Path:
        return self.run_staging_dir / run_id

    @property
    def run_materialization_locks_dir(self) -> Path:
        return self.state_dir / "locks" / "run-materialization"

    def run_materialization_lock(self, run_id: str) -> Path:
        return self.run_materialization_locks_dir / f"{run_id}.lock"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def run_commit_marker(self, run_id: str) -> Path:
        return self.run_dir(run_id) / RUN_COMMIT_MARKER

    def exists(self) -> bool:
        return self.config_path.exists()

    @classmethod
    def discover(cls, start: Path) -> ProjectPaths | None:
        """Walk up from ``start`` to find the nearest initialized project."""
        current = start.resolve()
        for candidate in (current, *current.parents):
            paths = cls(candidate)
            if paths.exists():
                return paths
        return None


@dataclass(frozen=True)
class RemoteLayout:
    """The remote directory tree under a remote's ``base`` path."""

    base: str

    def _join(self, *parts: str) -> str:
        return str(PurePosixPath(self.base.rstrip("/")).joinpath(*parts))

    @property
    def runs_dir(self) -> str:
        return self._join("runs")

    def run_root(self, run_id: str) -> str:
        return self._join("runs", run_id)

    def run_submission_receipt(self, token: str) -> str:
        return self._join("receipts", "run", f"{token}.json")

    def run_submission_lock(self, token: str) -> str:
        return self._join("locks", "run", f"{token}.lock")

    @property
    def snapshots_dir(self) -> str:
        return self._join("snapshots")

    def snapshot_dir(self, digest: str) -> str:
        return self._join("snapshots", digest)

    def snapshot_code_dir(self, digest: str) -> str:
        return self._join("snapshots", digest, "code")

    def snapshot_marker(self, digest: str) -> str:
        return self._join("snapshots", digest, ".complete.json")

    @property
    def snapshot_gc_lock(self) -> str:
        return self._join("locks", "snapshot-gc.lock")

    @property
    def envs_dir(self) -> str:
        return self._join("envs")

    @property
    def env_registry_dir(self) -> str:
        return self._join("envs", "registry")

    def env_registry_record(self, env_id: str) -> str:
        return self._join("envs", "registry", f"{env_id}.json")

    def env_generation_dir(self, env_id: str, generation_id: str) -> str:
        return self._join("envs", "generations", env_id, generation_id)

    def env_generations_dir(self, env_id: str) -> str:
        return self._join("envs", "generations", env_id)

    def env_attempt_dir(self, env_id: str, attempt_id: str) -> str:
        return self._join("envs", "attempts", env_id, attempt_id)

    def env_inbox_dir(self, attempt_id: str) -> str:
        return self._join("envs", "inbox", attempt_id)

    def env_trash_dir(self, env_id: str) -> str:
        return self._join("envs", "trash", env_id)

    def env_lock(self, full_hash: str) -> str:
        return self._join("locks", "env", full_hash, ".lock")

    def env_receipt(self, attempt_id: str) -> str:
        return self._join("receipts", "env", f"{attempt_id}.json")

    def all_top_dirs(self) -> list[str]:
        return [
            self.runs_dir,
            self.snapshots_dir,
            self.envs_dir,
            self._join("receipts"),
            self._join("locks"),
        ]
