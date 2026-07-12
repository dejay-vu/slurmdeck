"""Status refresh and the one public stale-aware status snapshot."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.common import RunState
from slurmdeck.models.status import (
    RunStatusSnapshot,
    RunSummary,
    SchedulerObservation,
    SchedulerSource,
    TaskStatusView,
)
from slurmdeck.operations import OperationPhase, OperationReporter, OperationSink, noop_operation_sink
from slurmdeck.services.context import AppContext
from slurmdeck.services.run_recovery import RunRecoveryService
from slurmdeck.slurm import TERMINAL_STATES, failed_states, parse_sacct, parse_squeue
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.storage.repos import RunRepo, RunRow, TaskRepo, TaskStatusRecord
from slurmdeck.structured_errors import StructuredError
from slurmdeck.transport import Transport, TransportError, parse_json_lines

_REFRESHABLE = {RunState.SUBMITTED, RunState.CANCELLED}
_DONE_TASK_STATES = TERMINAL_STATES | {"KILLED"}
_FAILED_TASK_STATES = (TERMINAL_STATES - {"COMPLETED"}) | {"FAILED", "KILLED"}
_ENV_DEPENDENCY_TERMINAL = {"ENV_BUILD_FAILED", "ENV_BUILD_CANCELLED"}


@dataclass(frozen=True)
class RefreshReport:
    refreshed: list[str] = field(default_factory=list)
    changed: int = 0
    is_stale: bool = False
    refresh_error: StructuredError | None = None


class StatusService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    @property
    def _runs(self) -> RunRepo:
        return RunRepo(self._ctx.db())

    @property
    def _tasks(self) -> TaskRepo:
        return TaskRepo(self._ctx.db())

    def snapshot(self, run_id: str) -> RunStatusSnapshot:
        """Return the only public composite task/run status read."""
        db = self._ctx.db()
        owns_transaction = not db.in_transaction
        if owns_transaction:
            db.execute("BEGIN")
        try:
            row = RunRepo(db).get(run_id)
            if row is None:
                raise UserError(f"Unknown run {run_id!r}.")

            refreshed_at = row.status_refreshed_at or None
            refresh_failed_at = row.status_refresh_failed_at or None
            is_stale = refresh_failed_at is not None and (refreshed_at is None or refresh_failed_at >= refreshed_at)
            tasks = [
                self._task_view(
                    record,
                    is_stale=is_stale,
                    env_dependency_state=row.env_dependency_state,
                    env_dependency_reason=row.env_dependency_reason,
                )
                for record in TaskRepo(db).status_records(run_id)
            ]

            raw_sources = json.loads(row.status_sources_json or "[]")
            sources = [SchedulerSource(source) for source in raw_sources] if isinstance(raw_sources, list) else []
            refresh_error = None
            if row.status_refresh_error_json and row.status_refresh_error_json != "{}":
                refresh_error = StructuredError.model_validate_json(row.status_refresh_error_json)
        except BaseException:
            if owns_transaction:
                db.rollback()
            raise
        else:
            if owns_transaction:
                db.commit()

        return RunStatusSnapshot(
            run_id=run_id,
            tasks=tasks,
            summary=row.summary,
            refreshed_at=refreshed_at,
            sources=sources,
            is_stale=is_stale,
            refresh_failed_at=refresh_failed_at,
            refresh_error=refresh_error,
            env_dependency_state=row.env_dependency_state,
            env_dependency_reason=row.env_dependency_reason,
        )

    def rows(self, run_id: str, *, states: set[str] | None = None) -> list[TaskStatusView]:
        tasks = self.snapshot(run_id).tasks
        return tasks if states is None else [task for task in tasks if task.effective_state in states]

    def summary(self, run_id: str) -> RunSummary:
        return self.snapshot(run_id).summary

    @staticmethod
    def _task_view(
        record: TaskStatusRecord,
        *,
        is_stale: bool,
        env_dependency_state: str = "",
        env_dependency_reason: str = "",
    ) -> TaskStatusView:
        scheduler_state = record.scheduler_state
        has_scheduler_state = scheduler_state not in ("", "UNKNOWN")
        local_state = record.artifact_state
        has_local_state = local_state not in ("", "UNKNOWN")
        if env_dependency_state in _ENV_DEPENDENCY_TERMINAL:
            effective_state = env_dependency_state
        elif env_dependency_state == "waiting":
            effective_state = "WAITING_FOR_ENV"
        elif has_scheduler_state:
            effective_state = scheduler_state
        elif has_local_state:
            effective_state = local_state
        else:
            effective_state = "PENDING"

        failure_reason = record.artifact_reason
        display_reason = None
        if effective_state in _ENV_DEPENDENCY_TERMINAL or effective_state == "WAITING_FOR_ENV":
            display_reason = env_dependency_reason or record.scheduler_reason
        elif effective_state in _FAILED_TASK_STATES and failure_reason:
            display_reason = failure_reason
        elif record.scheduler_reason:
            display_reason = record.scheduler_reason

        scheduler_exit = record.scheduler_exit
        exit_code: int | str | None
        if has_scheduler_state and scheduler_exit not in ("", "-"):
            exit_code = scheduler_exit
        else:
            exit_code = record.artifact_exit_code

        return TaskStatusView(
            task_id=record.task_id,
            name=record.name,
            local_state=local_state,
            scheduler_state=scheduler_state,
            effective_state=effective_state,
            scheduler_reason=record.scheduler_reason,
            failure_reason=failure_reason,
            display_reason=display_reason,
            exit_code=exit_code,
            observed_at=record.scheduler_observed_at or None,
            is_stale=is_stale,
        )

    def refresh(
        self,
        transport: Transport,
        layout: RemoteLayout,
        run_ids: list[str] | None = None,
        *,
        force: bool = False,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> RefreshReport:
        reporter = OperationReporter("status.refresh", operation_sink)
        reporter.started(OperationPhase.RECONCILE, message="Reconciling interrupted run commits")
        try:
            RunRecoveryService(self._ctx).reconcile()
            reporter.completed(OperationPhase.RECONCILE)
            reporter.started(OperationPhase.REFRESH, message="Preparing status refresh")
            report = self._refresh(transport, layout, run_ids, force=force, reporter=reporter)
        except Exception as exc:
            reporter.failed(message=str(exc))
            raise
        if report.is_stale:
            message = report.refresh_error.summary if report.refresh_error is not None else "Status refresh is stale"
            reporter.failed(message=message)
        else:
            reporter.completed(
                OperationPhase.REFRESH,
                result_counts={"refreshed": len(report.refreshed), "changed": report.changed},
            )
        return report

    def _refresh(
        self,
        transport: Transport,
        layout: RemoteLayout,
        run_ids: list[str] | None,
        *,
        force: bool,
        reporter: OperationReporter,
    ) -> RefreshReport:
        candidates = [
            row
            for row in self._runs.list()
            if (run_ids is None and row.state in _REFRESHABLE) or (run_ids is not None and row.id in run_ids)
        ]
        candidates = [
            row
            for row in candidates
            if row.state not in (RunState.PLANNED, RunState.SUBMITTING, RunState.SUBMIT_FAILED, RunState.SUBMIT_UNKNOWN)
        ]
        if not candidates:
            return RefreshReport()

        job_ids = sorted(
            {row.slurm_job_id for row in candidates if row.slurm_job_id}
            | {row.env_build_job_id for row in candidates if row.env_wait_policy == "afterok" and row.env_build_job_id}
        )
        since = 0.0 if force else min(row.scan_watermark for row in candidates)
        scan_args = ["scan", "--base", layout.base, "--since", str(since)]
        if job_ids:
            scan_args += ["--jobs", ",".join(job_ids)]
        for row in candidates:
            scan_args += ["--run", row.id]
        reporter.progress(
            OperationPhase.REFRESH,
            current=0,
            total=len(candidates),
            message=f"Refreshing {len(candidates)} run(s): scheduler + task artifacts",
        )

        try:
            result = transport.exec_python(protocol.agent_source(), scan_args, timeout=300, check=False)
        except TransportError as exc:
            error = self._refresh_error(
                "transport",
                str(exc),
                returncode=exc.returncode,
                stderr=exc.stderr,
            )
            return self._record_failure(candidates, error)
        except UserError as exc:
            error = exc.error if exc.error.operation == "status.refresh" else self._refresh_error("transport", str(exc))
            return self._record_failure(candidates, error)
        if result.returncode != 0:
            detail = result.stderr.strip() or f"remote agent exited with code {result.returncode}"
            return self._record_failure(
                candidates,
                self._refresh_error(
                    "agent",
                    detail,
                    returncode=result.returncode,
                    stderr=result.stderr,
                ),
            )

        try:
            queue, accounting, sources, by_run, dependency_by_run, watermarks = self._decode_scan(
                result.stdout, expect_scheduler=bool(job_ids)
            )
        except UserError as exc:
            return self._record_failure(candidates, exc.error)

        changed = 0
        refreshed_at = time.time()
        refreshed: list[str] = []
        db = self._ctx.db()
        tasks = TaskRepo(db)
        runs = RunRepo(db)
        with db:
            for row in candidates:
                dependency_state, dependency_reason = dependency_by_run.get(
                    row.id,
                    (row.env_dependency_state, row.env_dependency_reason),
                )
                dependency_state, dependency_reason = self._scheduler_dependency_state(
                    row,
                    dependency_state,
                    dependency_reason,
                    queue,
                    accounting,
                )
                runs._set_env_dependency(
                    row.id,
                    state=dependency_state,
                    reason=dependency_reason,
                )
                observations = self._scheduler_observations(row, queue, accounting, tasks)
                changed += tasks._apply_scheduler(row.id, observations)
                changed += tasks._apply_artifact(row.id, by_run.get(row.id, []))
                views = [
                    self._task_view(
                        record,
                        is_stale=False,
                        env_dependency_state=dependency_state,
                        env_dependency_reason=dependency_reason,
                    )
                    for record in tasks.status_records(row.id)
                ]
                summary = self._summarize(views)
                runs._record_refresh_success(
                    row.id,
                    summary=summary,
                    sources=sources,
                    refreshed_at=refreshed_at,
                    scan_watermark=watermarks.get(row.id, 0.0),
                )
                if dependency_state in _ENV_DEPENDENCY_TERMINAL or (
                    row.state == RunState.SUBMITTED
                    and summary.total > 0
                    and all(state in _DONE_TASK_STATES for state in summary.counts)
                ):
                    db.execute("UPDATE runs SET state = ? WHERE id = ?", (RunState.TERMINAL, row.id))
                refreshed.append(row.id)
        return RefreshReport(refreshed=refreshed, changed=changed)

    @staticmethod
    def _scheduler_dependency_state(
        row: RunRow,
        state: str,
        reason: str,
        queue: dict[str, SchedulerObservation],
        accounting: dict[str, SchedulerObservation],
    ) -> tuple[str, str]:
        """Converge dependent runs even when no env command refreshed the registry."""
        if (
            row.env_wait_policy != "afterok"
            or not row.env_build_job_id
            or state in {"ready", *_ENV_DEPENDENCY_TERMINAL}
        ):
            return state, reason
        observed = accounting.get(row.env_build_job_id)
        if observed is not None and observed.scheduler_state in TERMINAL_STATES:
            scheduler_state = observed.scheduler_state
            detail = observed.scheduler_reason or observed.exit_code
            suffix = f" ({detail})" if detail and detail != "-" else ""
            if scheduler_state == "CANCELLED":
                return (
                    "ENV_BUILD_CANCELLED",
                    f"Environment build {row.env_build_job_id} was CANCELLED by Slurm{suffix}",
                )
            if scheduler_state in failed_states():
                return (
                    "ENV_BUILD_FAILED",
                    f"Environment build {row.env_build_job_id} ended in {scheduler_state}{suffix}",
                )
            if scheduler_state == "COMPLETED":
                return (
                    "unknown",
                    f"Environment build {row.env_build_job_id} completed but its generation is not READY",
                )
        if row.env_build_job_id in queue:
            return "waiting", reason
        return state, reason

    @staticmethod
    def _decode_scan(
        stdout: str,
        *,
        expect_scheduler: bool,
    ) -> tuple[
        dict[str, SchedulerObservation],
        dict[str, SchedulerObservation],
        list[SchedulerSource],
        dict[str, list[dict[str, object]]],
        dict[str, tuple[str, str]],
        dict[str, float],
    ]:
        queue: dict[str, SchedulerObservation] = {}
        accounting: dict[str, SchedulerObservation] = {}
        sources: list[SchedulerSource] = []
        seen_scheduler: set[str] = set()
        by_run: dict[str, list[dict[str, object]]] = {}
        dependency_by_run: dict[str, tuple[str, str]] = {}
        watermarks: dict[str, float] = {}

        for payload in parse_json_lines(stdout):
            kind = payload.get("kind")
            if kind in (protocol.SCAN_KIND_SQUEUE, protocol.SCAN_KIND_SACCT):
                source = str(payload.get("source", kind))
                returncode = payload.get("returncode")
                stderr = str(payload.get("stderr", "") or "")
                error = str(payload.get("error", "") or "")
                if source != kind:
                    raise UserError(
                        StatusService._refresh_error(
                            source,
                            f"query kind {kind!r} did not match source {source!r}",
                        )
                    )
                if returncode != 0 or error:
                    detail = error or f"{source} query exited with code {returncode}"
                    if stderr.strip():
                        detail = f"{detail}: {stderr.strip()}"
                    raise UserError(
                        StatusService._refresh_error(
                            source,
                            detail,
                            returncode=returncode,
                            stderr=stderr,
                        )
                    )
                try:
                    observed_at = float(payload["observed_at"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise UserError(
                        StatusService._refresh_error(source, "query payload has no valid observation time")
                    ) from exc
                parsed_source = SchedulerSource(source)
                if parsed_source not in sources:
                    sources.append(parsed_source)
                seen_scheduler.add(source)
                output = str(payload.get("output", "") or "")
                if kind == protocol.SCAN_KIND_SQUEUE:
                    queue = dict(parse_squeue(output, observed_at=observed_at))
                else:
                    accounting = dict(parse_sacct(output, observed_at=observed_at))
            elif kind == protocol.SCAN_KIND_TASK:
                run_id = str(payload.get("run_id", ""))
                by_run.setdefault(run_id, []).append(payload)
                mtime = float(payload.get("mtime", 0.0))
                watermarks[run_id] = max(watermarks.get(run_id, 0.0), mtime)
            elif kind == protocol.SCAN_KIND_ENV_DEPENDENCY:
                run_id = payload.get("run_id")
                state = payload.get("state")
                reason = payload.get("reason")
                if (
                    not isinstance(run_id, str)
                    or state not in {"waiting", "ready", "ENV_BUILD_FAILED", "ENV_BUILD_CANCELLED", "unknown"}
                    or not isinstance(reason, str)
                ):
                    raise UserError(
                        StatusService._refresh_error(
                            protocol.SCAN_KIND_ENV_DEPENDENCY,
                            "agent returned an invalid environment dependency payload",
                        )
                    )
                dependency_by_run[run_id] = (str(state), reason)

        if expect_scheduler:
            for source in (protocol.SCAN_KIND_SQUEUE, protocol.SCAN_KIND_SACCT):
                if source not in seen_scheduler:
                    raise UserError(StatusService._refresh_error(source, f"agent returned no {source} query payload"))
        return queue, accounting, sources, by_run, dependency_by_run, watermarks

    @staticmethod
    def _scheduler_observations(
        row: RunRow,
        queue: dict[str, SchedulerObservation],
        accounting: dict[str, SchedulerObservation],
        tasks: TaskRepo,
    ) -> dict[str, SchedulerObservation]:
        if not row.slurm_job_id:
            return {}
        observations: dict[str, SchedulerObservation] = {}
        for spec in tasks.specs(row.id):
            key = f"{row.slurm_job_id}_{spec.index}"
            queue_record = queue.get(key)
            accounting_record = accounting.get(key)
            terminal = next(
                (
                    record
                    for record in (accounting_record, queue_record)
                    if record is not None and record.scheduler_state in TERMINAL_STATES
                ),
                None,
            )
            record = terminal or queue_record or accounting_record
            if record is not None:
                observations[spec.task_id] = record
        return observations

    @staticmethod
    def _summarize(tasks: list[TaskStatusView]) -> RunSummary:
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.effective_state] = counts.get(task.effective_state, 0) + 1
        return RunSummary(total=len(tasks), counts=counts)

    def _record_failure(self, candidates: list[RunRow], error: StructuredError) -> RefreshReport:
        failed_at = time.time()
        db = self._ctx.db()
        runs = RunRepo(db)
        with db:
            for row in candidates:
                runs._record_refresh_failure(row.id, failed_at=failed_at, error=error)
        if all(row.status_refreshed_at > 0 for row in candidates):
            return RefreshReport(is_stale=True, refresh_error=error)
        raise UserError(error)

    @staticmethod
    def _refresh_error(
        source: str,
        detail: str,
        *,
        returncode: object = None,
        stderr: str = "",
    ) -> StructuredError:
        context: dict[str, object] = {"source": source, "error": detail}
        if returncode is not None:
            context["returncode"] = returncode
        if stderr:
            context["stderr"] = stderr
        return StructuredError(
            code="status_refresh_failed",
            summary=f"Status refresh failed ({source}): {detail}",
            detail=detail,
            operation="status.refresh",
            phase=OperationPhase.REFRESH,
            retryable=True,
            remediation="Retry the refresh after the remote scheduler is available.",
            context=context,
        )
