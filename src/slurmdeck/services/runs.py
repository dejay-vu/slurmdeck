"""Run lifecycle: plan (pure local), submit, retry, cancel, clean.

Runs are immutable: every submit creates a new run directory and database row;
there is no mutable "current submission" pointer to corrupt. ``plan`` resolves
every placeholder for every task locally, so what executes on the cluster is
exactly what ``sweep preview`` shows.
"""

from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass, replace

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import InvalidDependencyPolicy
from slurmdeck.models.common import RunState
from slurmdeck.models.env import EnvBinding, EnvWaitPolicy
from slurmdeck.models.remote import Remote
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.operations import OperationPhase, OperationReporter, OperationSink, noop_operation_sink
from slurmdeck.services.context import AppContext
from slurmdeck.services.env_binding import EnvironmentRunBindingService
from slurmdeck.services.run_materialization import RunMaterializer
from slurmdeck.services.run_planning import RunPlanner
from slurmdeck.services.run_recovery import RunRecoveryService
from slurmdeck.services.run_submission import RemoteSubmissionResult, RunSubmissionClient
from slurmdeck.services.snapshots import SnapshotService
from slurmdeck.services.status import StatusService
from slurmdeck.slurm import failed_states, scancel_command
from slurmdeck.storage.repos import RunRepo, RunRow, TaskRepo
from slurmdeck.structured_errors import StructuredError
from slurmdeck.transport import Transport


@dataclass(frozen=True)
class RunCleanReport:
    run_id: str
    local_removed: bool
    remote_removed: bool
    receipt_removed: bool
    snapshot_reference_released: bool
    partial: bool = False
    error: str = ""


class RunService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._snapshots = SnapshotService()
        self._submissions = RunSubmissionClient()

    @property
    def _runs(self) -> RunRepo:
        return RunRepo(self._ctx.db())

    @property
    def _tasks(self) -> TaskRepo:
        return TaskRepo(self._ctx.db())

    # -- lookups -----------------------------------------------------------------

    def get(self, run_id: str | None = None) -> RunRow:
        if run_id is None:
            row = self._runs.latest()
            if row is None:
                raise UserError("No runs in this project yet.", hint="Create one with `slurmdeck submit ...`.")
            return row
        row = self._runs.get(run_id)
        if row is None:
            known = ", ".join(r.id for r in self._runs.list()[:8]) or "none"
            raise UserError(f"Unknown run: {run_id!r} (recent: {known}).")
        return row

    def list_runs(self) -> list[RunRow]:
        return self._runs.list()

    def list_views(self) -> list[RunRow]:
        """Rows projected with the service-owned user-visible dependency state."""
        views = []
        for row in self._runs.list():
            if row.env_dependency_state == "waiting":
                views.append(replace(row, state="WAITING_FOR_ENV"))
            elif row.env_dependency_state in {"ENV_BUILD_FAILED", "ENV_BUILD_CANCELLED"}:
                views.append(replace(row, state=row.env_dependency_state))
            else:
                views.append(row)
        return views

    # -- plan ---------------------------------------------------------------------

    def plan(
        self,
        *,
        command: CommandTemplateSpec,
        sweep: Sweep | None = None,
        sweep_file: str | None = None,
        name: str | None = None,
        overrides: ResourceOverrides | None = None,
        remote: Remote,
        env_binding: EnvBinding | None = None,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> RunRow:
        reporter = OperationReporter("run.plan", operation_sink)
        reporter.started(OperationPhase.VALIDATE, message="Validating run plan")
        try:
            reporter.progress(OperationPhase.SNAPSHOT, message="Computing code snapshot")
            plan = RunPlanner(self._ctx).plan(
                command=command,
                sweep=sweep,
                sweep_file=sweep_file,
                name=name,
                overrides=overrides,
                remote=remote,
                env_binding=env_binding,
            )
            reporter.progress(OperationPhase.RECONCILE, message="Reconciling interrupted run commits")
            RunRecoveryService(self._ctx).reconcile()
            row = RunMaterializer(self._ctx).commit(plan)
        except BaseException as exc:
            reporter.failed(message=str(exc))
            raise
        reporter.completed(OperationPhase.VALIDATE, result_counts={"tasks": row.summary.total})
        return row

    # -- submit ---------------------------------------------------------------------

    def submit(
        self,
        transport: Transport,
        run_id: str | None = None,
        *,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> RunRow:
        reporter = OperationReporter("run.submit", operation_sink)
        reporter.started(OperationPhase.VALIDATE, message="Validating run submission")
        token = ""
        claimed = False
        phase = OperationPhase.VALIDATE
        remote_submission_started = False
        dependency_job_id = ""
        invalid_dependency_policy: InvalidDependencyPolicy | None = None
        snapshot_exists: bool | None = None
        try:
            row = self.get(run_id)
            if row.state in (RunState.SUBMITTING, RunState.SUBMIT_UNKNOWN):
                raise UserError(self._unknown_submission_error(row))
            if row.state not in (RunState.PLANNED, RunState.SUBMIT_FAILED):
                raise UserError(
                    f"Run {row.id} is in state {row.state!r}; only planned or failed submissions can be submitted.",
                    hint="Use `slurmdeck submit ...` to create a new run.",
                )
            reporter.progress(OperationPhase.RECONCILE, message="Reconciling interrupted run commits")
            RunRecoveryService(self._ctx).reconcile()
            row = self.get(row.id)
            project = self._ctx.require_project()
            remote = self._ctx.user_store.read_remote(row.remote)
            layout = self._ctx.layout(remote)
            run_dir = project.paths.run_dir(row.id)
            binding = row.env_binding
            if binding is not None:
                phase = OperationPhase.ENVIRONMENT
                reporter.progress(OperationPhase.ENVIRONMENT, message=f"Checking environment {binding.env_id}")
                check = EnvironmentRunBindingService().check(
                    transport,
                    layout,
                    binding,
                    snapshot_hash=row.snapshot_hash,
                )
                EnvironmentRunBindingService.require_for_submit(check, binding)
                snapshot_exists = check.snapshot_exists
                if binding.wait_policy is EnvWaitPolicy.AFTEROK and check.state == "waiting":
                    profile = remote.cluster
                    policy = profile.slurm.kill_invalid_dependency if profile is not None else None
                    if (
                        profile is None
                        or profile.slurm.afterok_dependency is not True
                        or policy not in {InvalidDependencyPolicy.PER_JOB, InvalidDependencyPolicy.SITE_WIDE}
                    ):
                        raise UserError(
                            "afterok is no longer permitted by the current cluster profile.",
                            hint="Use a READY environment or restore an explicit invalid-dependency policy.",
                        )
                    dependency_job_id = binding.build_job_id
                    invalid_dependency_policy = policy
                    self._runs.set_env_dependency(
                        row.id,
                        state="waiting",
                        reason=f"Waiting for environment {binding.env_id} build {binding.build_job_id}",
                    )
                else:
                    self._runs.set_env_dependency(row.id, state="ready", reason="Environment generation is READY")

            token = secrets.token_hex(32)
            claimed = self._runs.begin_submission(row.id, token=token, phase=OperationPhase.VALIDATE.value)
            if not claimed:
                current = self.get(row.id)
                if current.state in (RunState.SUBMITTING, RunState.SUBMIT_UNKNOWN):
                    raise UserError(self._unknown_submission_error(current))
                raise UserError(f"Run {row.id} changed state while its submission was starting; retry the command.")

            phase = OperationPhase.SNAPSHOT
            self._runs.set_submission_phase(row.id, token=token, phase=phase.value)
            reporter.progress(OperationPhase.SNAPSHOT, message="Ensuring code snapshot on the remote")
            snapshot = self._snapshots.ensure(
                transport,
                layout,
                project.paths.root,
                project.config.sync,
                known_exists=snapshot_exists,
                operation_sink=operation_sink,
            )
            if snapshot.hash != row.snapshot_hash:
                raise UserError(
                    f"The working tree changed since run {row.id} was planned "
                    f"(snapshot {snapshot.hash[:12]} != planned {row.snapshot_hash[:12]}).",
                    hint="Plan and submit in one go with `slurmdeck submit ...`, or re-plan the run.",
                )
            phase = OperationPhase.UPLOAD
            self._runs.set_submission_phase(row.id, token=token, phase=phase.value)
            reporter.progress(OperationPhase.UPLOAD, message="Uploading run directory")
            transport.upload(f"{run_dir}/", f"{row.remote_root}/", delete=True, timeout=900)
            phase = OperationPhase.SUBMIT
            self._runs.set_submission_phase(row.id, token=token, phase=phase.value)
            reporter.progress(OperationPhase.SUBMIT, message="Submitting Slurm array")
            remote_submission_started = True
            result = self._submissions.submit(
                transport,
                layout,
                row,
                token,
                dependency_job_id=dependency_job_id,
                invalid_dependency_policy=invalid_dependency_policy,
            )
            if result.status != "submitted":
                error = self._submission_result_error(row, result, operation="run.submit")
                state = RunState.SUBMIT_UNKNOWN if result.status == "unknown" else RunState.SUBMIT_FAILED
                self._runs.record_submission_error(
                    row.id,
                    token=token,
                    state=state,
                    phase=phase.value,
                    error=error,
                )
                claimed = False
                raise UserError(error)
            if not self._runs.record_submission(
                row.id,
                slurm_job_id=result.job_id,
                snapshot_hash=snapshot.hash,
                env_id=row.env_id,
                token=token,
            ):
                raise RuntimeError(f"Submission token changed while recording job {result.job_id} for {row.id}.")
        except BaseException as exc:
            if claimed:
                unknown = remote_submission_started
                error = self._submission_exception_error(
                    row,
                    token=token,
                    phase=phase,
                    unknown=unknown,
                    operation="run.submit",
                    cause=exc,
                )
                self._runs.record_submission_error(
                    row.id,
                    token=token,
                    state=RunState.SUBMIT_UNKNOWN if unknown else RunState.SUBMIT_FAILED,
                    phase=phase.value,
                    error=error,
                )
                reporter.failed(message=error.summary)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise UserError(error) from exc
            reporter.failed(message=str(exc))
            raise
        refreshed = self._runs.get(row.id)
        assert refreshed is not None
        reporter.completed(OperationPhase.SUBMIT, result_counts={"submitted": 1})
        return refreshed

    def reconcile(
        self,
        transport: Transport,
        run_id: str | None = None,
        *,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> RunRow:
        """Resolve an uncertain submission without ever issuing another sbatch."""
        reporter = OperationReporter("run.reconcile", operation_sink)
        reporter.started(OperationPhase.RECONCILE, message="Reconciling remote submission")
        row = self.get(run_id)
        remote_started = False
        try:
            if row.state == RunState.SUBMITTED:
                reporter.completed(OperationPhase.RECONCILE, result_counts={"submitted": 1})
                return row
            if row.state not in (RunState.SUBMITTING, RunState.SUBMIT_UNKNOWN) or not row.submission_token:
                raise UserError(
                    f"Run {row.id} has no uncertain submission to reconcile (state: {row.state}).",
                    hint="Use `slurmdeck run submit` for planned or failed submissions.",
                )
            RunRecoveryService(self._ctx).reconcile()
            remote = self._ctx.user_store.read_remote(row.remote)
            layout = self._ctx.layout(remote)
            self._runs.set_submission_phase(
                row.id,
                token=row.submission_token,
                phase=OperationPhase.RECONCILE.value,
            )
            remote_started = True
            result = self._submissions.reconcile(transport, layout, row)
        except BaseException as exc:
            if not remote_started:
                reporter.failed(message=str(exc))
                raise
            error = StructuredError(
                code="run_reconcile_failed",
                summary=f"Could not reconcile submission for run {row.id}.",
                detail=str(exc),
                operation="run.reconcile",
                phase=OperationPhase.RECONCILE,
                retryable=True,
                remediation=f"Retry `slurmdeck run reconcile {row.id}`; it will not submit another job.",
                context={"run_id": row.id, "token": row.submission_token},
                underlying_cause=exc,
            )
            self._runs.record_submission_error(
                row.id,
                token=row.submission_token,
                state=RunState.SUBMIT_UNKNOWN,
                phase=OperationPhase.RECONCILE.value,
                error=error,
            )
            reporter.failed(message=error.summary)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise UserError(error) from exc

        if result.status == "submitted":
            if not self._runs.record_submission(
                row.id,
                slurm_job_id=result.job_id,
                snapshot_hash=row.snapshot_hash,
                env_id=row.env_id,
                token=row.submission_token,
            ):
                raise UserError(f"Run {row.id} changed submission token during reconciliation.")
            refreshed = self.get(row.id)
            reporter.completed(OperationPhase.RECONCILE, result_counts={"submitted": 1})
            return refreshed

        error = self._submission_result_error(row, result, operation="run.reconcile")
        state = RunState.SUBMIT_UNKNOWN if result.status == "unknown" else RunState.SUBMIT_FAILED
        self._runs.record_submission_error(
            row.id,
            token=row.submission_token,
            state=state,
            phase=(OperationPhase.RECONCILE if result.status == "unknown" else OperationPhase.SUBMIT).value,
            error=error,
        )
        reporter.failed(message=error.summary)
        raise UserError(error)

    @staticmethod
    def _unknown_submission_error(row: RunRow) -> StructuredError:
        return StructuredError(
            code="run_submit_unknown",
            summary=f"Run {row.id} may already have been submitted.",
            detail="The previous submission did not return a trustworthy Slurm job id.",
            operation="run.submit",
            phase=OperationPhase.SUBMIT,
            retryable=False,
            remediation=f"Run `slurmdeck run reconcile {row.id}`; do not resubmit it.",
            context={"run_id": row.id, "token": row.submission_token},
        )

    @classmethod
    def _submission_result_error(
        cls,
        row: RunRow,
        result: RemoteSubmissionResult,
        *,
        operation: str,
    ) -> StructuredError:
        if result.status == "unknown":
            error = cls._unknown_submission_error(row)
            return error.model_copy(
                update={
                    "detail": result.error or error.detail,
                    "operation": operation,
                    "context": {
                        "run_id": row.id,
                        "token": result.token,
                        "source": result.source,
                    },
                }
            )
        return StructuredError(
            code="run_submit_failed",
            summary=f"Slurm rejected submission for run {row.id}.",
            detail=result.error or "The remote submission helper reported a failure.",
            operation=operation,
            phase=OperationPhase.SUBMIT,
            retryable=True,
            remediation=f"Fix the reported problem, then retry `slurmdeck run submit {row.id}`.",
            context={"run_id": row.id, "token": result.token, "source": result.source},
        )

    @staticmethod
    def _submission_exception_error(
        row: RunRow,
        *,
        token: str,
        phase: OperationPhase,
        unknown: bool,
        operation: str,
        cause: BaseException,
    ) -> StructuredError:
        cause_summary = cause.error.summary if isinstance(cause, UserError) else str(cause)
        return StructuredError(
            code="run_submit_unknown" if unknown else "run_submit_failed",
            summary=(
                f"Run {row.id} may already have been submitted."
                if unknown
                else f"Submission for run {row.id} failed during {phase.value}: {cause_summary}"
            ),
            detail=str(cause),
            operation=operation,
            phase=phase,
            retryable=not unknown,
            remediation=(
                f"Run `slurmdeck run reconcile {row.id}`; do not resubmit it."
                if unknown
                else f"Fix the reported problem, then retry `slurmdeck run submit {row.id}`."
            ),
            context={"run_id": row.id, "token": token},
            underlying_cause=cause,
        )

    # -- retry -----------------------------------------------------------------------

    def retry(
        self,
        run_id: str | None = None,
        *,
        task_ids: list[str] | None = None,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> RunRow:
        """Plan a new run containing the selected (default: failed) tasks of ``run_id``.

        The original run's resources (including its CLI overrides) and command
        template are reused; task ids keep their original names so results
        stay cross-referenceable.
        """
        reporter = OperationReporter("run.retry", operation_sink)
        reporter.started(OperationPhase.VALIDATE, message="Validating retry selection")
        try:
            source = self.get(run_id)
            records = {record.spec.task_id: record for record in self._tasks.planned_records(source.id)}
            if task_ids is None:
                failed = failed_states() | {"FAILED", "KILLED"}
                rows = StatusService(self._ctx).snapshot(source.id).tasks
                task_ids = [row.task_id for row in rows if row.effective_state in failed]
            remote = self._ctx.user_store.read_remote(source.remote)
            plan = RunPlanner(self._ctx).retry(
                source=source,
                records=records,
                task_ids=task_ids or (),
                remote=remote,
            )
            reporter.progress(OperationPhase.RECONCILE, message="Reconciling interrupted run commits")
            RunRecoveryService(self._ctx).reconcile()
            row = RunMaterializer(self._ctx).commit(plan)
        except BaseException as exc:
            reporter.failed(message=str(exc))
            raise
        reporter.completed(OperationPhase.VALIDATE, result_counts={"tasks": row.summary.total})
        return row

    # -- cancel / clean -----------------------------------------------------------------

    def cancel(self, transport: Transport, run_id: str | None = None) -> RunRow:
        row = self.get(run_id)
        if not row.slurm_job_id:
            raise UserError(f"Run {row.id} has no Slurm job to cancel (state: {row.state}).")
        RunRecoveryService(self._ctx).reconcile()
        transport.exec(scancel_command([row.slurm_job_id]))
        self._runs.set_state(row.id, RunState.CANCELLED)
        refreshed = self._runs.get(row.id)
        assert refreshed is not None
        return refreshed

    def clean(self, run_id: str, *, transport: Transport | None = None) -> RunCleanReport:
        """Delete a run locally, and its remote directory when a transport is given."""
        row = self.get(run_id)
        if transport is not None and not row.remote_root.endswith(f"/runs/{row.id}"):
            raise UserError(
                f"Refusing to delete unexpected remote path: {row.remote_root!r}.",
            )
        RunRecoveryService(self._ctx).reconcile()
        remote_removed = False
        receipt_removed = False
        if transport is not None:
            remote = self._ctx.user_store.read_remote(row.remote)
            remote_result = self._submissions.clean(transport, self._ctx.layout(remote), row)
            if not remote_result.ok:
                return RunCleanReport(
                    run_id=row.id,
                    local_removed=False,
                    remote_removed=remote_result.removed_run,
                    receipt_removed=remote_result.removed_receipt,
                    snapshot_reference_released=False,
                    partial=True,
                    error=remote_result.error or "remote cleanup did not complete",
                )
            # A successful idempotent helper result means both paths are now
            # absent, even if this particular call did not remove them.
            remote_removed = True
            receipt_removed = True
        run_dir = self._ctx.require_project().paths.run_dir(row.id)
        try:
            if run_dir.exists():
                shutil.rmtree(run_dir)
        except OSError as exc:
            return RunCleanReport(
                run_id=row.id,
                local_removed=False,
                remote_removed=remote_removed,
                receipt_removed=receipt_removed,
                snapshot_reference_released=remote_removed and receipt_removed,
                partial=True,
                error=str(exc),
            )
        self._runs.delete(row.id)
        return RunCleanReport(
            run_id=row.id,
            local_removed=True,
            remote_removed=remote_removed,
            receipt_removed=receipt_removed,
            snapshot_reference_released=remote_removed and receipt_removed,
        )
