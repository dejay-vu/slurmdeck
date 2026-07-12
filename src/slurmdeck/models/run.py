"""Run and task manifests (versioned; written to the run directory and remote)."""

from __future__ import annotations

from pydantic import Field, model_validator

from slurmdeck.errors import SchemaVersionError
from slurmdeck.models.common import StrictModel
from slurmdeck.models.env import EnvBinding
from slurmdeck.models.resources import Resources

RUN_SCHEMA_VERSION = 1


class CommandTemplateSpec(StrictModel):
    """The user command as given: an argv vector XOR a shell string.

    Placeholders are still unresolved here; this is what retry reuses.
    """

    argv: list[str] | None = None
    shell: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> CommandTemplateSpec:
        if bool(self.argv) == bool(self.shell):
            raise ValueError("set exactly one of 'argv' or 'shell'")
        return self


class TaskSpec(StrictModel):
    """A fully resolved task: everything the remote agent needs to run it.

    All paths are relative to the run root on the remote side.
    """

    index: int
    task_id: str
    name: str
    argv: list[str] | None = None
    shell: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    config: str | None = None  # run-root-relative config file, if any
    result_dir: str

    @model_validator(mode="after")
    def _exactly_one_command(self) -> TaskSpec:
        if bool(self.argv) == bool(self.shell):
            raise ValueError("task must have exactly one of 'argv' or 'shell'")
        return self


class RunManifest(StrictModel):
    schema_version: int = RUN_SCHEMA_VERSION
    project_id: str
    project_display_name: str
    run_id: str
    name: str
    created_at: str
    remote: str
    remote_root: str
    snapshot_hash: str = ""
    env_binding: EnvBinding | None = None
    env_dependency_state: str = ""
    env_dependency_reason: str = ""
    resources: Resources
    command: CommandTemplateSpec
    sweep_file: str | None = None
    retry_of: str | None = None
    task_count: int

    @model_validator(mode="after")
    def _supported_version(self) -> RunManifest:
        if self.schema_version != RUN_SCHEMA_VERSION:
            raise SchemaVersionError("run manifest", self.schema_version, RUN_SCHEMA_VERSION)
        return self
