"""Project configuration (``.slurmdeck/project.yaml`` — human-edited)."""

from __future__ import annotations

from typing import Any

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from slurmdeck.models.common import NameStr, StrictModel
from slurmdeck.models.env import EnvSpec
from slurmdeck.models.resources import Resources


class SyncConfig(StrictModel):
    """Code snapshot options."""

    include_untracked: bool = False
    ignore_file: str = ".slurmdeckignore"
    extra_ignores: list[str] = Field(default_factory=list)
    allow_sensitive_files: list[str] = Field(default_factory=list)


class ProjectTarget(StrictModel):
    """One cluster-specific execution target for a project."""

    remote: NameStr
    resources: Resources = Field(default_factory=Resources)
    env: EnvSpec | None = None


class ProjectExecutionConfig(StrictModel):
    """A fully selected project target consumed by planning services."""

    project_id: str
    display_name: str
    target: NameStr | None = None
    remote: NameStr | None = None
    resources: Resources = Field(default_factory=Resources)
    env: EnvSpec | None = None
    sync: SyncConfig = Field(default_factory=SyncConfig)

    @model_validator(mode="after")
    def _target_requires_remote(self) -> ProjectExecutionConfig:
        if self.target is not None and self.remote is None:
            raise ValueError("a named project target must select a remote")
        return self


class ProjectConfig(StrictModel):
    schema_version: int = 1
    project_id: str
    display_name: str
    remote: NameStr | None = None  # overrides the user-level current remote
    resources: Resources = Field(default_factory=Resources)
    env: EnvSpec | None = None
    default_target: NameStr | None = None
    targets: dict[NameStr, ProjectTarget] = Field(default_factory=dict)
    sync: SyncConfig = Field(default_factory=SyncConfig)

    @model_validator(mode="after")
    def _legacy_or_targets(self) -> ProjectConfig:
        if not self.targets:
            if self.default_target is not None:
                raise ValueError("default_target requires a non-empty targets mapping")
            return self

        if self.default_target is None:
            raise ValueError("default_target is required when targets are configured")
        if self.default_target not in self.targets:
            raise ValueError(f"default_target {self.default_target!r} is not present in targets")
        mixed = sorted({"remote", "resources", "env"} & self.model_fields_set)
        if mixed:
            raise ValueError(
                "top-level "
                + ", ".join(mixed)
                + " cannot be combined with targets; put cluster-dependent settings in each target"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_layout(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Write only the selected legacy or named-target project layout."""
        payload = handler(self)
        if not isinstance(payload, dict):
            raise TypeError("project configuration did not serialize to a mapping")
        if self.targets:
            for field_name in ("remote", "resources", "env"):
                payload.pop(field_name, None)
        else:
            payload.pop("default_target", None)
            payload.pop("targets", None)
        return payload

    def execution(self, target: str | None = None, *, remote: str | None = None) -> ProjectExecutionConfig:
        """Resolve legacy configuration or one named target into one execution config."""
        if self.targets:
            if remote is not None:
                raise ValueError("remote overrides cannot be combined with project targets")
            chosen = target if target is not None else self.default_target
            if chosen is None or chosen not in self.targets:
                known = ", ".join(sorted(self.targets))
                raise ValueError(f"unknown project target {chosen!r} (known: {known})")
            configured = self.targets[chosen]
            return ProjectExecutionConfig(
                project_id=self.project_id,
                display_name=self.display_name,
                target=chosen,
                remote=configured.remote,
                resources=configured.resources,
                env=configured.env,
                sync=self.sync,
            )
        if target is not None:
            raise ValueError("this project uses legacy single-target configuration")
        return ProjectExecutionConfig(
            project_id=self.project_id,
            display_name=self.display_name,
            remote=remote if remote is not None else self.remote,
            resources=self.resources,
            env=self.env,
            sync=self.sync,
        )
