"""Pulling results and logs from the remote run directory."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from slurmdeck.errors import UserError
from slurmdeck.operations import OperationPhase, OperationReporter, OperationSink, noop_operation_sink
from slurmdeck.services.context import AppContext
from slurmdeck.services.status import StatusService
from slurmdeck.slurm import failed_states
from slurmdeck.storage.repos import RunRow
from slurmdeck.transport import Transport


@dataclass(frozen=True)
class PullReport:
    run_id: str
    destination: Path
    matched: int
    transferred: int
    skipped: int
    failed: int
    bytes: int
    failed_paths: tuple[str, ...] = ()


def pull_filters(
    *,
    task_ids: Sequence[str] | None = None,
    logs_only: bool = False,
    excludes: Sequence[str] = (),
) -> list[str]:
    """rsync filter rules for a pull. Excludes always win; then selection narrows."""
    rules = [*(f"- {pattern}" for pattern in excludes), "- .keep"]
    if logs_only:
        rules += ["+ /logs/", "+ /logs/**", "- *"]
    elif task_ids is not None:
        rules += ["+ /logs/", "+ /logs/**", "+ /results/"]
        for task_id in task_ids:
            rules += [f"+ /results/{task_id}/", f"+ /results/{task_id}/**"]
        rules += ["- /results/*", "+ /*", "+ /configs/**"]
    return rules


class ResultsService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def select_tasks(
        self,
        run_id: str,
        *,
        selection: Literal["failed", "completed"] | None = None,
        task_ids: Sequence[str] | None = None,
    ) -> tuple[str, ...] | None:
        if selection is None and task_ids is None:
            return None
        snapshot = StatusService(self._ctx).snapshot(run_id)
        known = {task.task_id for task in snapshot.tasks}
        if task_ids is not None:
            requested = tuple(dict.fromkeys(task_ids))
            unknown = [task_id for task_id in requested if task_id not in known]
            if unknown:
                raise UserError(f"Unknown task(s) in run {run_id}: {', '.join(unknown)}.")
            return requested
        wanted = (failed_states() | {"FAILED", "KILLED"}) if selection == "failed" else {"COMPLETED"}
        selected = tuple(task.task_id for task in snapshot.tasks if task.effective_state in wanted)
        if not selected:
            raise UserError(f"Run {run_id} has no {selection} tasks to pull.")
        return selected

    @staticmethod
    def _relative_failed_paths(run: RunRow, paths: Sequence[str]) -> tuple[str, ...]:
        root = run.remote_root.rstrip("/") + "/"
        return tuple(path[len(root) :] if path.startswith(root) else path for path in paths)

    def pull(
        self,
        transport: Transport,
        run: RunRow,
        *,
        into: Path,
        task_ids: Sequence[str] | None = None,
        logs_only: bool = False,
        excludes: Sequence[str] = (),
        operation_sink: OperationSink = noop_operation_sink,
    ) -> PullReport:
        reporter = OperationReporter("results.pull", operation_sink)
        reporter.started(OperationPhase.DOWNLOAD, message=f"Preparing to pull {run.id}")
        try:
            into.mkdir(parents=True, exist_ok=True)
            filters = pull_filters(task_ids=task_ids, logs_only=logs_only, excludes=excludes)
            reporter.progress(OperationPhase.DOWNLOAD, message=f"Pulling {run.id} into {into}")
            stats = transport.download(f"{run.remote_root}/", f"{into}/", filters=filters, timeout=3600)
            report = PullReport(
                run_id=run.id,
                destination=into,
                matched=stats.matched_files,
                transferred=stats.transferred_files,
                skipped=stats.skipped_files,
                failed=stats.failed_files,
                bytes=stats.bytes_transferred,
                failed_paths=self._relative_failed_paths(run, stats.failed_paths),
            )
        except BaseException as exc:
            reporter.failed(message=str(exc))
            raise
        reporter.completed(
            OperationPhase.DOWNLOAD,
            result_counts={
                "matched": report.matched,
                "transferred": report.transferred,
                "skipped": report.skipped,
                "failed": report.failed,
            },
        )
        return report
