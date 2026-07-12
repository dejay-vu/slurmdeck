"""Read-only inspection and explicit reconciliation of interrupted run commits."""

from __future__ import annotations

import fcntl
import json
import shutil
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from slurmdeck.errors import UserError
from slurmdeck.operations import OperationPhase
from slurmdeck.services.context import AppContext
from slurmdeck.services.run_materialization import _write_commit_marker
from slurmdeck.storage.paths import ProjectPaths
from slurmdeck.structured_errors import StructuredError


@dataclass(frozen=True)
class RecoveryReport:
    stale_staging: tuple[str, ...] = ()
    active_staging: tuple[str, ...] = ()
    orphaned_run_dirs: tuple[str, ...] = ()
    unmarked_committed_runs: tuple[str, ...] = ()
    missing_run_dirs: tuple[str, ...] = ()


class RunRecoveryService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def inspect(self) -> RecoveryReport:
        """Report interrupted states without creating SQLite or changing files."""
        paths = self._ctx.require_project().paths
        rows = self._stored_run_ids(paths)
        final_dirs = self._final_run_ids(paths)
        stale_staging, active_staging = self._classify_staging(paths)
        active_run_ids = set(active_staging)

        orphaned = tuple(
            sorted(
                run_id
                for run_id in final_dirs - rows
                if run_id not in active_run_ids and not self._has_valid_marker(paths.run_commit_marker(run_id), run_id)
            )
        )
        unmarked = tuple(
            sorted(
                run_id
                for run_id in final_dirs & rows
                if run_id not in active_run_ids and not self._has_valid_marker(paths.run_commit_marker(run_id), run_id)
            )
        )
        missing = tuple(sorted((rows - final_dirs) - active_run_ids))
        return RecoveryReport(
            stale_staging=stale_staging,
            active_staging=active_staging,
            orphaned_run_dirs=orphaned,
            unmarked_committed_runs=unmarked,
            missing_run_dirs=missing,
        )

    def reconcile(self) -> RecoveryReport:
        """Repair recoverable interrupted states and reject data loss."""
        paths = self._ctx.require_project().paths
        report = self.inspect()
        if report.missing_run_dirs:
            run_ids = list(report.missing_run_dirs)
            raise UserError(
                StructuredError(
                    code="run_state_corrupt",
                    summary="Run database rows are missing their committed directories.",
                    detail=f"Missing local directories for: {', '.join(run_ids)}.",
                    operation="run.reconcile",
                    phase=OperationPhase.RECONCILE,
                    retryable=False,
                    remediation="Restore the missing run directories from backup or remove the corrupt rows manually.",
                    context={"run_ids": run_ids},
                )
            )

        for name in report.stale_staging:
            self._remove_unlocked_staging(
                paths.run_staging_wrapper(name),
                paths.run_materialization_lock(name),
            )
        for run_id in report.orphaned_run_dirs:
            run_dir = paths.run_dir(run_id)
            if run_dir.is_dir() and not self._has_valid_marker(paths.run_commit_marker(run_id), run_id):
                shutil.rmtree(run_dir)
        for run_id in report.unmarked_committed_runs:
            run_dir = paths.run_dir(run_id)
            if run_dir.is_dir() and not self._has_valid_marker(paths.run_commit_marker(run_id), run_id):
                _write_commit_marker(paths.run_commit_marker(run_id), run_id)

        self._prune_empty(paths.run_staging_dir, paths.run_staging_dir.parent)
        return report

    @staticmethod
    def _stored_run_ids(paths: ProjectPaths) -> set[str]:
        if not paths.db_path.is_file():
            return set()
        connection = sqlite3.connect(f"file:{paths.db_path}?mode=ro", uri=True)
        try:
            return {str(row[0]) for row in connection.execute("SELECT id FROM runs")}
        finally:
            connection.close()

    @staticmethod
    def _final_run_ids(paths: ProjectPaths) -> set[str]:
        if not paths.runs_dir.is_dir():
            return set()
        return {path.name for path in paths.runs_dir.iterdir() if path.is_dir()}

    @classmethod
    def _classify_staging(cls, paths: ProjectPaths) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not paths.run_staging_dir.is_dir():
            return (), ()
        stale: list[str] = []
        active: list[str] = []
        for wrapper in sorted(
            (path for path in paths.run_staging_dir.iterdir() if path.is_dir()), key=lambda p: p.name
        ):
            if cls._lock_is_active(paths.run_materialization_lock(wrapper.name)):
                active.append(wrapper.name)
            else:
                stale.append(wrapper.name)
        return tuple(stale), tuple(active)

    @staticmethod
    def _lock_is_active(lock_path: Path) -> bool:
        if not lock_path.is_file():
            return False
        with lock_path.open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False

    @staticmethod
    def _remove_unlocked_staging(wrapper: Path, lock_path: Path) -> None:
        if not wrapper.is_dir():
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            shutil.rmtree(wrapper)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _has_valid_marker(marker: Path, run_id: str) -> bool:
        if not marker.is_file():
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("schema_version") == 1
            and payload.get("run_id") == run_id
            and isinstance(payload.get("committed_at"), str)
        )

    @staticmethod
    def _prune_empty(*directories: Path) -> None:
        for directory in directories:
            with suppress(OSError):
                directory.rmdir()
