"""`slurmdeck init`, `doctor`, and `ui` commands."""

from __future__ import annotations

import uuid
from pathlib import Path

import typer

from slurmdeck.cli._deps import get_context, reset
from slurmdeck.cli._output import activity, data_table, emit_json, set_json_output, styled_state, success
from slurmdeck.errors import UserError
from slurmdeck.models.project import ProjectConfig
from slurmdeck.services.doctor import DoctorService
from slurmdeck.storage.db import connect
from slurmdeck.storage.paths import ProjectPaths
from slurmdeck.storage.yamlio import dump_yaml_model


def init_command(
    remote: str | None = typer.Option(None, "--remote", help="Pin this project to a specific remote."),
) -> None:
    """Initialize a slurmdeck project in the current directory."""
    paths = ProjectPaths(Path.cwd())
    if paths.config_path.exists():
        raise UserError(
            f"Project already initialized: {paths.config_path}",
            hint="Edit .slurmdeck/project.yaml to change project settings.",
        )
    config = ProjectConfig(project_id=str(uuid.uuid4()), display_name=paths.root.name, remote=remote)
    dump_yaml_model(paths.config_path, config)
    connect(paths.db_path).close()
    reset()
    success(f"Initialized slurmdeck project at {paths.state_dir}.")
    typer.echo("Next: edit .slurmdeck/project.yaml (resources/env), then `slurmdeck submit -- <command>`.")


def doctor_command(
    cli_context: typer.Context,
    remote: str | None = typer.Option(None, "--remote", help="Check a specific remote."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Check local tools, the remote, Slurm availability, and project state."""
    set_json_output(json_output, cli_context)
    with activity("Running doctor") as report:
        checks = DoctorService(get_context()).run(remote_name=remote, operation_sink=report)
    if json_output:
        emit_json(checks)
    else:
        data_table(
            "Doctor",
            ["CHECK", "STATE", "DETAIL", "FIX"],
            [[check.name, styled_state(check.state), check.detail, check.fix] for check in checks],
        )
    if any(check.state == "FAILED" for check in checks):
        raise typer.Exit(1)


def ui_command(
    theme: str | None = typer.Option(None, "--theme", help="Temporary UI theme override; use Theme to list choices."),
) -> None:
    """Launch the interactive TUI."""
    from slurmdeck.tui.app import SlurmDeckApp

    SlurmDeckApp(get_context(), theme_name=theme).run()
