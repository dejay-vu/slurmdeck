"""`slurmdeck remote ...` commands."""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.text import Text

from slurmdeck.cli._deps import get_context
from slurmdeck.cli._output import activity, confirm_or_exit, data_table, emit_json, kv_panel, set_json_output, success
from slurmdeck.errors import UserError
from slurmdeck.models.remote import HostKeyPolicy
from slurmdeck.planning.commandline import shell_join
from slurmdeck.services.remotes import RemoteService

remote_app = typer.Typer(no_args_is_help=True, help="Manage cluster remotes and SSH connections.")
profile_app = typer.Typer(no_args_is_help=True, help="Inspect or explicitly replace cluster capability policy.")
remote_app.add_typer(profile_app, name="profile")


@profile_app.command("show")
def profile_show(
    cli_context: typer.Context,
    name: str | None = typer.Argument(None, help="Remote name (default: selected remote)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show the configured profile without probing or changing anything."""
    set_json_output(json_output, cli_context)
    profile = RemoteService(get_context()).show_profile(name)
    if profile is None:
        raise UserError(
            "This remote has no cluster profile.",
            hint="Create a YAML profile and save it with `slurmdeck remote profile set --file PROFILE.yaml`.",
        )
    if json_output:
        emit_json(profile)
    else:
        typer.echo(yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False), nl=False)


@profile_app.command("set")
def profile_set(
    cli_context: typer.Context,
    name: str | None = typer.Argument(None, help="Remote name (default: selected remote)."),
    profile_file: Path = typer.Option(..., "--file", exists=True, dir_okay=False, readable=True),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Validate a profile file, then atomically replace the remote's cluster block."""
    set_json_output(json_output, cli_context)
    remote = RemoteService(get_context()).set_profile(name, profile_file)
    if json_output:
        emit_json(remote)
        return
    success(f"Saved cluster profile for remote {remote.name}.")


@remote_app.command("add")
def add(
    cli_context: typer.Context,
    name: str,
    host: str | None = typer.Option(None, "--host", help="SSH destination, e.g. user@login.example.com."),
    ssh_alias: str | None = typer.Option(None, "--ssh-alias", help="Host alias from your ~/.ssh/config."),
    base: str = typer.Option(..., "--base", help="Remote base directory for slurmdeck state (may use $VARS)."),
    host_key_policy: HostKeyPolicy = typer.Option(
        HostKeyPolicy.INHERIT,
        "--host-key-policy",
        help="Host-key policy: inherit OpenSSH config, require a known key, or explicitly accept a new key.",
    ),
    use: bool = typer.Option(False, "--use", help="Select this remote as the current one."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Register a cluster (authentication stays in your own SSH setup)."""
    set_json_output(json_output, cli_context)
    remote = RemoteService(get_context()).add(
        name,
        host=host,
        ssh_alias=ssh_alias,
        base=base,
        host_key_policy=host_key_policy,
        use=use,
    )
    if json_output:
        emit_json(remote)
        return
    success(f"Added remote {remote.name} ({remote.destination}).")


@remote_app.command("list")
def list_(
    cli_context: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List configured remotes."""
    set_json_output(json_output, cli_context)
    infos = RemoteService(get_context()).list_remotes()
    if json_output:
        emit_json(infos)
        return
    data_table(
        "Remotes",
        ["", "NAME", "DESTINATION", "BASE"],
        [["*" if info.current else "", info.name, info.destination, info.resolved_base or info.base] for info in infos],
    )


@remote_app.command("use")
def use(
    cli_context: typer.Context,
    name: str,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Select the current remote."""
    set_json_output(json_output, cli_context)
    remote = RemoteService(get_context()).use(name)
    if json_output:
        emit_json(remote)
        return
    success(f"Using remote {remote.name} ({remote.destination}).")


@remote_app.command("remove")
def remove(
    cli_context: typer.Context,
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Remove a remote definition (never touches the cluster)."""
    set_json_output(json_output, cli_context)
    confirm_or_exit(f"Remove remote {name!r}", yes=yes)
    RemoteService(get_context()).remove(name)
    if json_output:
        emit_json({"remote": name, "removed": True})
        return
    success(f"Removed remote {name}.")


@remote_app.command("connect")
def connect(
    cli_context: typer.Context,
    name: str | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Open the SSH connection and prepare the remote base directory."""
    set_json_output(json_output, cli_context)
    with activity("Connecting"):
        report = RemoteService(get_context()).connect(name)
    if json_output:
        emit_json(report)
        return
    kv_panel(
        "Connected",
        [("Remote", report.remote), ("Destination", report.destination), ("Base", report.resolved_base)],
    )


@remote_app.command("disconnect")
def disconnect(
    cli_context: typer.Context,
    name: str | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Close the multiplexed SSH connection."""
    set_json_output(json_output, cli_context)
    closed = RemoteService(get_context()).disconnect(name)
    if json_output:
        emit_json({"remote": closed, "connected": False})
        return
    success(f"Disconnected {closed}.")


@remote_app.command("status")
def status(
    cli_context: typer.Context,
    name: str | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show whether the SSH connection is alive."""
    set_json_output(json_output, cli_context)
    view = RemoteService(get_context()).status(name)
    if json_output:
        emit_json(view)
    else:
        state = (
            Text("connected", style="state.success") if view.connected else Text("not connected", style="state.warning")
        )
        kv_panel("Connection", [("Remote", view.remote), ("Destination", view.destination), ("State", state)])
    raise typer.Exit(0 if view.connected else 1)


@remote_app.command("exec")
def exec_(
    command: list[str] = typer.Argument(..., help="Command to run on the remote (prefix with `--`)."),
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
) -> None:
    """Run an ad-hoc command on the remote login node."""
    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    result = ctx.transport(remote).exec(shell_join(command), check=False, timeout=600)
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, nl=False, err=True)
    raise typer.Exit(result.returncode)
