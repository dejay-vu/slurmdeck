"""`slurmdeck run ...` commands: inspect and control runs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

import typer

from slurmdeck.cli._deps import get_context
from slurmdeck.cli._output import (
    activity,
    confirm_or_exit,
    console,
    context_line,
    data_table,
    emit_json,
    kv_panel,
    set_json_output,
    styled_state,
    success,
    warn,
)
from slurmdeck.errors import UserError
from slurmdeck.services.logs import LogService, LogStream
from slurmdeck.services.results import ResultsService
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import StatusService
from slurmdeck.slurm import failed_states

run_app = typer.Typer(no_args_is_help=True, help="Inspect and control runs (default run: the most recent).")

_FAILED = failed_states() | {"FAILED", "KILLED"}


def _refresh(run_id: str, *, force: bool = False) -> None:
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    if row.state in ("planned", "submitting", "submit_failed", "submit_unknown"):
        return
    remote = ctx.user_store.read_remote(row.remote)
    with activity("Refreshing status") as report:
        StatusService(ctx).refresh(
            ctx.transport(remote),
            ctx.layout(remote),
            [row.id],
            force=force,
            operation_sink=report,
        )


@run_app.command("list")
def list_(
    cli_context: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List runs in this project."""
    set_json_output(json_output, cli_context)
    rows = RunService(get_context()).list_views()
    if json_output:
        emit_json(rows)
        return
    data_table(
        "Runs",
        ["RUN", "STATE", "TASKS", "SLURM JOB", "CREATED"],
        [
            [row.id, styled_state(row.state), row.summary.format_counts(), row.slurm_job_id or "-", row.created_at]
            for row in rows
        ],
    )


@run_app.command("show")
def show(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show one run's manifest and summary."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    if json_output:
        emit_json(row)
        return
    command = " ".join(row.command.argv) if row.command.argv else f"--shell {row.command.shell!r}"
    kv_panel(
        f"Run {row.id}",
        [
            ("State", styled_state(row.state)),
            ("Created", row.created_at),
            ("Remote", f"{row.remote}:{row.remote_root}"),
            ("Slurm job", row.slurm_job_id or "-"),
            ("Command", command),
            ("Resources", row.resources.model_dump_json(exclude_none=True)),
            ("Sweep", row.sweep_file or "-"),
            ("Retry of", row.retry_of or "-"),
            ("Summary", row.summary.format_counts()),
        ],
    )


@run_app.command("submit")
def submit(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Submit a previously planned run."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    remote = ctx.user_store.read_remote(row.remote)
    with activity("Submitting") as report:
        row = RunService(ctx).submit(ctx.transport(remote), row.id, operation_sink=report)
    if json_output:
        emit_json(row)
        return
    success(f"Submitted {row.id} as Slurm job {row.slurm_job_id}.")


@run_app.command("reconcile")
def reconcile(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Resolve an uncertain submission without submitting another job."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    remote = ctx.user_store.read_remote(row.remote)
    with activity("Reconciling submission") as report:
        row = RunService(ctx).reconcile(ctx.transport(remote), row.id, operation_sink=report)
    if json_output:
        emit_json(row)
        return
    success(f"Reconciled {row.id} as Slurm job {row.slurm_job_id}.")


@run_app.command("status")
def status(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    cached: bool = typer.Option(False, "--cached", help="Skip the remote refresh; read local state only."),
    failed: bool = typer.Option(False, "--failed", help="Show only failed tasks."),
    watch: bool = typer.Option(False, "--watch", help="Refresh every 30s until interrupted."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show merged scheduler + task status for a run."""
    set_json_output(json_output, cli_context)
    if json_output and watch:
        raise UserError("--json cannot be combined with --watch.")
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    while True:
        if not cached:
            _refresh(row.id)
        states = _FAILED if failed else None
        snapshot = StatusService(ctx).snapshot(row.id)
        rows = snapshot.tasks if states is None else [task for task in snapshot.tasks if task.effective_state in states]
        summary = snapshot.summary
        if json_output:
            data = snapshot.model_copy(update={"tasks": rows})
            emit_json(
                data,
                meta={
                    "stale": snapshot.is_stale,
                    "refresh_warning": snapshot.refresh_error,
                },
            )
        else:
            kv_panel(
                f"Run {row.id}",
                [
                    ("Summary", summary.format_counts()),
                    ("Refreshed", snapshot.refreshed_at if snapshot.refreshed_at is not None else "never"),
                    ("Stale", "yes" if snapshot.is_stale else "no"),
                    ("Warning", snapshot.refresh_error.summary if snapshot.refresh_error is not None else "none"),
                ],
            )
            data_table(
                "Tasks",
                ["TASK", "NAME", "STATE", "SLURM", "EXIT", "REASON"],
                [
                    [
                        task.task_id,
                        task.name,
                        styled_state(task.effective_state),
                        task.scheduler_state or "-",
                        str(task.exit_code) if task.exit_code is not None else "-",
                        task.display_reason or "",
                    ]
                    for task in rows
                ],
            )
        if not watch:
            break
        try:
            time.sleep(30)
            console.rule()
        except KeyboardInterrupt:
            break


@run_app.command("logs")
def logs(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    task: str | None = typer.Option(None, "--task", "-t", help="Task id (default: service-selected)."),
    stream: LogStream | None = typer.Option(None, "--stream", help="Explicit stream: stdout or stderr."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep following the log."),
    lines: int = typer.Option(200, "--lines", help="How many trailing lines to fetch."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Fetch (or follow) a task's log."""
    set_json_output(json_output, cli_context)
    if json_output and follow:
        raise UserError("--json cannot be combined with --follow.")
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    remote = ctx.user_store.read_remote(row.remote)
    transport = ctx.transport(remote)
    service = LogService(ctx)
    if not follow:
        log = service.fetch(transport, row.id, task_id=task, stream=stream, lines=lines)
        if json_output:
            emit_json(log)
            return
        context_line(
            f"Run {log.run_id}",
            [("task", log.task_id), ("stream", log.stream.value), ("path", log.path)],
        )
        typer.echo(log.text, nl=not log.text.endswith("\n"))
        return
    log = service.follow(
        transport,
        row.id,
        task_id=task,
        stream=stream,
        lines=lines,
        on_line=typer.echo,
    )
    context_line(
        f"Run {log.run_id}",
        [("task", log.task_id), ("stream", log.stream.value), ("path", log.path)],
    )
    assert log.handle is not None
    try:
        log.handle.wait()
    except KeyboardInterrupt:
        log.handle.close()


@run_app.command("cancel")
def cancel(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Cancel a run's Slurm array."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    confirm_or_exit(f"Cancel run {row.id} (Slurm job {row.slurm_job_id})", yes=yes)
    remote = ctx.user_store.read_remote(row.remote)
    row = RunService(ctx).cancel(ctx.transport(remote), row.id)
    if json_output:
        emit_json(row)
        return
    success(f"Cancelled {row.id}.")


@run_app.command("retry")
def retry(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    tasks: str | None = typer.Option(None, "--tasks", help="Comma-separated task ids (default: failed tasks)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create and submit a new run from a run's failed (or selected) tasks."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    source = RunService(ctx).get(run_id)
    _refresh(source.id)
    task_ids = [item.strip() for item in tasks.split(",")] if tasks else None
    remote = ctx.user_store.read_remote(source.remote)
    runs = RunService(ctx)
    with activity("Planning retry") as report:
        row = runs.retry(source.id, task_ids=task_ids, operation_sink=report)
        row = runs.submit(ctx.transport(remote), row.id, operation_sink=report)
    if json_output:
        emit_json(row)
        return
    success(f"Retry {row.id} submitted (from {source.id}, Slurm job {row.slurm_job_id}).")


@run_app.command("pull")
def pull(
    cli_context: typer.Context,
    run_id: str | None = typer.Argument(None),
    into: Path = typer.Option(..., "--into", "-o", file_okay=False, help="Local destination directory."),
    failed: bool = typer.Option(False, "--failed", help="Only failed tasks' results."),
    completed: bool = typer.Option(False, "--completed", help="Only completed tasks' results."),
    tasks: str | None = typer.Option(None, "--tasks", help="Comma-separated task ids."),
    logs_only: bool = typer.Option(False, "--logs-only", help="Only the logs directory."),
    exclude: list[str] | None = typer.Option(None, "--exclude", help="rsync exclude pattern (repeatable)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Download results/logs from the remote run directory."""
    set_json_output(json_output, cli_context)
    if sum([failed, completed, tasks is not None]) > 1:
        raise UserError("Pick at most one of --failed, --completed, or --tasks.")
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    selection: Literal["failed", "completed"] | None = "failed" if failed else "completed" if completed else None
    requested = [item.strip() for item in tasks.split(",") if item.strip()] if tasks else None
    if selection is not None:
        _refresh(row.id)
    service = ResultsService(ctx)
    task_ids = service.select_tasks(row.id, selection=selection, task_ids=requested)
    remote = ctx.user_store.read_remote(row.remote)
    with activity("Pulling") as report:
        report_result = service.pull(
            ctx.transport(remote),
            row,
            into=into,
            task_ids=task_ids,
            logs_only=logs_only,
            excludes=exclude or [],
            operation_sink=report,
        )
    empty = report_result.matched == 0
    if json_output:
        meta: dict[str, object] = {"partial": report_result.failed > 0}
        if empty:
            meta["empty"] = True
        emit_json(report_result, meta=meta)
    else:
        kv_panel(
            "Pull report",
            [
                ("Run", report_result.run_id),
                ("Destination", report_result.destination),
                ("Matched", report_result.matched),
                ("Transferred", report_result.transferred),
                ("Skipped", report_result.skipped),
                ("Failed", report_result.failed),
                ("Bytes", report_result.bytes),
            ],
        )
        if report_result.failed_paths:
            warn("Retryable failed paths: " + ", ".join(report_result.failed_paths))
        if empty:
            warn("No files matched the pull selection; nothing was transferred.")
    if report_result.failed or empty:
        raise typer.Exit(4)


@run_app.command("clean")
def clean(
    cli_context: typer.Context,
    run_id: str = typer.Argument(...),
    keep_remote: bool = typer.Option(
        False,
        "--keep-remote",
        help="Keep the remote run directory (e.g. when the remote is unreachable).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Delete a run: the local record and the remote run directory."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    row = RunService(ctx).get(run_id)
    remote_too = bool(row.remote_root) and not keep_remote
    scope = "locally and on the remote" if remote_too else "locally"
    confirm_or_exit(f"Delete run {row.id} {scope}", yes=yes)
    transport = None
    if remote_too:
        remote = ctx.user_store.read_remote(row.remote)
        transport = ctx.transport(remote)
    report = RunService(ctx).clean(row.id, transport=transport)
    if json_output:
        emit_json(report, meta={"partial": report.partial})
    elif report.partial:
        kv_panel(
            "Clean report",
            [
                ("Run", report.run_id),
                ("Local removed", report.local_removed),
                ("Remote removed", report.remote_removed),
                ("Receipt removed", report.receipt_removed),
                ("Snapshot reference released", report.snapshot_reference_released),
            ],
        )
        warn("Clean incomplete: " + report.error)
    else:
        success(f"Deleted run {row.id} {scope}.")
    if report.partial:
        raise typer.Exit(4)
