"""Project configuration (``.slurmdeck/project.yaml`` — human-edited)."""

from __future__ import annotations

from pydantic import Field

from slurmdeck.models.common import StrictModel
from slurmdeck.models.env import EnvSpec
from slurmdeck.models.resources import Resources


class SyncConfig(StrictModel):
    """Code snapshot options."""

    include_untracked: bool = False
    ignore_file: str = ".slurmdeckignore"
    extra_ignores: list[str] = Field(default_factory=list)
    allow_sensitive_files: list[str] = Field(default_factory=list)


class ProjectConfig(StrictModel):
    schema_version: int = 1
    project_id: str
    display_name: str
    remote: str | None = None  # overrides the user-level current remote
    resources: Resources = Field(default_factory=Resources)
    env: EnvSpec | None = None
    sync: SyncConfig = Field(default_factory=SyncConfig)
