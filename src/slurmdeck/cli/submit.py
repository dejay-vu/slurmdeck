"""`slurmdeck submit` — plan and submit a run in one step."""

from __future__ import annotations

from pathlib import Path

import typer

from slurmdeck.cli._deps import get_context
from slurmdeck.cli._output import activity, emit_json, kv_panel, set_json_output, success
from slurmdeck.errors import UserError
from slurmdeck.models.env import EnvWaitPolicy
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.services.env_binding import EnvironmentRunBindingService
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.runs import RunService
from slurmdeck.storage.yamlio import load_yaml_model

SUBMIT_HELP = """Submit a command (optionally a sweep) as a Slurm array run.

Your command goes at the end. Put `--` before it whenever it has options of
its own — anything after `--` is passed to your program untouched, while an
unknown option before it is an error (typos never reach your command):

  slurmdeck submit -- python train.py --config {config}
  slurmdeck submit --sweep sweep.yaml -- python train.py {args}
  slurmdeck submit --time 04:00:00 --gres gpu:1 python train.py
  slurmdeck submit --shell 'python prep.py && python train.py --config {config}'

Placeholders resolved per task: {config} {output} {task_id} {task_name} {run}
{index} {args} plus your sweep parameter names.
"""


def submit_command(
    cli_context: typer.Context,
    command: list[str] | None = typer.Argument(
        None, help="Your command (use `--` before it when it has options of its own)."
    ),
    name: str | None = typer.Option(None, "--name", "-n", help="Run name (default: derived)."),
    sweep: Path | None = typer.Option(
        None, "--sweep", "-s", exists=True, dir_okay=False, readable=True, help="Sweep YAML file."
    ),
    shell: str | None = typer.Option(
        None, "--shell", help="Run this string through bash instead of a direct argv command."
    ),
    remote_name: str | None = typer.Option(None, "--remote", "-r", help="Remote to use (default: current)."),
    time: str | None = typer.Option(None, "--time", help="Slurm time limit override."),
    cpus: int | None = typer.Option(None, "--cpus", help="CPUs per task override."),
    mem: str | None = typer.Option(None, "--mem", help="Memory override."),
    gres: str | None = typer.Option(None, "--gres", help="Generic resources, e.g. gpu:1."),
    partition: str | None = typer.Option(None, "--partition", help="Partition override."),
    account: str | None = typer.Option(None, "--account", help="Account override."),
    qos: str | None = typer.Option(None, "--qos", help="QOS override."),
    constraint: str | None = typer.Option(None, "--constraint", help="Constraint override."),
    max_parallel: int | None = typer.Option(None, "--max-parallel", help="Max concurrent array tasks."),
    plan_only: bool = typer.Option(False, "--plan-only", help="Plan locally without submitting."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    env_wait: EnvWaitPolicy = typer.Option(
        EnvWaitPolicy.READY,
        "--env-wait",
        help="Environment policy: require READY or submit with a safe Slurm afterok dependency.",
    ),
) -> None:
    """Submit a command (optionally a sweep) as a Slurm array run."""
    set_json_output(json_output, cli_context)
    argv = list(command or [])
    if shell is not None and argv:
        raise UserError("Use either --shell '<string>' or a `--`-separated command, not both.")
    if shell is None and not argv:
        raise UserError(
            "No command given.",
            hint="Put your command after `--`, e.g. `slurmdeck submit -- python train.py`.",
        )
    command_spec = CommandTemplateSpec(argv=argv or None, shell=shell)
    sweep_model: Sweep | None = None
    if sweep is not None:
        sweep_model = load_yaml_model(sweep, Sweep, hint="Run `slurmdeck sweep validate` for details.")
    overrides = ResourceOverrides(
        time=time,
        cpus=cpus,
        mem=mem,
        gres=gres,
        partition=partition,
        account=account,
        qos=qos,
        constraint=constraint,
        max_parallel=max_parallel,
    )

    ctx = get_context()
    remote = ctx.resolve_remote(remote_name)
    transport = ctx.transport(remote)
    project = ctx.require_project()
    env_binding = EnvironmentRunBindingService(cache=EnvironmentCache(ctx.user_paths)).resolve(
        transport=transport,
        remote=remote,
        layout=ctx.layout(remote),
        project=project.config,
        project_dir=project.paths.root,
        wait_policy=env_wait,
    )
    runs = RunService(ctx)
    with activity("Planning run") as report:
        row = runs.plan(
            command=command_spec,
            sweep=sweep_model,
            sweep_file=str(sweep) if sweep else None,
            name=name,
            overrides=overrides,
            remote=remote,
            env_binding=env_binding,
            operation_sink=report,
        )
        if not plan_only:
            row = runs.submit(transport, row.id, operation_sink=report)

    from slurmdeck.services.status import StatusService

    if json_output:
        emit_json(row)
        return

    kv_panel(
        "Run planned" if plan_only else "Run submitted",
        [
            ("Run", row.id),
            ("Command", shell if shell is not None else " ".join(argv)),
            ("State", row.state),
            ("Tasks", StatusService(ctx).summary(row.id).total),
            ("Slurm job", row.slurm_job_id or "-"),
            ("Remote", f"{row.remote}:{row.remote_root}"),
        ],
    )
    if plan_only:
        success(f"Planned. Submit later with `slurmdeck run submit {row.id}`.")
    else:
        success("Submitted. Check progress with `slurmdeck run status` or `slurmdeck ui`.")
