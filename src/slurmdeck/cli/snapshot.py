"""`slurmdeck snapshot ...` commands."""

from __future__ import annotations

import typer

from slurmdeck.cli._deps import get_context
from slurmdeck.cli._output import activity, data_table, emit_json, set_json_output, success
from slurmdeck.services.snapshots import SnapshotService

snapshot_app = typer.Typer(no_args_is_help=True, help="Preview local and manage remote code snapshots.")


@snapshot_app.command("preview")
def preview(
    cli_context: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show the exact local files that the next snapshot would upload."""
    set_json_output(json_output, cli_context)
    project = get_context().require_project()
    result = SnapshotService().preview(project.paths.root, project.config.sync)
    if json_output:
        emit_json(result)
        return
    data_table("Local snapshot preview", ["FILE"], [[relative_path] for relative_path in result.files])
    success(f"{result.file_count} file(s), {result.size_bytes} bytes, hash {result.hash}.")


@snapshot_app.command("list")
def list_(
    cli_context: typer.Context,
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List valid snapshots and their dynamically derived references."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    with activity("Listing snapshots"):
        snapshots = SnapshotService().list_snapshots(ctx.transport(remote), ctx.layout(remote))
    if json_output:
        emit_json(snapshots)
        return
    data_table(
        "Snapshots",
        ["HASH", "SIZE", "REFERENCES", "CREATED"],
        [
            [
                snapshot.hash,
                str(snapshot.size_bytes),
                str(snapshot.reference_count),
                snapshot.created_at,
            ]
            for snapshot in snapshots
        ],
    )


@snapshot_app.command("gc")
def gc(
    cli_context: typer.Context,
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete eligible snapshots; default is a dry run."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Find unreferenced snapshots at least 24 hours old; delete only with --yes."""
    set_json_output(json_output, cli_context)
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    with activity("Scanning snapshots") as report:
        result = SnapshotService().gc(
            ctx.transport(remote),
            ctx.layout(remote),
            confirmed=yes,
            operation_sink=report,
        )
    if json_output:
        emit_json(result)
        return
    if result.dry_run:
        data_table("Snapshot GC dry run", ["ELIGIBLE HASH"], [[digest] for digest in result.candidates])
        success(f"Dry run: {len(result.candidates)} snapshot(s) eligible; rerun with --yes to delete.")
    else:
        success(f"Deleted {len(result.deleted)} snapshot(s); {len(result.failed)} failed.")
