"""`slurmdeck env ...` commands."""

from __future__ import annotations

import time

import typer

from slurmdeck.cli._deps import get_context
from slurmdeck.cli._output import (
    activity,
    confirm_or_exit,
    context_line,
    data_table,
    emit_json,
    kv_panel,
    set_json_output,
    success,
    warn,
)
from slurmdeck.errors import UserError
from slurmdeck.models.cluster import BuildExecutor
from slurmdeck.models.env import EnvironmentPlanAction, EnvironmentStatus
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_execution import EnvironmentPreparationService
from slurmdeck.services.env_lifecycle import EnvironmentLifecycleService
from slurmdeck.services.env_planning import EnvironmentPlanner, EnvironmentPlanningService

env_app = typer.Typer(no_args_is_help=True, help="Prepare and manage remote environments.")


def _desired_env_id(remote_name: str | None) -> str | None:
    ctx = get_context()
    project = ctx.project
    if project is None or project.config.env is None:
        return None
    remote = ctx.resolve_remote(remote_name)
    try:
        return (
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
    except UserError:
        return None


@env_app.command("plan")
def plan(
    cli_context: typer.Context,
    executor: BuildExecutor | None = typer.Option(None, "--executor", help="Requested managed build executor."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Preview a new immutable generation."),
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Read-only preview of environment identity, policy, resources, and action."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    project = ctx.require_project()
    if project.config.env is None:
        raise UserError(
            "This project has no environment configured.",
            hint="Add an `env:` section to .slurmdeck/project.yaml (type: conda or existing).",
        )
    remote = ctx.resolve_remote(remote_name)
    service = EnvironmentPlanningService()
    if json_output:
        resolved = service.plan(
            transport=ctx.transport(remote),
            remote=remote,
            layout=ctx.layout(remote),
            project=project.config,
            project_dir=project.paths.root,
            requested_executor=executor,
            rebuild=rebuild,
        )
        emit_json(resolved)
        return
    with activity("Planning environment"):
        resolved = service.plan(
            transport=ctx.transport(remote),
            remote=remote,
            layout=ctx.layout(remote),
            project=project.config,
            project_dir=project.paths.root,
            requested_executor=executor,
            rebuild=rebuild,
        )
    kv_panel(
        "Environment plan",
        [
            ("Action", resolved.action.value),
            ("Environment", resolved.env_id),
            ("Backend", resolved.backend.value),
            ("Executor", resolved.executor.value if resolved.executor else "n/a"),
            ("Prefix", resolved.prefix),
            ("Channels", ", ".join(resolved.channels) or "n/a"),
            ("Resources", resolved.resolved_resources.model_dump_json() if resolved.resolved_resources else "n/a"),
            ("Afterok", "eligible" if resolved.afterok_eligible else "not eligible"),
            ("Ready to prepare", "yes" if resolved.complete else "no"),
        ],
    )
    for message in [*resolved.missing, *resolved.conflicts, *resolved.warnings]:
        warn(message)


@env_app.command("prepare")
def prepare(
    cli_context: typer.Context,
    rebuild: bool = typer.Option(False, "--rebuild", help="Build a new immutable generation."),
    executor: BuildExecutor | None = typer.Option(None, "--executor", help="Requested managed build executor."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Start or attach and return immediately."),
    follow: bool = typer.Option(False, "--follow", help="Follow build logs until the attempt becomes terminal."),
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Prepare the project's managed environment or verify its external prefix."""
    set_json_output(json_output, cli_context)
    if json_output and follow:
        raise UserError("--json cannot be combined with --follow.")
    if no_wait and follow:
        raise UserError("--no-wait and --follow cannot be used together.")
    ctx = get_context()
    project = ctx.require_project()
    if project.config.env is None:
        raise UserError(
            "This project has no environment configured.",
            hint="Add an `env:` section to .slurmdeck/project.yaml (type: conda or existing).",
        )
    remote = ctx.resolve_remote(remote_name)
    transport = ctx.transport(remote)
    layout = ctx.layout(remote)
    cache = EnvironmentCache(ctx.user_paths)
    with activity("Preparing environment"):
        result = EnvironmentPreparationService(cache=cache).prepare(
            transport=transport,
            remote=remote,
            layout=layout,
            project=project.config,
            project_dir=project.paths.root,
            requested_executor=executor,
            rebuild=rebuild,
            wait=not no_wait and not follow,
        )
    record = result.record
    if follow and record.status not in {EnvironmentStatus.READY, EnvironmentStatus.FAILED, EnvironmentStatus.CANCELLED}:
        lifecycle = EnvironmentLifecycleService()
        followed = lifecycle.follow_logs(transport, layout, record.env_id, on_line=typer.echo)
        try:
            while record.status not in {
                EnvironmentStatus.READY,
                EnvironmentStatus.FAILED,
                EnvironmentStatus.CANCELLED,
            }:
                time.sleep(2)
                record = lifecycle.status(transport, layout, record.env_id).record
                cache.remember_record(remote, record)
        finally:
            assert followed.handle is not None
            followed.handle.close()
            followed.handle.wait()
    if record.status in {EnvironmentStatus.FAILED, EnvironmentStatus.CANCELLED}:
        summary = record.last_error.summary if record.last_error else record.status.value
        raise UserError(f"Environment {record.env_id} ended in {record.status.value}: {summary}")
    if json_output:
        emit_json(result)
        return
    if record.status is EnvironmentStatus.READY:
        success(f"Environment {record.env_id} is READY at {record.active_prefix}.")
    else:
        attempt = record.attempts[-1] if record.attempts else None
        job = f" job {attempt.job_id}" if attempt and attempt.job_id else ""
        action = result.action.value if isinstance(result.action, EnvironmentPlanAction) else str(result.action)
        success(f"Environment {record.env_id}: {action}, {record.status.value}{job}.")


@env_app.command("list")
def list_(
    cli_context: typer.Context,
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List registry-backed environments and dynamic run references."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    with activity("Listing environments"):
        views = EnvironmentLifecycleService().list(
            ctx.transport(remote),
            ctx.layout(remote),
            desired_env_id=_desired_env_id(remote_name),
        )
    EnvironmentCache(ctx.user_paths).remember_registry(remote, [view.record for view in views])
    if json_output:
        emit_json(views)
        return
    data_table(
        "Environments",
        ["", "ENV", "BACKEND", "STATUS", "JOB / REASON", "REFS", "UPDATED"],
        [
            [
                "*" if view.desired_by_project else "",
                view.record.env_id,
                view.record.backend.value,
                view.record.status.value,
                view.job_reason,
                str(view.reference_count),
                view.record.updated_at,
            ]
            for view in views
        ],
    )


@env_app.command("show")
def show(
    cli_context: typer.Context,
    env_id: str,
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show environment identity, provenance, attempts, generations, and references."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    view = EnvironmentLifecycleService().show(
        ctx.transport(remote),
        ctx.layout(remote),
        env_id,
        desired_env_id=_desired_env_id(remote_name),
    )
    EnvironmentCache(ctx.user_paths).remember_record(remote, view.record)
    if json_output:
        emit_json(view)
        return
    record = view.record
    kv_panel(
        "Environment",
        [
            ("ID", record.env_id),
            ("Status", record.status.value),
            ("Backend / owner", f"{record.backend.value} / {record.ownership.value}"),
            ("Full hash", record.full_hash),
            ("Generation", record.active_generation or "n/a"),
            ("Prefix", record.active_prefix or "n/a"),
            ("Current attempt", record.current_attempt or "n/a"),
            ("Build job", view.build_job_id or "n/a"),
            ("Scheduler", view.scheduler_state or "n/a"),
            ("Build reason", view.display_reason or "none"),
            (
                "Build resources",
                view.resolved_resources.model_dump_json(exclude_none=True)
                if view.resolved_resources is not None
                else "n/a",
            ),
            ("Channels", ", ".join(record.provenance.channels) or "none"),
            ("Solver", record.provenance.solver or "n/a"),
            ("Platform", record.provenance.platform or "n/a"),
            ("stdout", view.stdout_path or "n/a"),
            ("stderr", view.stderr_path or "n/a"),
            ("References", ", ".join(view.references) or "none"),
            ("Last error", record.last_error.summary if record.last_error else "none"),
        ],
    )


@env_app.command("status")
def status(
    cli_context: typer.Context,
    env_id: str,
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Refresh and show one environment's effective lifecycle status."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    view = EnvironmentLifecycleService().status(ctx.transport(remote), ctx.layout(remote), env_id)
    EnvironmentCache(ctx.user_paths).remember_record(remote, view.record)
    if json_output:
        emit_json(view)
        return
    kv_panel(
        "Environment status",
        [
            ("Environment", view.record.env_id),
            ("Status", view.record.status.value),
            ("Job / reason", view.job_reason),
            ("References", view.reference_count),
        ],
    )


@env_app.command("logs")
def logs(
    cli_context: typer.Context,
    env_id: str,
    lines: int = typer.Option(100, "--lines", "-n", min=1),
    follow: bool = typer.Option(False, "--follow", help="Follow the selected stream."),
    stderr: bool = typer.Option(False, "--stderr", help="Select stderr."),
    stdout: bool = typer.Option(False, "--stdout", help="Select stdout."),
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Read or follow logs from the current/latest environment attempt."""
    set_json_output(json_output, cli_context)
    if json_output and follow:
        raise UserError("--json cannot be combined with --follow.")
    if stderr and stdout:
        raise UserError("Choose only one of --stderr or --stdout.")
    selected = "stderr" if stderr else "stdout" if stdout else None
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    service = EnvironmentLifecycleService()
    if follow:
        followed = service.follow_logs(
            ctx.transport(remote),
            ctx.layout(remote),
            env_id,
            stream=selected,
            on_line=typer.echo,
        )
        context_line(
            f"Environment {followed.env_id}",
            [("attempt", followed.attempt_id), ("stream", followed.stream), ("path", followed.path)],
        )
        assert followed.handle is not None
        raise typer.Exit(followed.handle.wait())
    log = service.logs(ctx.transport(remote), ctx.layout(remote), env_id, lines=lines, stream=selected)
    if json_output:
        emit_json(log)
        return
    context_line(
        f"Environment {log.env_id}",
        [("attempt", log.attempt_id), ("stream", log.stream), ("path", log.path)],
    )
    typer.echo(log.text, nl=not log.text.endswith("\n"))


@env_app.command("cancel")
def cancel(
    cli_context: typer.Context,
    env_id: str,
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Cancel only the active environment build attempt."""
    set_json_output(json_output, cli_context)
    confirm_or_exit(f"Cancel active build for environment {env_id}", yes=yes)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    record = EnvironmentLifecycleService().cancel(ctx.transport(remote), ctx.layout(remote), env_id)
    EnvironmentCache(ctx.user_paths).remember_record(remote, record)
    if json_output:
        emit_json(record)
        return
    success(f"Environment {record.env_id} is {record.status.value}.")


@env_app.command("remove")
def remove(
    cli_context: typer.Context,
    env_id: str,
    force: bool = typer.Option(False, "--force", help="Remove even when remote run manifests reference it."),
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Unregister an external env or move managed generations to trash."""
    set_json_output(json_output, cli_context)
    action = f"Force remove environment {env_id}" if force else f"Remove environment {env_id}"
    confirm_or_exit(action, yes=yes)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    result = EnvironmentLifecycleService().remove(
        ctx.transport(remote),
        ctx.layout(remote),
        env_id,
        force=force,
    )
    EnvironmentCache(ctx.user_paths).remember_record(remote, result.record)
    if json_output:
        emit_json(result)
        return
    if result.external_unregistered:
        success(f"Unregistered external environment {env_id}; its prefix was not deleted.")
    else:
        success(f"Environment {env_id} moved to trash; deletion continues in the background.")


@env_app.command("gc")
def gc(
    cli_context: typer.Context,
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete candidates instead of a dry run."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Preview or delete safe environment garbage; dry-run by default."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    report = EnvironmentLifecycleService().gc(ctx.transport(remote), ctx.layout(remote), delete=yes)
    if json_output:
        emit_json(report)
        return
    data_table(
        "Environment GC",
        ["KIND", "ENV", "BYTES", "PATH", "REASON"],
        [
            [candidate.kind, candidate.env_id, str(candidate.size_bytes), candidate.path, candidate.reason]
            for candidate in report.candidates
        ],
    )
    if report.dry_run:
        warn("Dry run only; pass --yes to delete these candidates.")
    else:
        success(f"Deleted {len(report.deleted)} candidates; {len(report.failed)} failed.")
