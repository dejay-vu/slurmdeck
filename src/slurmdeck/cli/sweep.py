"""`slurmdeck sweep ...` commands: validate and preview sweep files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from slurmdeck.cli._output import data_table, emit_json, set_json_output, success
from slurmdeck.models.sweep import Sweep
from slurmdeck.planning.placeholders import expand_text, expand_value
from slurmdeck.planning.sweep import expand_sweep, render_args_from_config
from slurmdeck.storage.yamlio import load_yaml_model

sweep_app = typer.Typer(no_args_is_help=True, help="Validate and preview sweep files.")

_SCHEMA_HINT = "See `docs/sweeps.md` for the sweep schema (version: 1 with parameters/tasks)."


def _load(path: Path) -> Sweep:
    return load_yaml_model(path, Sweep, hint=_SCHEMA_HINT)


def preview_tasks(sweep: Sweep, *, run_id: str = "<run>") -> list[dict[str, Any]]:
    """Resolve every task the way `submit` would, with symbolic paths."""
    rows = []
    for index, draft in enumerate(expand_sweep(sweep)):
        task_id = f"{index:03d}"
        context: dict[str, Any] = {
            **draft.params,
            "index": index,
            "task_id": task_id,
            "task_name": draft.name,
            "run": run_id,
        }
        config = None
        if draft.config is not None:
            config = expand_value(draft.config, context, position=f"task {task_id} config")
            context["config"] = f"{run_id}/configs/{task_id}.yaml"
        context["output"] = f"{run_id}/results/{task_id}"
        if draft.args_template is not None:
            args = [
                expand_text(arg, context, position=f"task {task_id} args[{i}]")
                for i, arg in enumerate(draft.args_template)
            ]
        elif config is not None:
            args = render_args_from_config(config, draft.arg_style)
        else:
            args = []
        env = {
            key: expand_text(value, context, position=f"task {task_id} env[{key}]")
            for key, value in draft.env_template.items()
        }
        rows.append(
            {"task_id": task_id, "name": draft.name, "params": draft.params, "args": args, "env": env, "config": config}
        )
    return rows


@sweep_app.command("validate")
def validate(path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True)) -> None:
    """Check a sweep file: schema, placeholders, and expansion."""
    sweep = _load(path)
    tasks = preview_tasks(sweep)
    success(f"{path}: valid, expands to {len(tasks)} task(s).")


@sweep_app.command("preview")
def preview(
    cli_context: typer.Context,
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    limit: int = typer.Option(10, "--limit", help="Show at most this many tasks."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show the resolved tasks a sweep file produces."""
    set_json_output(json_output, cli_context)
    sweep = _load(path)
    tasks = preview_tasks(sweep)
    if json_output:
        emit_json({"total": len(tasks), "tasks": tasks[:limit]})
        return
    data_table(
        f"Sweep preview: {path} ({len(tasks)} tasks, showing {min(limit, len(tasks))})",
        ["TASK", "NAME", "ARGS", "ENV"],
        [
            [
                row["task_id"],
                str(row["name"]),
                " ".join(row["args"]),
                " ".join(f"{k}={v}" for k, v in row["env"].items()),
            ]
            for row in tasks[:limit]
        ],
    )
