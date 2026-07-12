"""Sweep file schema, version 1 (strict).

Two mutually exclusive forms:

Matrix form::

    version: 1
    parameters:            # cartesian product
      lr: [1.0e-3, 1.0e-4]
      model: [small, large]
    include:               # extra explicit combinations (optional)
      - {lr: 1.0e-3, model: xl}
    exclude:               # combinations to drop (optional)
      - {lr: 1.0e-4, model: large}
    config:                # per-task YAML config template (optional)
      training:
        lr: "{lr}"
    args: ["--lr", "{lr}"] # optional; else derived from config via arg_style
    arg_style: posix       # posix | hydra | none
    env:
      RUN_TAG: "{model}-lr{lr}"

Explicit form::

    version: 1
    tasks:
      - name: baseline
        config: {training: {lr: 1.0e-3}}
        args: ["--config", "{config}"]
        env: {SEED: "1"}

Unknown keys are rejected everywhere.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from slurmdeck.models.common import StrictModel

SWEEP_SCHEMA_VERSION = 1

Scalar = bool | int | float | str | None

ArgStyle = Literal["posix", "hydra", "none"]


def _coerce_env(raw: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            env[str(key)] = ""
        elif isinstance(value, bool):
            env[str(key)] = "true" if value else "false"
        else:
            env[str(key)] = str(value)
    return env


class SweepTaskSpec(StrictModel):
    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    args: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env", mode="before")
    @classmethod
    def _env_scalars(cls, value: Any) -> Any:
        return _coerce_env(value) if isinstance(value, dict) else value


class Sweep(StrictModel):
    version: Literal[1]
    name: str | None = None

    # Matrix form
    parameters: dict[str, list[Scalar]] | None = None
    include: list[dict[str, Scalar]] = Field(default_factory=list)
    exclude: list[dict[str, Scalar]] = Field(default_factory=list)
    config: dict[str, Any] | None = None
    args: list[str] | None = None
    arg_style: ArgStyle = "posix"
    env: dict[str, str] = Field(default_factory=dict)

    # Explicit form
    tasks: list[SweepTaskSpec] | None = None

    @field_validator("env", mode="before")
    @classmethod
    def _env_scalars(cls, value: Any) -> Any:
        return _coerce_env(value) if isinstance(value, dict) else value

    @field_validator("parameters")
    @classmethod
    def _non_empty_axes(cls, value: dict[str, list[Scalar]] | None) -> dict[str, list[Scalar]] | None:
        if value is not None:
            for key, options in value.items():
                if not options:
                    raise ValueError(f"parameters.{key} must be a non-empty list")
        return value

    @model_validator(mode="after")
    def _one_form(self) -> Sweep:
        matrix_fields = [
            name
            for name, value in (
                ("parameters", self.parameters),
                ("include", self.include),
                ("exclude", self.exclude),
                ("config", self.config),
                ("args", self.args),
                ("env", self.env),
            )
            if value
        ]
        if self.tasks is not None:
            if matrix_fields:
                raise ValueError(
                    f"'tasks' cannot be combined with matrix fields ({', '.join(matrix_fields)}); pick one form"
                )
            if not self.tasks:
                raise ValueError("'tasks' must contain at least one task")
        elif self.parameters is None and not self.include:
            raise ValueError("sweep needs either 'parameters' (matrix form) or 'tasks' (explicit form)")
        return self
