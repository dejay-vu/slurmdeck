"""Service-owned task-log selection, fetching, and following."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from slurmdeck.errors import UserError
from slurmdeck.models.status import TaskStatusView
from slurmdeck.services.context import AppContext
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import StatusService
from slurmdeck.slurm import failed_states
from slurmdeck.storage.repos import RunRow, TaskRepo
from slurmdeck.transport import ExecResult, StreamHandle, Transport

_FAILED_STATES = failed_states() | {"FAILED", "KILLED"}


class LogStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True)
class RunLog:
    run_id: str
    task_id: str
    stream: LogStream
    path: str
    text: str
    handle: StreamHandle | None = None


class LogService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def _task(self, run: RunRow, tasks: Sequence[TaskStatusView], task_id: str) -> TaskStatusView:
        task = next((item for item in tasks if item.task_id == task_id), None)
        if task is not None:
            return task
        known = ", ".join(item.task_id for item in tasks[:10]) or "none"
        raise UserError(f"Unknown task {task_id!r} in run {run.id} (tasks: {known}).")

    def _log_path(self, run: RunRow, task_id: str, stream: LogStream) -> str:
        if not run.slurm_job_id:
            raise UserError(f"Run {run.id} has not been submitted; there are no logs yet.")
        spec = next((item for item in TaskRepo(self._ctx.db()).specs(run.id) if item.task_id == task_id), None)
        if spec is None:
            known = ", ".join(item.task_id for item in TaskRepo(self._ctx.db()).specs(run.id)[:10]) or "none"
            raise UserError(f"Unknown task {task_id!r} in run {run.id} (tasks: {known}).")
        suffix = "err" if stream is LogStream.STDERR else "out"
        return f"{run.remote_root}/logs/task_{run.slurm_job_id}_{spec.index}.{suffix}"

    @staticmethod
    def _tail(transport: Transport, path: str, lines: int) -> ExecResult:
        return transport.exec(f"tail -n {int(lines)} {shlex.quote(path)}", check=False, retries=1)

    @staticmethod
    def _stream(value: LogStream | str | None) -> LogStream | None:
        if value is None or isinstance(value, LogStream):
            return value
        try:
            return LogStream(value)
        except ValueError as exc:
            raise UserError("Log stream must be 'stdout' or 'stderr'.") from exc

    def _context(self, run_id: str | None) -> tuple[RunRow, list[TaskStatusView]]:
        run = RunService(self._ctx).get(run_id)
        tasks = StatusService(self._ctx).snapshot(run.id).tasks
        if not tasks:
            raise UserError(f"Run {run.id} has no tasks.")
        return run, tasks

    def _candidates(
        self,
        run: RunRow,
        tasks: Sequence[TaskStatusView],
        *,
        task_id: str | None,
        stream: LogStream | None,
    ) -> list[tuple[TaskStatusView, LogStream]]:
        failed = [task for task in tasks if task.effective_state in _FAILED_STATES]
        if task_id is not None:
            task = self._task(run, tasks, task_id)
            if stream is not None:
                return [(task, stream)]
            if task.effective_state in _FAILED_STATES:
                return [(task, LogStream.STDERR), (task, LogStream.STDOUT)]
            return [(task, LogStream.STDOUT)]
        if stream is not None:
            return [((failed or list(tasks))[0], stream)]
        if failed:
            return [*((task, LogStream.STDERR) for task in failed), (failed[0], LogStream.STDOUT)]
        return [(tasks[0], LogStream.STDOUT)]

    def fetch(
        self,
        transport: Transport,
        run_id: str | None = None,
        *,
        task_id: str | None = None,
        stream: LogStream | str | None = None,
        lines: int = 200,
    ) -> RunLog:
        if lines < 1:
            raise UserError("Log line count must be at least 1.")
        run, tasks = self._context(run_id)
        selected_stream = self._stream(stream)
        candidates = self._candidates(run, tasks, task_id=task_id, stream=selected_stream)
        explicit_stream = selected_stream is not None
        last: tuple[TaskStatusView, LogStream, str, ExecResult] | None = None
        for index, (task, candidate_stream) in enumerate(candidates):
            path = self._log_path(run, task.task_id, candidate_stream)
            result = self._tail(transport, path, lines)
            last = (task, candidate_stream, path, result)
            final_candidate = index == len(candidates) - 1
            if result.returncode == 0 and (result.stdout.strip() or explicit_stream or final_candidate):
                return RunLog(
                    run_id=run.id,
                    task_id=task.task_id,
                    stream=candidate_stream,
                    path=path,
                    text=result.stdout,
                )
        assert last is not None
        task, candidate_stream, path, result = last
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "remote tail failed"
        raise UserError(
            f"{candidate_stream.value} log for task {task.task_id!r} in run {run.id} "
            f"is unavailable at {path}: {detail}",
            hint="Check `slurmdeck run status` and retry after the task has started.",
        )

    def follow(
        self,
        transport: Transport,
        run_id: str | None = None,
        *,
        task_id: str | None = None,
        stream: LogStream | str | None = None,
        lines: int = 50,
        on_line: Callable[[str], None],
    ) -> RunLog:
        run, tasks = self._context(run_id)
        selected_stream = self._stream(stream)
        if task_id is not None and selected_stream is not None:
            task = self._task(run, tasks, task_id)
            selection = RunLog(
                run_id=run.id,
                task_id=task.task_id,
                stream=selected_stream,
                path=self._log_path(run, task.task_id, selected_stream),
                text="",
            )
        else:
            selection = self.fetch(
                transport,
                run.id,
                task_id=task_id,
                stream=selected_stream,
                lines=1,
            )
        handle = transport.stream(
            f"tail -n {int(lines)} -F {shlex.quote(selection.path)}",
            on_line=on_line,
        )
        return RunLog(
            run_id=selection.run_id,
            task_id=selection.task_id,
            stream=selection.stream,
            path=selection.path,
            text="",
            handle=handle,
        )
