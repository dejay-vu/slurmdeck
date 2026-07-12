"""Sweep expansion: schema → per-task drafts (pure; no paths, no I/O)."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from slurmdeck.errors import UserError
from slurmdeck.models.common import safe_name
from slurmdeck.models.sweep import ArgStyle, Scalar, Sweep
from slurmdeck.planning.placeholders import to_text


@dataclass(frozen=True)
class TaskDraft:
    """One task after sweep expansion, before placeholder/path resolution."""

    index: int
    name: str
    params: dict[str, Scalar] = field(default_factory=dict)
    config: dict[str, Any] | None = None
    args_template: list[str] | None = None
    env_template: dict[str, str] = field(default_factory=dict)
    arg_style: ArgStyle = "posix"


def _combo_name(params: dict[str, Scalar]) -> str:
    parts = [f"{key}-{to_text(value)}" for key, value in params.items()]
    return safe_name("__".join(parts)) if parts else "task"


def expand_sweep(sweep: Sweep) -> list[TaskDraft]:
    if sweep.tasks is not None:
        return [
            TaskDraft(
                index=index,
                name=safe_name(task.name) if task.name else f"task-{index}",
                config=task.config or None,
                args_template=task.args,
                env_template=task.env,
                arg_style=sweep.arg_style,
            )
            for index, task in enumerate(sweep.tasks)
        ]

    parameters = sweep.parameters or {}
    axis_names = list(parameters)
    combos: list[dict[str, Scalar]] = [
        dict(zip(axis_names, values, strict=True))
        for values in itertools.product(*(parameters[name] for name in axis_names))
    ]
    if not parameters:
        combos = []

    for extra in sweep.include:
        combos.append(dict(extra))

    def excluded(combo: dict[str, Scalar]) -> bool:
        return any(all(combo.get(key) == value for key, value in rule.items()) for rule in sweep.exclude)

    combos = [combo for combo in combos if not excluded(combo)]
    if not combos:
        raise UserError("Sweep produced no tasks (all combinations excluded, or empty parameters).")

    return [
        TaskDraft(
            index=index,
            name=_combo_name(combo),
            params=combo,
            config=sweep.config,
            args_template=sweep.args,
            env_template=sweep.env,
            arg_style=sweep.arg_style,
        )
        for index, combo in enumerate(combos)
    ]


def _flatten(config: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    items: list[tuple[tuple[str, ...], Any]] = []
    for key, value in config.items():
        path = (*prefix, str(key))
        if isinstance(value, dict):
            if not value:
                raise UserError(f"config.{'.'.join(path)} is an empty mapping; remove it or give it keys.")
            items.extend(_flatten(value, path))
        else:
            items.append((path, value))
    return items


def render_args_from_config(config: dict[str, Any], style: ArgStyle) -> list[str]:
    """Derive command-line args from a resolved config tree (when ``args`` is omitted)."""
    if style == "none":
        return []
    args: list[str] = []
    if style == "posix":
        for path, value in _flatten(config):
            flag = "--" + "-".join(part.replace("_", "-") for part in path)
            if value is None:
                continue
            if isinstance(value, bool):
                args.append(flag if value else "--no-" + flag[2:])
            elif isinstance(value, list):
                args.append(flag)
                args.extend(to_text(item) for item in value)
            else:
                args.append(flag)
                args.append(to_text(value))
        return args
    if style == "hydra":

        def hydra_value(value: Any) -> str:
            if value is None:
                return "null"
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, list):
                return "[" + ",".join(hydra_value(item) for item in value) + "]"
            return str(value)

        for path, value in _flatten(config):
            args.append(f"{'.'.join(path)}={hydra_value(value)}")
        return args
    raise UserError(f"Unknown arg style: {style!r} (expected posix, hydra, or none).")
