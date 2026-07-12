"""DeckController: the only place the TUI touches transports and remote services.

Screens read SQLite directly (cheap, never blocks) and call controller methods
for everything that goes over SSH. The controller runs remote work in thread
workers, reports through the messages in :mod:`slurmdeck.tui.messages`, and
serializes mutations so only one guarded operation runs at a time.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from slurmdeck.errors import UserError
from slurmdeck.models.env import EnvironmentRecord, EnvironmentView
from slurmdeck.models.remote import Remote
from slurmdeck.models.sweep import Sweep
from slurmdeck.operations import OperationEvent, OperationPhase, OperationReporter, OperationSink
from slurmdeck.services.context import AppContext
from slurmdeck.services.doctor import Check, DoctorService
from slurmdeck.services.env_binding import EnvironmentRunBindingService
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_execution import EnvironmentPreparationService
from slurmdeck.services.env_lifecycle import EnvironmentGcReport, EnvironmentLifecycleService, EnvironmentLog
from slurmdeck.services.env_planning import EnvironmentPlanner
from slurmdeck.services.logs import LogService, RunLog
from slurmdeck.services.remotes import ConnectReport, RemoteService
from slurmdeck.services.results import PullReport, ResultsService
from slurmdeck.services.runs import RunCleanReport, RunService
from slurmdeck.services.status import StatusService
from slurmdeck.storage.repos import RunRow
from slurmdeck.storage.yamlio import load_yaml_model
from slurmdeck.transport import StreamHandle, Transport, TransportError
from slurmdeck.tui.drafts import NewRunDraft, ProfileDraft, RemoteDraft
from slurmdeck.tui.format import ACTIVE_TASK_STATES
from slurmdeck.tui.messages import (
    OperationFinished,
    OperationProgressed,
    OperationStarted,
    RefreshFinished,
    RefreshStarted,
)

if TYPE_CHECKING:
    from textual.app import App

T = TypeVar("T")

#: Refresh cadence while at least one run is actively on the scheduler.
FAST_INTERVAL = 15.0
#: Cadence while only cancelled runs are still settling their final artifacts.
SLOW_INTERVAL = 60.0


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, UserError) and exc.hint:
        return f"{exc} — {exc.hint}"
    if isinstance(exc, (UserError, TransportError)):
        return str(exc)
    return repr(exc)


def refresh_interval(runs: Sequence[RunRow]) -> float | None:
    """Auto-refresh policy: fast while submitted runs exist, slow while
    cancelled runs still have unsettled tasks, paused otherwise."""
    if any(row.state == "submitted" for row in runs):
        return FAST_INTERVAL
    for row in runs:
        if row.state != "cancelled":
            continue
        if row.summary.total == 0 or any(state in ACTIVE_TASK_STATES for state in row.summary.counts):
            return SLOW_INTERVAL
    return None


class DeckController:
    def __init__(self, app: App[None], ctx: AppContext) -> None:
        self._app = app
        self._ctx = ctx
        self._transports: dict[str, Transport] = {}
        self.auto_refresh = True
        self.refreshing = False
        self.operation: str | None = None
        self.connection: str = "unknown"  # unknown | ok | error
        self.last_refresh_at: float | None = None
        self._last_attempt = 0.0
        self._operation_sequence = 0

    @property
    def ctx(self) -> AppContext:
        return self._ctx

    def transport_for(self, remote: Remote) -> Transport:
        transport = self._transports.get(remote.name)
        if transport is None:
            transport = self._ctx.transport(remote)
            self._transports[remote.name] = transport
        return transport

    # -- status refresh ----------------------------------------------------------

    def maybe_auto_refresh(self) -> None:
        """Timer hook: refresh when the adaptive interval says it is due."""
        if not self.auto_refresh or self.refreshing or self._ctx.project is None:
            return
        interval = refresh_interval(RunService(self._ctx).list_runs())
        if interval is None:
            return
        if time.time() - self._last_attempt >= interval:
            self.refresh_now()

    def refresh_now(self, run_ids: list[str] | None = None) -> None:
        """Refresh scheduler + artifact state for refreshable runs (one worker)."""
        if self.refreshing or self._ctx.project is None:
            return
        self.refreshing = True
        self._last_attempt = time.time()
        self._app.post_message(RefreshStarted())
        self._app.run_worker(
            lambda: self._refresh_body(run_ids),
            thread=True,
            group="deck-refresh",
            exclusive=True,
            name="status refresh",
        )

    def _refresh_body(self, run_ids: list[str] | None) -> None:
        try:
            ctx = self._ctx
            runs = RunService(ctx).list_runs()
            selected = [
                row
                for row in runs
                if row.state in ("submitted", "cancelled") and (run_ids is None or row.id in run_ids)
            ]
            by_remote: dict[str, list[str]] = {}
            for row in selected:
                by_remote.setdefault(row.remote, []).append(row.id)
            changed = 0
            stale_error = ""
            for remote_name, ids in by_remote.items():
                remote = ctx.user_store.read_remote(remote_name)
                report = StatusService(ctx).refresh(self.transport_for(remote), ctx.layout(remote), ids)
                changed += report.changed
                if report.is_stale and report.refresh_error is not None and not stale_error:
                    stale_error = report.refresh_error.summary
        except Exception as exc:
            # schedule the next attempt from completion: on slow links a
            # refresh can outlast the interval, which must not mean
            # "permanently refreshing"
            self._last_attempt = time.time()
            self.refreshing = False
            if isinstance(exc, TransportError):
                self.connection = "error"
            self._app.post_message(
                RefreshFinished(
                    ok=False,
                    error=describe_error(exc),
                    transport_error=isinstance(exc, TransportError),
                    stale=self.last_refresh_at is not None,
                )
            )
            return
        self._last_attempt = time.time()
        self.refreshing = False
        if stale_error:
            self._app.post_message(RefreshFinished(ok=True, changed=changed, error=stale_error, stale=True))
            return
        self.last_refresh_at = time.time()
        self.connection = "ok"
        self._app.post_message(RefreshFinished(ok=True, changed=changed))

    # -- guarded operations --------------------------------------------------------

    def run_operation(
        self,
        label: str,
        fn: Callable[[OperationSink], T],
        *,
        operation: str | None = None,
        phase: OperationPhase | None = None,
        success: Callable[[T], str] | None = None,
        on_result: Callable[[T], None] | None = None,
        refresh_after: bool = False,
        mutation: bool = True,
    ) -> bool:
        """Run remote work in a background thread with progress + result routing.

        Mutations share one lock. Read-only work uses an independent worker
        group and is never rejected merely because a mutation is active.
        """
        if mutation and self.operation is not None:
            self._app.notify(f"{self.operation} is still running.", severity="warning")
            return False
        if mutation:
            self.operation = label
        self._operation_sequence += 1
        operation_id = f"{operation or 'operation'}:{self._operation_sequence}"
        started_at = time.monotonic()
        self._app.post_message(OperationStarted(operation_id, label, mutation=mutation, started_at=started_at))

        def body() -> None:
            def operation_sink(event: OperationEvent) -> None:
                self._app.post_message(OperationProgressed(operation_id, event))

            reporter: OperationReporter | None = None
            if operation is not None and phase is not None:
                reporter = OperationReporter(operation, operation_sink)
                reporter.started(phase, message=label)

            try:
                result = fn(operation_sink)
            except Exception as exc:
                if reporter is not None:
                    reporter.failed(message=str(exc))
                if mutation:
                    self.operation = None
                if isinstance(exc, TransportError):
                    self.connection = "error"
                self._app.post_message(
                    OperationFinished(
                        operation_id,
                        label,
                        ok=False,
                        error=describe_error(exc),
                        transport_error=isinstance(exc, TransportError),
                        elapsed=time.monotonic() - started_at,
                    )
                )
                return
            if reporter is not None:
                reporter.completed(phase or OperationPhase.VALIDATE)
            if mutation:
                self.operation = None
            if on_result is not None:
                self._app.call_from_thread(on_result, result)
            self._app.post_message(
                OperationFinished(
                    operation_id,
                    label,
                    ok=True,
                    message=success(result) if success else "",
                    elapsed=time.monotonic() - started_at,
                )
            )
            if refresh_after:
                self._app.call_from_thread(self.refresh_now)

        self._app.run_worker(
            body,
            thread=True,
            group="deck-mutations" if mutation else "deck-read",
            exclusive=mutation,
            name=label,
        )
        return True

    # -- run actions -----------------------------------------------------------

    def create_run(self, draft: NewRunDraft) -> None:
        def task(operation_sink: OperationSink) -> RunRow:
            ctx = self._ctx
            project = ctx.require_project()
            remote = ctx.resolve_remote()
            transport = self.transport_for(remote)
            sweep = (
                load_yaml_model(
                    draft.sweep_file,
                    Sweep,
                    hint="Validate it with `slurmdeck sweep validate`.",
                )
                if draft.sweep_file is not None
                else None
            )
            env_binding = EnvironmentRunBindingService().resolve(
                transport=transport,
                remote=remote,
                layout=ctx.layout(remote),
                project=project.config,
                project_dir=project.paths.root,
                wait_policy=draft.env_wait,
            )
            runs = RunService(ctx)
            row = runs.plan(
                command=draft.command,
                sweep=sweep,
                sweep_file=str(draft.sweep_file) if draft.sweep_file is not None else None,
                name=draft.name,
                overrides=draft.overrides,
                remote=remote,
                env_binding=env_binding,
                operation_sink=operation_sink,
            )
            if draft.submit:
                row = runs.submit(transport, row.id, operation_sink=operation_sink)
            return row

        self.run_operation(
            "Creating run",
            task,
            success=lambda row: (
                f"Submitted {row.id} as job {row.slurm_job_id}."
                if draft.submit
                else f"Planned {row.id}; submit it when ready."
            ),
            refresh_after=draft.submit,
        )

    def submit_planned(self, run_id: str) -> None:
        def task(operation_sink: OperationSink) -> RunRow:
            ctx = self._ctx
            row = RunService(ctx).get(run_id)
            remote = ctx.user_store.read_remote(row.remote)
            return RunService(ctx).submit(self.transport_for(remote), run_id, operation_sink=operation_sink)

        self.run_operation(
            f"Submitting {run_id}",
            task,
            success=lambda row: f"Submitted {run_id} as job {row.slurm_job_id}.",
            refresh_after=True,
        )

    def cancel_run(self, run_id: str) -> None:
        def task(_operation_sink: OperationSink) -> RunRow:
            ctx = self._ctx
            row = RunService(ctx).get(run_id)
            remote = ctx.user_store.read_remote(row.remote)
            return RunService(ctx).cancel(self.transport_for(remote), run_id)

        self.run_operation(
            f"Cancelling {run_id}",
            task,
            operation="run.cancel",
            phase=OperationPhase.CLEANUP,
            success=lambda _: f"Cancelled {run_id}.",
            refresh_after=True,
        )

    def retry_run(self, run_id: str) -> None:
        def task(operation_sink: OperationSink) -> RunRow:
            ctx = self._ctx
            source = RunService(ctx).get(run_id)
            remote = ctx.user_store.read_remote(source.remote)
            transport = self.transport_for(remote)
            StatusService(ctx).refresh(transport, ctx.layout(remote), [run_id], operation_sink=operation_sink)
            new_row = RunService(ctx).retry(run_id, operation_sink=operation_sink)
            return RunService(ctx).submit(transport, new_row.id, operation_sink=operation_sink)

        self.run_operation(
            f"Retrying {run_id}",
            task,
            success=lambda row: f"Retry {row.id} submitted (job {row.slurm_job_id}).",
            refresh_after=True,
        )

    def pull_run(self, run_id: str, destination: Path) -> None:
        def task(operation_sink: OperationSink) -> PullReport:
            ctx = self._ctx
            row = RunService(ctx).get(run_id)
            remote = ctx.user_store.read_remote(row.remote)
            report = ResultsService(ctx).pull(
                self.transport_for(remote),
                row,
                into=destination,
                operation_sink=operation_sink,
            )
            if report.matched == 0:
                raise UserError(
                    f"No files matched run {run_id}; nothing was pulled.",
                    hint="Wait for results or choose a different task/log selection.",
                )
            return report

        self.run_operation(
            f"Pulling {run_id}",
            task,
            success=lambda report: (
                f"Pulled {report.transferred}/{report.matched} files ({report.bytes} bytes) into "
                f"{report.destination}; {report.skipped} skipped, {report.failed} failed."
            ),
        )

    def clean_run(self, run_id: str) -> None:
        def task(_operation_sink: OperationSink) -> RunCleanReport:
            ctx = self._ctx
            row = RunService(ctx).get(run_id)
            transport: Transport | None = None
            if row.remote_root:
                remote = ctx.user_store.read_remote(row.remote)
                transport = self.transport_for(remote)
            report = RunService(ctx).clean(run_id, transport=transport)
            if report.partial:
                raise UserError(
                    f"Clean incomplete for {run_id}: {report.error}",
                    hint="Retry clean; the local run was retained as a recovery handle.",
                )
            return report

        self.run_operation(
            f"Cleaning {run_id}",
            task,
            operation="run.clean",
            phase=OperationPhase.CLEANUP,
            success=lambda _: f"Cleaned {run_id}.",
            refresh_after=True,
        )

    # -- environments ------------------------------------------------------------

    def prepare_env(self, *, rebuild: bool = False, after: Callable[[], None] | None = None) -> None:
        def task(_operation_sink: OperationSink) -> EnvironmentRecord:
            ctx = self._ctx
            project = ctx.require_project()
            spec = project.config.env
            if spec is None:
                raise UserError(
                    "No environment configured for this project.",
                    hint="Add an `env:` section to .slurmdeck/project.yaml first.",
                )
            remote = ctx.resolve_remote()
            return (
                EnvironmentPreparationService(cache=EnvironmentCache(ctx.user_paths))
                .prepare(
                    transport=self.transport_for(remote),
                    remote=remote,
                    layout=ctx.layout(remote),
                    project=project.config,
                    project_dir=project.paths.root,
                    rebuild=rebuild,
                    wait=False,
                )
                .record
            )

        self.run_operation(
            "Rebuilding environment" if rebuild else "Preparing environment",
            task,
            operation="env.prepare",
            phase=OperationPhase.ENVIRONMENT,
            success=lambda record: f"Environment {record.env_id} is {record.status}.",
            on_result=(lambda _result: after()) if after else None,
        )

    def list_envs(self, on_result: Callable[[list[EnvironmentView]], None]) -> None:
        def task(_operation_sink: OperationSink) -> list[EnvironmentView]:
            ctx = self._ctx
            remote = ctx.resolve_remote()
            desired_env_id = None
            project = ctx.project
            if project is not None and project.config.env is not None:
                with suppress(UserError):
                    desired_env_id = (
                        EnvironmentPlanner()
                        .plan(
                            project=project.config,
                            project_dir=project.paths.root,
                            layout=ctx.layout(remote),
                            profile=remote.cluster,
                            observation=None,
                            registry=[],
                        )
                        .env_id
                    )
            views = EnvironmentLifecycleService().list(
                self.transport_for(remote),
                ctx.layout(remote),
                desired_env_id=desired_env_id,
            )
            EnvironmentCache(ctx.user_paths).remember_registry(remote, [view.record for view in views])
            return views

        self.run_operation(
            "Loading environments",
            task,
            operation="env.list",
            phase=OperationPhase.REFRESH,
            on_result=on_result,
            mutation=False,
        )

    def remove_env(self, env_id: str, *, after: Callable[[], None] | None = None) -> None:
        def task(_operation_sink: OperationSink) -> None:
            ctx = self._ctx
            remote = ctx.resolve_remote()
            result = EnvironmentLifecycleService().remove(self.transport_for(remote), ctx.layout(remote), env_id)
            EnvironmentCache(ctx.user_paths).remember_record(remote, result.record)

        self.run_operation(
            f"Removing environment {env_id}",
            task,
            operation="env.remove",
            phase=OperationPhase.CLEANUP,
            success=lambda _: f"Removed {env_id}.",
            on_result=(lambda _result: after()) if after else None,
        )

    def cancel_env(self, env_id: str, *, after: Callable[[], None] | None = None) -> None:
        def task(_operation_sink: OperationSink) -> EnvironmentRecord:
            ctx = self._ctx
            remote = ctx.resolve_remote()
            record = EnvironmentLifecycleService().cancel(self.transport_for(remote), ctx.layout(remote), env_id)
            EnvironmentCache(ctx.user_paths).remember_record(remote, record)
            return record

        self.run_operation(
            f"Cancelling environment {env_id}",
            task,
            operation="env.cancel",
            phase=OperationPhase.CLEANUP,
            success=lambda record: f"Environment {record.env_id} is {record.status.value}.",
            on_result=(lambda _result: after()) if after else None,
        )

    def gc_envs(self, *, delete: bool, on_result: Callable[[EnvironmentGcReport], None]) -> None:
        def task(_operation_sink: OperationSink) -> EnvironmentGcReport:
            ctx = self._ctx
            remote = ctx.resolve_remote()
            return EnvironmentLifecycleService().gc(
                self.transport_for(remote),
                ctx.layout(remote),
                delete=delete,
            )

        self.run_operation(
            "Deleting environment garbage" if delete else "Scanning environment garbage",
            task,
            operation="env.gc",
            phase=OperationPhase.CLEANUP,
            success=lambda report: (
                f"Deleted {len(report.deleted)} item(s); {len(report.failed)} failed."
                if delete
                else f"Found {len(report.candidates)} safe GC candidate(s)."
            ),
            on_result=on_result,
            mutation=delete,
        )

    # -- remotes ------------------------------------------------------------------

    def add_remote(self, draft: RemoteDraft, *, after: Callable[[], None] | None = None) -> None:
        def task(_operation_sink: OperationSink) -> Remote:
            return RemoteService(self._ctx).add(
                draft.name,
                host=draft.destination if draft.method == "host" else None,
                ssh_alias=draft.destination if draft.method == "ssh_alias" else None,
                base=draft.base,
                host_key_policy=draft.host_key_policy,
                use=draft.use,
            )

        self.run_operation(
            f"Adding remote {draft.name}",
            task,
            operation="remote.add",
            phase=OperationPhase.VALIDATE,
            success=lambda remote: f"Added {remote.name} ({remote.destination}).",
            on_result=(lambda _result: after()) if after else None,
        )

    def save_profile(self, draft: ProfileDraft, *, after: Callable[[], None] | None = None) -> None:
        def task(_operation_sink: OperationSink) -> Remote:
            return RemoteService(self._ctx).replace_profile(draft.remote_name, draft.profile)

        self.run_operation(
            f"Saving profile for {draft.remote_name}",
            task,
            operation="remote.profile.set",
            phase=OperationPhase.VALIDATE,
            success=lambda remote: f"Saved cluster profile for {remote.name}.",
            on_result=(lambda _result: after()) if after else None,
        )

    def connect_remote(self, name: str, *, after: Callable[[], None] | None = None) -> None:
        def task(_operation_sink: OperationSink) -> ConnectReport:
            return RemoteService(self._ctx).connect(name)

        self.run_operation(
            f"Connecting {name}",
            task,
            operation="remote.connect",
            phase=OperationPhase.CONNECT,
            success=lambda report: f"Connected {report.remote}: {report.resolved_base}",
            on_result=(lambda _result: after()) if after else None,
        )

    def disconnect_remote(self, name: str) -> None:
        def task(_operation_sink: OperationSink) -> str:
            return RemoteService(self._ctx).disconnect(name)

        self.run_operation(
            f"Disconnecting {name}",
            task,
            operation="remote.disconnect",
            phase=OperationPhase.CLEANUP,
            success=lambda _: f"Disconnected {name}.",
        )

    def doctor(self, remote_name: str | None, on_result: Callable[[list[Check]], None]) -> None:
        def task(operation_sink: OperationSink) -> list[Check]:
            return DoctorService(self._ctx).run(remote_name=remote_name, operation_sink=operation_sink)

        self.run_operation(
            "Running doctor",
            task,
            operation="remote.doctor",
            phase=OperationPhase.PROBE,
            on_result=on_result,
            mutation=False,
        )

    # -- logs -----------------------------------------------------------------------

    def fetch_env_log(
        self,
        env_id: str,
        *,
        stream: str | None,
        lines: int,
        on_result: Callable[[EnvironmentLog], None],
        on_error: Callable[[str], None],
    ) -> None:
        def body() -> None:
            try:
                ctx = self._ctx
                remote = ctx.resolve_remote()
                log = EnvironmentLifecycleService().logs(
                    self.transport_for(remote),
                    ctx.layout(remote),
                    env_id,
                    stream=stream,
                    lines=lines,
                )
            except Exception as exc:
                self._app.call_from_thread(on_error, describe_error(exc))
                return
            self._app.call_from_thread(on_result, log)

        self._app.run_worker(body, thread=True, group="deck-env-logs", exclusive=True, name="environment log fetch")

    def follow_env_log(self, env_id: str, *, stream: str | None, on_line: Callable[[str], None]) -> EnvironmentLog:
        ctx = self._ctx
        remote = ctx.resolve_remote()
        return EnvironmentLifecycleService().follow_logs(
            self.transport_for(remote),
            ctx.layout(remote),
            env_id,
            stream=stream,
            on_line=on_line,
        )

    def fetch_log(
        self,
        run_id: str,
        task_id: str,
        *,
        stream: str,
        lines: int,
        on_result: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.fetch_log_view(
            run_id,
            task_id,
            stream=stream,
            lines=lines,
            on_result=lambda log: on_result(log.text),
            on_error=on_error,
        )

    def fetch_log_view(
        self,
        run_id: str,
        task_id: str,
        *,
        stream: str | None,
        lines: int,
        on_result: Callable[[RunLog], None],
        on_error: Callable[[str], None],
    ) -> None:
        def body() -> None:
            try:
                ctx = self._ctx
                row = RunService(ctx).get(run_id)
                remote = ctx.user_store.read_remote(row.remote)
                log = LogService(ctx).fetch(
                    self.transport_for(remote),
                    row.id,
                    task_id=task_id,
                    stream=None if stream is None else "stderr" if stream == "err" else "stdout",
                    lines=lines,
                )
            except Exception as exc:
                self._app.call_from_thread(on_error, describe_error(exc))
                return
            self._app.call_from_thread(on_result, log)

        self._app.run_worker(body, thread=True, group="deck-logs", exclusive=True, name="log fetch")

    def follow_log(self, run_id: str, task_id: str, *, stream: str, on_line: Callable[[str], None]) -> StreamHandle:
        """Start a follow stream; raises UserError/TransportError synchronously."""
        ctx = self._ctx
        row = RunService(ctx).get(run_id)
        remote = ctx.user_store.read_remote(row.remote)
        log = LogService(ctx).follow(
            self.transport_for(remote),
            row.id,
            task_id=task_id,
            stream="stderr" if stream == "err" else "stdout",
            on_line=on_line,
        )
        assert log.handle is not None
        return log.handle
