"""Recoverable filesystem and SQLite commit protocol for planned runs."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.common import RunState, validate_name
from slurmdeck.services.context import AppContext
from slurmdeck.services.run_planning import RunPlan
from slurmdeck.storage.paths import ProjectPaths
from slurmdeck.storage.repos import RunRepo, RunRow, TaskRepo


def _write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_run_directory(staging_run_dir: Path, final_run_dir: Path) -> None:
    os.replace(staging_run_dir, final_run_dir)


def _commit_transaction(connection: sqlite3.Connection) -> None:
    connection.commit()


def _write_commit_marker(marker_path: Path, run_id: str) -> None:
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "committed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temporary = marker_path.with_name(f".{marker_path.name}.{uuid.uuid4().hex}.tmp")
    _write_file(temporary, (json.dumps(payload, sort_keys=True) + "\n").encode())
    os.replace(temporary, marker_path)
    _fsync_directory(marker_path.parent)


class RunMaterializer:
    """Commit one complete plan, or compensate a caught failure back to nothing."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def commit(self, plan: RunPlan) -> RunRow:
        project = self._ctx.require_project()
        paths = project.paths
        self._validate_plan(plan)
        final_run_dir = paths.run_dir(plan.run_id)
        wrapper = paths.run_staging_wrapper(plan.run_id)
        lock_path = paths.run_materialization_lock(plan.run_id)
        attempt_dir: Path | None = None
        staging_run_dir: Path | None = None
        lock_handle = None
        owns_lock = False
        renamed = False
        transaction_started = False
        transaction_committed = False
        connection: sqlite3.Connection | None = None

        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_handle = lock_path.open("a+b")
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UserError(f"Run {plan.run_id!r} is already being materialized.") from exc
            owns_lock = True
            wrapper.mkdir(parents=True, exist_ok=True)

            if final_run_dir.exists():
                raise UserError(f"Run directory already exists: {final_run_dir}.")

            candidate_attempt_dir = wrapper / f"attempt-{uuid.uuid4().hex}"
            candidate_attempt_dir.mkdir(exist_ok=False)
            attempt_dir = candidate_attempt_dir
            staging_run_dir = attempt_dir / "run"
            staging_run_dir.mkdir()
            self._write_plan(staging_run_dir, plan)

            connection = self._ctx.db()
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            runs = RunRepo(connection)
            tasks = TaskRepo(connection)
            manifest = plan.manifest
            binding = manifest.env_binding
            runs.insert(
                run_id=plan.run_id,
                project_id=manifest.project_id,
                project_display_name=manifest.project_display_name,
                name=manifest.name,
                remote=manifest.remote,
                created_at=manifest.created_at,
                state=RunState.PLANNED,
                remote_root=manifest.remote_root,
                snapshot_hash=manifest.snapshot_hash,
                env_id=binding.env_id if binding else "",
                env_generation_id=binding.generation_id if binding else "",
                env_prefix=binding.prefix if binding else "",
                env_attempt_id=binding.attempt_id if binding else "",
                env_build_job_id=binding.build_job_id if binding else "",
                env_wait_policy=binding.wait_policy.value if binding else "",
                env_dependency_state=manifest.env_dependency_state,
                env_dependency_reason=manifest.env_dependency_reason,
                resources=manifest.resources,
                command=manifest.command,
                sweep_file=manifest.sweep_file,
                retry_of=manifest.retry_of,
                transaction=connection,
            )
            tasks.insert_planned(
                plan.run_id,
                [task.record for task in plan.tasks],
                transaction=connection,
            )

            paths.runs_dir.mkdir(parents=True, exist_ok=True)
            if final_run_dir.exists():
                raise UserError(f"Run directory already exists: {final_run_dir}.")
            _replace_run_directory(staging_run_dir, final_run_dir)
            renamed = True
            _fsync_directory(paths.runs_dir)
            _commit_transaction(connection)
            transaction_committed = True
            _write_commit_marker(paths.run_commit_marker(plan.run_id), plan.run_id)

            row = runs.get(plan.run_id)
            if row is None:
                raise RuntimeError(f"Committed run row disappeared: {plan.run_id}")
            return row
        except BaseException:
            self._compensate(
                plan=plan,
                paths=paths,
                connection=connection,
                transaction_started=transaction_started,
                transaction_committed=transaction_committed,
                renamed=renamed,
                final_run_dir=final_run_dir,
            )
            raise
        finally:
            if attempt_dir is not None and attempt_dir.exists():
                shutil.rmtree(attempt_dir, ignore_errors=True)
            if owns_lock:
                self._prune_empty(wrapper, paths.run_staging_dir, paths.run_staging_dir.parent)
            if lock_handle is not None:
                if owns_lock:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    @staticmethod
    def _write_plan(staging_run_dir: Path, plan: RunPlan) -> None:
        directories: set[Path] = {staging_run_dir}
        for relative_path, payload in plan.rendered_files.items():
            destination = staging_run_dir.joinpath(*PurePosixPath(relative_path).parts)
            _write_file(destination, payload)
            directories.update(destination.parents)
            if relative_path == protocol.SBATCH_FILE:
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        for directory in sorted(
            (path for path in directories if path == staging_run_dir or staging_run_dir in path.parents),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            _fsync_directory(directory)

    @staticmethod
    def _validate_plan(plan: RunPlan) -> None:
        validate_name(plan.run_id, what="run id")
        if plan.manifest.run_id != plan.run_id:
            raise UserError("Run plan id does not match its manifest.")
        if plan.manifest.task_count != len(plan.tasks):
            raise UserError("Run plan task count does not match its manifest.")
        for relative_path, payload in plan.rendered_files.items():
            path = PurePosixPath(relative_path)
            if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
                raise UserError(f"Unsafe rendered run path: {relative_path!r}.")
            if not isinstance(payload, bytes):
                raise UserError(f"Rendered run payload is not bytes: {relative_path!r}.")

    @staticmethod
    def _compensate(
        *,
        plan: RunPlan,
        paths: ProjectPaths,
        connection: sqlite3.Connection | None,
        transaction_started: bool,
        transaction_committed: bool,
        renamed: bool,
        final_run_dir: Path,
    ) -> None:
        persisted = transaction_committed
        if connection is not None and transaction_started:
            try:
                if connection.in_transaction:
                    connection.rollback()
                persisted = persisted or RunRepo(connection).get(plan.run_id) is not None
            except sqlite3.Error:
                pass
        if persisted and connection is not None:
            try:
                RunRepo(connection).delete(plan.run_id)
            except sqlite3.Error:
                return
        if renamed and final_run_dir.exists():
            shutil.rmtree(final_run_dir, ignore_errors=True)
            if paths.runs_dir.exists():
                with suppress(OSError):
                    _fsync_directory(paths.runs_dir)

    @staticmethod
    def _prune_empty(*directories: Path) -> None:
        for directory in directories:
            with suppress(OSError):
                directory.rmdir()
