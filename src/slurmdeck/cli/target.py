"""`slurmdeck target ...` commands."""

from __future__ import annotations

import typer

from slurmdeck.cli._deps import get_context
from slurmdeck.cli._output import data_table, emit_json, kv_panel, set_json_output, success
from slurmdeck.services.targets import TargetService, TargetView

target_app = typer.Typer(no_args_is_help=True, help="Inspect and select project execution targets.")


def _environment_label(view: TargetView) -> str:
    if view.env is None:
        return "not configured"
    return f"{view.env.type}:{view.env.name}"


@target_app.command("list")
def list_(
    cli_context: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List the current project's configured execution targets."""
    set_json_output(json_output, cli_context)
    targets = TargetService(get_context()).list_targets()
    if json_output:
        emit_json(targets)
        return
    data_table(
        "Project targets",
        ["", "NAME", "REMOTE", "DEFAULT", "ENVIRONMENT"],
        [
            [
                "*" if target.current else "",
                target.name,
                target.remote,
                "yes" if target.default else "",
                _environment_label(target),
            ]
            for target in targets
        ],
    )


@target_app.command("show")
def show(
    cli_context: typer.Context,
    name: str | None = typer.Argument(None, help="Target name (default: current project target)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show a project's target, including its resources and environment."""
    set_json_output(json_output, cli_context)
    target = TargetService(get_context()).show(name)
    if json_output:
        emit_json(target)
        return
    kv_panel(
        "Project target",
        [
            ("Name", target.name),
            ("Remote", target.remote),
            ("Current", "yes" if target.current else "no"),
            ("Default", "yes" if target.default else "no"),
            ("Environment", target.env.model_dump_json() if target.env is not None else "not configured"),
            ("Resources", target.resources.model_dump_json()),
        ],
    )


@target_app.command("use")
def use(
    cli_context: typer.Context,
    name: str,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Select the current target for this project."""
    set_json_output(json_output, cli_context)
    target = TargetService(get_context()).use(name)
    if json_output:
        emit_json(target)
        return
    success(f"Using target {target.name} (remote {target.remote}) for this project.")
