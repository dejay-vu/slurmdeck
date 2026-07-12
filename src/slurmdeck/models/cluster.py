"""Explicit cluster policy, read-only observations, and their effective contract."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from slurmdeck.models.common import StrictModel

CLUSTER_SCHEMA_VERSION = 1


class BuildExecutor(StrEnum):
    SLURM = "slurm"
    LOGIN = "login"


class LoginBuildPolicy(StrEnum):
    FORBIDDEN = "forbidden"
    ALLOWED = "allowed"


class ModuleInitializationStrategy(StrEnum):
    NONE = "none"
    SOURCE = "source"
    COMMANDS = "commands"


class ComputeNetworkAccess(StrEnum):
    FULL = "full"
    RESTRICTED = "restricted"
    NONE = "none"


class ChannelAccess(StrEnum):
    DIRECT = "direct"
    MIRRORS = "mirrors"
    NONE = "none"


class InvalidDependencyPolicy(StrEnum):
    PER_JOB = "per_job"
    SITE_WIDE = "site_wide"
    UNSUPPORTED = "unsupported"


class SharedFilesystemProfile(StrictModel):
    login_to_compute: bool | None = None


class ModuleInitializationProfile(StrictModel):
    strategy: ModuleInitializationStrategy | None = None
    source: str | None = None
    commands: list[str] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _nonempty_source(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("source must be non-empty when provided")
        return value

    @field_validator("commands")
    @classmethod
    def _nonempty_commands(cls, value: list[str]) -> list[str]:
        if any(not command.strip() for command in value):
            raise ValueError("commands must not contain empty values")
        return value

    @model_validator(mode="after")
    def _strategy_inputs_match(self) -> ModuleInitializationProfile:
        if self.strategy is ModuleInitializationStrategy.SOURCE and not (self.source or "").strip():
            raise ValueError("source is required when strategy is 'source'")
        if self.strategy is ModuleInitializationStrategy.COMMANDS and not self.commands:
            raise ValueError("commands are required when strategy is 'commands'")
        if self.strategy is ModuleInitializationStrategy.NONE and (self.source or self.commands):
            raise ValueError("strategy 'none' cannot include source or commands")
        return self


class CondaProfile(StrictModel):
    executable: str | None = None
    modules: list[str] = Field(default_factory=list)

    @field_validator("executable")
    @classmethod
    def _nonempty_executable(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("executable must be non-empty when provided")
        return value

    @field_validator("modules")
    @classmethod
    def _nonempty_modules(cls, value: list[str]) -> list[str]:
        if any(not module.strip() for module in value):
            raise ValueError("modules must not contain empty values")
        return value


class NetworkProfile(StrictModel):
    compute_access: ComputeNetworkAccess | None = None
    channel_access: ChannelAccess | None = None
    mirrors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mirror_policy_is_explicit(self) -> NetworkProfile:
        if self.channel_access is ChannelAccess.MIRRORS and not self.mirrors:
            raise ValueError("mirrors are required when channel_access is 'mirrors'")
        if self.channel_access is not ChannelAccess.MIRRORS and self.mirrors:
            raise ValueError("mirrors require channel_access 'mirrors'")
        return self


class SlurmClusterProfile(StrictModel):
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    constraint: str | None = None
    afterok_dependency: bool | None = None
    kill_invalid_dependency: InvalidDependencyPolicy | None = None

    @field_validator("partition", "account", "qos", "constraint")
    @classmethod
    def _nonempty_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must be non-empty when provided")
        return value


class PlatformProfile(StrictModel):
    system: str | None = None
    machine: str | None = None
    conda_subdir: str | None = None

    @field_validator("system", "machine", "conda_subdir")
    @classmethod
    def _nonempty_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must be non-empty when provided")
        return value


class ClusterProfile(StrictModel):
    schema_version: int = CLUSTER_SCHEMA_VERSION
    allowed_build_executors: list[BuildExecutor] = Field(default_factory=list)
    default_build_executor: BuildExecutor | None = None
    login_build_policy: LoginBuildPolicy | None = None
    shared_filesystem: SharedFilesystemProfile = Field(default_factory=SharedFilesystemProfile)
    module_initialization: ModuleInitializationProfile = Field(default_factory=ModuleInitializationProfile)
    conda: CondaProfile = Field(default_factory=CondaProfile)
    network: NetworkProfile = Field(default_factory=NetworkProfile)
    slurm: SlurmClusterProfile = Field(default_factory=SlurmClusterProfile)
    platform: PlatformProfile = Field(default_factory=PlatformProfile)

    @model_validator(mode="after")
    def _consistent_policy(self) -> ClusterProfile:
        if self.schema_version != CLUSTER_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CLUSTER_SCHEMA_VERSION}")
        if len(set(self.allowed_build_executors)) != len(self.allowed_build_executors):
            raise ValueError("allowed_build_executors contains duplicates")
        if self.default_build_executor is not None and self.default_build_executor not in self.allowed_build_executors:
            raise ValueError("default_build_executor must be present in allowed_build_executors")
        if (
            BuildExecutor.LOGIN in self.allowed_build_executors
            and self.login_build_policy is LoginBuildPolicy.FORBIDDEN
        ):
            raise ValueError("login_build_policy forbids an allowed login executor")
        if self.conda.modules and self.module_initialization.strategy is ModuleInitializationStrategy.NONE:
            raise ValueError("conda modules require module initialization")
        return self


class ToolObservation(StrictModel):
    available: bool
    path: str = ""
    version: str = ""


class PartitionObservation(StrictModel):
    name: str
    is_default: bool = False
    max_time: str = ""


class ClusterObservation(StrictModel):
    schema_version: int = CLUSTER_SCHEMA_VERSION
    observed_at: float
    python_version: str
    tools: dict[str, ToolObservation] = Field(default_factory=dict)
    base_writable: bool | None = None
    default_partition: str | None = None
    partitions: list[PartitionObservation] = Field(default_factory=list)
    system: str
    machine: str
    conda_subdir: str | None = None
    module_available: bool = False
    conda_path: str | None = None
    shared_path_visible: bool | None = None
    afterok_dependency_supported: bool | None = None
    kill_invalid_dependency_supported: bool | None = None
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _supported_schema(self) -> ClusterObservation:
        if self.schema_version != CLUSTER_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CLUSTER_SCHEMA_VERSION}")
        return self


class EffectiveClusterContract(StrictModel):
    profile_present: bool
    requested_executor: BuildExecutor | None = None
    executor: BuildExecutor | None = None
    effective_partition: str | None = None
    effective_account: str | None = None
    effective_qos: str | None = None
    effective_constraint: str | None = None
    complete: bool
    missing: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    afterok_eligible: bool = False
    kill_invalid_dependency: InvalidDependencyPolicy | None = None
