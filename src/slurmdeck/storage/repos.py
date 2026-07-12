"""Typed repositories over the project database.

Status refreshes go through ``TaskRepo.apply_scheduler`` / ``apply_artifact``,
which update only rows whose values actually changed — the TUI and CLI read
paths never rewrite whole state files.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass

from slurmdeck.models.env import EnvBinding, EnvWaitPolicy
from slurmdeck.models.resources import Resources
from slurmdeck.models.run import CommandTemplateSpec, TaskSpec
from slurmdeck.models.status import RunSummary, SchedulerObservation, SchedulerSource
from slurmdeck.structured_errors import StructuredError


@dataclass(frozen=True)
class PlannedTaskRecord:
    """A task spec plus the templates it was resolved from (needed by retry)."""

    spec: TaskSpec
    params: dict[str, object]
    args_template: list[str] | None
    env_template: dict[str, str]
    arg_style: str


@dataclass(frozen=True)
class TaskStatusRecord:
    """Stored scheduler and artifact facts before service-owned derivation."""

    task_id: str
    name: str
    scheduler_job_id: str
    scheduler_array_task_id: str | None
    scheduler_state: str
    scheduler_exit: str
    scheduler_reason: str
    scheduler_observed_at: float
    scheduler_source: str
    artifact_state: str
    artifact_exit_code: int | None
    artifact_reason: str
    artifact_observed_at: float


@dataclass(frozen=True)
class RunRow:
    id: str
    project_id: str
    project_display_name: str
    name: str
    remote: str
    created_at: str
    state: str
    slurm_job_id: str
    remote_root: str
    snapshot_hash: str
    env_id: str
    env_generation_id: str
    env_prefix: str
    env_attempt_id: str
    env_build_job_id: str
    env_wait_policy: str
    env_dependency_state: str
    env_dependency_reason: str
    resources: Resources
    command: CommandTemplateSpec
    sweep_file: str | None
    retry_of: str | None
    submission_token: str
    submission_phase: str
    submission_error_json: str
    status_refreshed_at: float
    status_refresh_failed_at: float
    status_refresh_error_json: str
    status_sources_json: str
    scan_watermark: float
    summary: RunSummary

    @property
    def env_binding(self) -> EnvBinding | None:
        if not self.env_id:
            return None
        return EnvBinding(
            env_id=self.env_id,
            generation_id=self.env_generation_id,
            prefix=self.env_prefix,
            attempt_id=self.env_attempt_id,
            build_job_id=self.env_build_job_id,
            wait_policy=EnvWaitPolicy(self.env_wait_policy or EnvWaitPolicy.READY),
        )


def _run_row(row: sqlite3.Row) -> RunRow:
    return RunRow(
        id=row["id"],
        project_id=row["project_id"],
        project_display_name=row["project_display_name"],
        name=row["name"],
        remote=row["remote"],
        created_at=row["created_at"],
        state=row["state"],
        slurm_job_id=row["slurm_job_id"],
        remote_root=row["remote_root"],
        snapshot_hash=row["snapshot_hash"],
        env_id=row["env_id"],
        env_generation_id=row["env_generation_id"],
        env_prefix=row["env_prefix"],
        env_attempt_id=row["env_attempt_id"],
        env_build_job_id=row["env_build_job_id"],
        env_wait_policy=row["env_wait_policy"],
        env_dependency_state=row["env_dependency_state"],
        env_dependency_reason=row["env_dependency_reason"],
        resources=Resources.model_validate_json(row["resources_json"]),
        command=CommandTemplateSpec.model_validate_json(row["command_json"]),
        sweep_file=row["sweep_file"],
        retry_of=row["retry_of"],
        submission_token=row["submission_token"],
        submission_phase=row["submission_phase"],
        submission_error_json=row["submission_error_json"],
        status_refreshed_at=row["status_refreshed_at"],
        status_refresh_failed_at=row["status_refresh_failed_at"],
        status_refresh_error_json=row["status_refresh_error_json"],
        status_sources_json=row["status_sources_json"],
        scan_watermark=row["scan_watermark"],
        summary=RunSummary.model_validate(json.loads(row["summary_json"] or "{}")),
    )


class RunRepo:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def insert(
        self,
        *,
        run_id: str,
        project_id: str,
        project_display_name: str,
        name: str,
        remote: str,
        created_at: str,
        state: str,
        remote_root: str,
        snapshot_hash: str,
        env_id: str,
        resources: Resources,
        command: CommandTemplateSpec,
        sweep_file: str | None,
        retry_of: str | None,
        env_generation_id: str = "",
        env_prefix: str = "",
        env_attempt_id: str = "",
        env_build_job_id: str = "",
        env_wait_policy: str = "",
        env_dependency_state: str = "",
        env_dependency_reason: str = "",
        submission_token: str = "",
        submission_phase: str = "",
        submission_error_json: str = "{}",
        status_refresh_failed_at: float = 0.0,
        status_refresh_error_json: str = "{}",
        status_sources_json: str = "[]",
        transaction: sqlite3.Connection | None = None,
    ) -> None:
        if transaction is not None and transaction is not self._db:
            raise ValueError("outer transaction must use the repository connection")

        def execute() -> None:
            self._db.execute(
                """
                INSERT INTO runs (id, project_id, project_display_name, name, remote, created_at, state,
                                  remote_root, snapshot_hash, env_id, env_generation_id, env_prefix, env_attempt_id,
                                  env_build_job_id, env_wait_policy, env_dependency_state, env_dependency_reason,
                                  resources_json, command_json, sweep_file, retry_of, submission_token,
                                  submission_phase, submission_error_json, status_refresh_failed_at,
                                  status_refresh_error_json, status_sources_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    project_display_name,
                    name,
                    remote,
                    created_at,
                    state,
                    remote_root,
                    snapshot_hash,
                    env_id,
                    env_generation_id,
                    env_prefix,
                    env_attempt_id,
                    env_build_job_id,
                    env_wait_policy,
                    env_dependency_state,
                    env_dependency_reason,
                    resources.model_dump_json(),
                    command.model_dump_json(),
                    sweep_file,
                    retry_of,
                    submission_token,
                    submission_phase,
                    submission_error_json,
                    status_refresh_failed_at,
                    status_refresh_error_json,
                    status_sources_json,
                ),
            )

        if transaction is None:
            with self._db:
                execute()
        else:
            execute()

    def get(self, run_id: str) -> RunRow | None:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _run_row(row) if row else None

    def latest(self) -> RunRow | None:
        row = self._db.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        return _run_row(row) if row else None

    def list(self) -> list[RunRow]:
        rows = self._db.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC").fetchall()
        return [_run_row(row) for row in rows]

    def set_state(self, run_id: str, state: str) -> None:
        with self._db:
            self._db.execute("UPDATE runs SET state = ? WHERE id = ?", (state, run_id))

    def set_env_dependency(self, run_id: str, *, state: str, reason: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE runs SET env_dependency_state = ?, env_dependency_reason = ? WHERE id = ?",
                (state, reason, run_id),
            )

    def _set_env_dependency(self, run_id: str, *, state: str, reason: str) -> None:
        self._db.execute(
            "UPDATE runs SET env_dependency_state = ?, env_dependency_reason = ? WHERE id = ?",
            (state, reason, run_id),
        )

    def begin_submission(self, run_id: str, *, token: str, phase: str) -> bool:
        with self._db:
            cursor = self._db.execute(
                """
                UPDATE runs
                SET state = 'submitting', slurm_job_id = '', submission_token = ?,
                    submission_phase = ?, submission_error_json = '{}'
                WHERE id = ? AND state IN ('planned', 'submit_failed')
                """,
                (token, phase, run_id),
            )
        return cursor.rowcount == 1

    def set_submission_phase(self, run_id: str, *, token: str, phase: str) -> bool:
        with self._db:
            cursor = self._db.execute(
                "UPDATE runs SET submission_phase = ? WHERE id = ? AND submission_token = ?",
                (phase, run_id, token),
            )
        return cursor.rowcount == 1

    def record_submission_error(
        self,
        run_id: str,
        *,
        token: str,
        state: str,
        phase: str,
        error: StructuredError,
    ) -> bool:
        with self._db:
            cursor = self._db.execute(
                """
                UPDATE runs
                SET state = ?, submission_phase = ?, submission_error_json = ?
                WHERE id = ? AND submission_token = ?
                """,
                (state, phase, error.model_dump_json(), run_id, token),
            )
        return cursor.rowcount == 1

    def record_submission(
        self,
        run_id: str,
        *,
        slurm_job_id: str,
        snapshot_hash: str,
        env_id: str,
        token: str | None = None,
    ) -> bool:
        condition = "id = ?" if token is None else "id = ? AND submission_token = ?"
        parameters: tuple[object, ...]
        if token is None:
            parameters = (slurm_job_id, snapshot_hash, env_id, run_id)
        else:
            parameters = (slurm_job_id, snapshot_hash, env_id, run_id, token)
        with self._db:
            cursor = self._db.execute(
                "UPDATE runs SET state = 'submitted', slurm_job_id = ?, snapshot_hash = ?, env_id = ?, "
                f"submission_phase = 'submitted', submission_error_json = '{{}}' WHERE {condition}",
                parameters,
            )
        return cursor.rowcount == 1

    def set_summary(
        self,
        run_id: str,
        summary: RunSummary,
        *,
        refreshed_at: float | None = None,
        scan_watermark: float | None = None,
    ) -> None:
        with self._db:
            self._db.execute(
                "UPDATE runs SET summary_json = ?, status_refreshed_at = ?,"
                " scan_watermark = max(scan_watermark, ?) WHERE id = ?",
                (
                    summary.model_dump_json(),
                    refreshed_at if refreshed_at is not None else time.time(),
                    scan_watermark if scan_watermark is not None else 0.0,
                    run_id,
                ),
            )

    def record_refresh_success(
        self,
        run_id: str,
        *,
        summary: RunSummary,
        sources: Sequence[SchedulerSource],
        refreshed_at: float,
        scan_watermark: float = 0.0,
    ) -> None:
        with self._db:
            self._record_refresh_success(
                run_id,
                summary=summary,
                sources=sources,
                refreshed_at=refreshed_at,
                scan_watermark=scan_watermark,
            )

    def _record_refresh_success(
        self,
        run_id: str,
        *,
        summary: RunSummary,
        sources: Sequence[SchedulerSource],
        refreshed_at: float,
        scan_watermark: float = 0.0,
    ) -> None:
        self._db.execute(
            """
            UPDATE runs
            SET summary_json = ?, status_refreshed_at = ?, status_sources_json = ?,
                status_refresh_failed_at = 0, status_refresh_error_json = '{}',
                scan_watermark = max(scan_watermark, ?)
            WHERE id = ?
            """,
            (
                summary.model_dump_json(),
                refreshed_at,
                json.dumps([source.value for source in sources]),
                scan_watermark,
                run_id,
            ),
        )

    def record_refresh_failure(self, run_id: str, *, failed_at: float, error: StructuredError) -> None:
        with self._db:
            self._record_refresh_failure(run_id, failed_at=failed_at, error=error)

    def _record_refresh_failure(self, run_id: str, *, failed_at: float, error: StructuredError) -> None:
        self._db.execute(
            """
            UPDATE runs
            SET status_refresh_failed_at = ?, status_refresh_error_json = ?
            WHERE id = ?
            """,
            (failed_at, error.model_dump_json(), run_id),
        )

    def delete(self, run_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM tasks WHERE run_id = ?", (run_id,))
            self._db.execute("DELETE FROM runs WHERE id = ?", (run_id,))


class TaskRepo:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def insert_planned(
        self,
        run_id: str,
        records: list[PlannedTaskRecord],
        *,
        transaction: sqlite3.Connection | None = None,
    ) -> None:
        if transaction is not None and transaction is not self._db:
            raise ValueError("outer transaction must use the repository connection")

        def execute() -> None:
            self._db.executemany(
                """
                INSERT INTO tasks (run_id, idx, task_id, name, argv_json, shell, env_json, config_rel, result_rel,
                                   params_json, args_template_json, env_template_json, arg_style)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        record.spec.index,
                        record.spec.task_id,
                        record.spec.name,
                        json.dumps(record.spec.argv) if record.spec.argv is not None else None,
                        record.spec.shell,
                        json.dumps(record.spec.env),
                        record.spec.config,
                        record.spec.result_dir,
                        json.dumps(record.params),
                        json.dumps(record.args_template) if record.args_template is not None else None,
                        json.dumps(record.env_template),
                        record.arg_style,
                    )
                    for record in records
                ],
            )
            summary = RunSummary(total=len(records), counts={"PENDING": len(records)})
            self._db.execute(
                "UPDATE runs SET summary_json = ? WHERE id = ?",
                (summary.model_dump_json(), run_id),
            )

        if transaction is None:
            with self._db:
                execute()
        else:
            execute()

    def planned_records(self, run_id: str) -> list[PlannedTaskRecord]:
        rows = self._db.execute("SELECT * FROM tasks WHERE run_id = ? ORDER BY idx", (run_id,)).fetchall()
        return [
            PlannedTaskRecord(
                spec=TaskSpec(
                    index=row["idx"],
                    task_id=row["task_id"],
                    name=row["name"],
                    argv=json.loads(row["argv_json"]) if row["argv_json"] else None,
                    shell=row["shell"],
                    env=json.loads(row["env_json"] or "{}"),
                    config=row["config_rel"],
                    result_dir=row["result_rel"],
                ),
                params=json.loads(row["params_json"] or "{}"),
                args_template=json.loads(row["args_template_json"]) if row["args_template_json"] else None,
                env_template=json.loads(row["env_template_json"] or "{}"),
                arg_style=row["arg_style"],
            )
            for row in rows
        ]

    def specs(self, run_id: str) -> list[TaskSpec]:
        rows = self._db.execute("SELECT * FROM tasks WHERE run_id = ? ORDER BY idx", (run_id,)).fetchall()
        return [
            TaskSpec(
                index=row["idx"],
                task_id=row["task_id"],
                name=row["name"],
                argv=json.loads(row["argv_json"]) if row["argv_json"] else None,
                shell=row["shell"],
                env=json.loads(row["env_json"] or "{}"),
                config=row["config_rel"],
                result_dir=row["result_rel"],
            )
            for row in rows
        ]

    def status_records(self, run_id: str) -> list[TaskStatusRecord]:
        rows = self._db.execute("SELECT * FROM tasks WHERE run_id = ? ORDER BY idx", (run_id,)).fetchall()
        return [
            TaskStatusRecord(
                task_id=row["task_id"],
                name=row["name"],
                scheduler_job_id=row["scheduler_job_id"],
                scheduler_array_task_id=row["scheduler_array_task_id"],
                scheduler_state=row["scheduler_state"],
                scheduler_exit=row["scheduler_exit"],
                scheduler_reason=row["scheduler_reason"],
                scheduler_observed_at=row["scheduler_observed_at"],
                scheduler_source=row["scheduler_source"],
                artifact_state=row["artifact_state"],
                artifact_exit_code=row["artifact_exit_code"],
                artifact_reason=row["artifact_reason"],
                artifact_observed_at=row["artifact_observed_at"],
            )
            for row in rows
        ]

    def apply_scheduler(self, run_id: str, observations_by_task: dict[str, SchedulerObservation]) -> int:
        """Persist complete scheduler observations; return the number of changed rows."""
        with self._db:
            changed = self._apply_scheduler(run_id, observations_by_task)
        return changed

    def _apply_scheduler(self, run_id: str, observations_by_task: dict[str, SchedulerObservation]) -> int:
        changed = 0
        now = time.time()
        for task_id, observation in observations_by_task.items():
            cursor = self._db.execute(
                """
                UPDATE tasks
                SET scheduler_job_id = :job_id, scheduler_array_task_id = :array_task_id,
                    scheduler_state = :state, scheduler_exit = :exit_code,
                    scheduler_reason = :reason, scheduler_observed_at = :observed_at,
                    scheduler_source = :source, updated_at = :updated_at
                WHERE run_id = :run_id AND task_id = :task_id
                  AND scheduler_observed_at <= :observed_at
                  AND (scheduler_job_id != :job_id
                       OR ifnull(scheduler_array_task_id, '') != ifnull(:array_task_id, '')
                       OR scheduler_state != :state OR scheduler_exit != :exit_code
                       OR scheduler_reason != :reason OR scheduler_observed_at != :observed_at
                       OR scheduler_source != :source)
                """,
                {
                    "job_id": observation.job_id,
                    "array_task_id": observation.array_task_id,
                    "state": observation.scheduler_state,
                    "exit_code": observation.exit_code,
                    "reason": observation.scheduler_reason,
                    "observed_at": observation.observed_at,
                    "source": observation.source.value,
                    "updated_at": now,
                    "run_id": run_id,
                    "task_id": task_id,
                },
            )
            changed += cursor.rowcount
        return changed

    def apply_artifact(
        self,
        run_id: str,
        records: list[dict[str, object]],
    ) -> int:
        """Apply agent scan records (dicts with task_id/state/exit_code/...); returns changed rows."""
        with self._db:
            changed = self._apply_artifact(run_id, records)
        return changed

    def _apply_artifact(self, run_id: str, records: list[dict[str, object]]) -> int:
        changed = 0
        now = time.time()
        for record in records:
            raw_observed_at = record.get("mtime", 0.0)
            observed_at = float(raw_observed_at) if isinstance(raw_observed_at, int | float | str) else 0.0
            state = record.get("state", "UNKNOWN")
            exit_code = record.get("exit_code")
            reason = record.get("reason", "") or ""
            started_at = record.get("started_at", "") or ""
            ended_at = record.get("ended_at", "") or ""
            cursor = self._db.execute(
                """
                UPDATE tasks SET artifact_state = ?, artifact_exit_code = ?, artifact_reason = ?,
                                 artifact_observed_at = ?, started_at = ?, ended_at = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ?
                  AND artifact_observed_at <= ?
                  AND (artifact_state != ?
                       OR ifnull(artifact_exit_code, -999999) != ifnull(?, -999999)
                       OR artifact_reason != ? OR artifact_observed_at != ?
                       OR started_at != ? OR ended_at != ?)
                """,
                (
                    state,
                    exit_code,
                    reason,
                    observed_at,
                    started_at,
                    ended_at,
                    now,
                    run_id,
                    record.get("task_id", ""),
                    observed_at,
                    state,
                    exit_code,
                    reason,
                    observed_at,
                    started_at,
                    ended_at,
                ),
            )
            changed += cursor.rowcount
        return changed
