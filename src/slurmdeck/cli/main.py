"""slurmdeck CLI entry point and error funnel."""

from __future__ import annotations

import typer

from slurmdeck import __version__
from slurmdeck.cli._deps import get_context
from slurmdeck.cli._output import (
    configure_output_theme,
    emit_error_json,
    error,
    json_output_requested,
    set_json_output,
)
from slurmdeck.cli.env import env_app
from slurmdeck.cli.project import doctor_command, init_command, ui_command
from slurmdeck.cli.remote import remote_app
from slurmdeck.cli.run import run_app
from slurmdeck.cli.snapshot import snapshot_app
from slurmdeck.cli.submit import SUBMIT_HELP, submit_command
from slurmdeck.cli.sweep import sweep_app
from slurmdeck.errors import UserError
from slurmdeck.transport.errors import TransportError

app = typer.Typer(
    no_args_is_help=True,
    help="SlurmDeck: run, monitor, and retrieve Slurm workloads on remote clusters.",
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(remote_app, name="remote")
app.add_typer(env_app, name="env")
app.add_typer(run_app, name="run")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(sweep_app, name="sweep")
app.command("init")(init_command)
app.command("doctor")(doctor_command)
app.command("ui")(ui_command)
app.command("submit", help=SUBMIT_HELP)(submit_command)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"slurmdeck {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    context: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """SlurmDeck: run, monitor, and retrieve Slurm workloads on remote clusters."""
    set_json_output(False, context)
    try:
        persisted_theme = get_context().user_store.ui_theme()
    except UserError:
        persisted_theme = None
    configure_output_theme(persisted=persisted_theme)


def main() -> None:
    try:
        app()
    except UserError as exc:
        if json_output_requested():
            emit_error_json(exc.error)
        else:
            error(str(exc))
        raise SystemExit(exc.exit_code) from None
    except TransportError as exc:
        if json_output_requested():
            emit_error_json(exc.error)
        else:
            error(str(exc))
        raise SystemExit(exc.exit_code) from None


if __name__ == "__main__":
    main()
