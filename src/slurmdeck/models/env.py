"""Environment specifications (discriminated on ``type``)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, computed_field, model_validator

from slurmdeck.models.cluster import BuildExecutor
from slurmdeck.models.common import NameStr, StrictModel
from slurmdeck.models.resources import ResourceOverrides, Resources

ENV_REGISTRY_SCHEMA_VERSION: Literal[1] = 1
FullHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class EnvBackend(StrEnum):
    CONDA = "conda"
    EXISTING = "existing"


class EnvOwnership(StrEnum):
    MANAGED = "managed"
    EXTERNAL = "external"


class CondaChannelPriority(StrEnum):
    STRICT = "strict"
    FLEXIBLE = "flexible"
    DISABLED = "disabled"


class CondaSolver(StrEnum):
    LIBMAMBA = "libmamba"
    CLASSIC = "classic"


class EnvironmentPlanAction(StrEnum):
    REUSE = "reuse"
    ATTACH = "attach"
    CREATE = "create"
    REBUILD = "rebuild"
    VERIFY = "verify"


class EnvWaitPolicy(StrEnum):
    READY = "ready"
    AFTEROK = "afterok"


class EnvironmentStatus(StrEnum):
    PLANNED = "PLANNED"
    STAGING = "STAGING"
    QUEUED = "QUEUED"
    BUILDING = "BUILDING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUILD_UNKNOWN = "BUILD_UNKNOWN"
    REMOVING = "REMOVING"
    REMOVED = "REMOVED"
    REMOVE_UNKNOWN = "REMOVE_UNKNOWN"


class EnvironmentError(StrictModel):
    code: str
    summary: str
    detail: str = ""
    remediation: str = ""
    context: dict[str, object] = Field(default_factory=dict)


class EnvironmentProvenance(StrictModel):
    canonical_spec_hash: FullHash
    environment_file_hash: FullHash | None = None
    channels: list[str] = Field(default_factory=list)
    channel_priority: str | None = None
    solver: str | None = None
    platform: str | None = None
    package_urls: list[str] = Field(default_factory=list)


class EnvBuildAttempt(StrictModel):
    schema_version: Literal[1] = ENV_REGISTRY_SCHEMA_VERSION
    attempt_id: NameStr
    status: EnvironmentStatus
    executor: BuildExecutor
    generation_id: NameStr | None = None
    prefix: str = ""
    job_id: str = ""
    scheduler_state: str = ""
    scheduler_reason: str = ""
    resolved_resources: Resources = Field(default_factory=Resources)
    module_stack: list[str] = Field(default_factory=list)
    conda_executable: str = ""
    conda_version: str = ""
    resolved_channels: list[str] = Field(default_factory=list)
    build_dir: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    error_code: str = ""
    error_summary: str = ""
    login_host: str = ""
    login_pid: int | None = None
    heartbeat_at: str | None = None


class EnvGeneration(StrictModel):
    schema_version: Literal[1] = ENV_REGISTRY_SCHEMA_VERSION
    generation_id: NameStr
    attempt_id: NameStr
    prefix: str
    status: EnvironmentStatus
    created_at: str
    verified_at: str | None = None
    provenance: EnvironmentProvenance


class EnvironmentRecord(StrictModel):
    """First-format remote registry record; references are deliberately absent."""

    schema_version: Literal[1] = ENV_REGISTRY_SCHEMA_VERSION
    env_id: NameStr
    full_hash: FullHash
    backend: EnvBackend
    ownership: EnvOwnership
    status: EnvironmentStatus
    active_generation: NameStr | None = None
    active_prefix: str | None = None
    created_at: str
    updated_at: str
    verified_at: str | None = None
    current_attempt: NameStr | None = None
    generations: list[EnvGeneration] = Field(default_factory=list)
    attempts: list[EnvBuildAttempt] = Field(default_factory=list)
    last_error: EnvironmentError | None = None
    provenance: EnvironmentProvenance

    @model_validator(mode="after")
    def _identity_and_pointers_are_consistent(self) -> EnvironmentRecord:
        if not self.env_id.endswith("-" + self.full_hash[:12]):
            raise ValueError("env_id must end with the first 12 characters of full_hash")
        if self.provenance.canonical_spec_hash != self.full_hash:
            raise ValueError("provenance canonical_spec_hash must match full_hash")
        if self.backend is EnvBackend.CONDA and self.ownership is not EnvOwnership.MANAGED:
            raise ValueError("conda registry records must be managed")
        if self.backend is EnvBackend.EXISTING and self.ownership is not EnvOwnership.EXTERNAL:
            raise ValueError("existing registry records must be external")
        if self.ownership is EnvOwnership.EXTERNAL and self.generations:
            raise ValueError("external registry records cannot contain managed generations")
        generation_ids = [generation.generation_id for generation in self.generations]
        attempt_ids = [attempt.attempt_id for attempt in self.attempts]
        if len(generation_ids) != len(set(generation_ids)):
            raise ValueError("generation ids must be unique")
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("attempt ids must be unique")
        if self.active_generation is not None and self.active_generation not in generation_ids:
            raise ValueError("active_generation must identify a stored generation")
        if any(generation.provenance.canonical_spec_hash != self.full_hash for generation in self.generations):
            raise ValueError("generation provenance must match full_hash")
        if self.active_generation is not None:
            active = next(
                generation for generation in self.generations if generation.generation_id == self.active_generation
            )
            if self.active_prefix != active.prefix:
                raise ValueError("active_prefix must match the active generation")
        if self.current_attempt is not None and self.current_attempt not in attempt_ids:
            raise ValueError("current_attempt must identify a stored attempt")
        if self.status is EnvironmentStatus.READY and not self.active_prefix:
            raise ValueError("READY registry records must identify an active prefix")
        if (
            self.status is EnvironmentStatus.READY
            and self.ownership is EnvOwnership.MANAGED
            and self.active_generation is None
        ):
            raise ValueError("READY managed registry records must identify an active generation")
        return self


class EnvironmentView(StrictModel):
    record: EnvironmentRecord
    references: list[str] = Field(default_factory=list)
    desired_by_project: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reference_count(self) -> int:
        return len(self.references)

    @property
    def latest_attempt(self) -> EnvBuildAttempt | None:
        return self.record.attempts[-1] if self.record.attempts else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def build_job_id(self) -> str:
        return self.latest_attempt.job_id if self.latest_attempt is not None else ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scheduler_state(self) -> str:
        return self.latest_attempt.scheduler_state if self.latest_attempt is not None else ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_reason(self) -> str:
        attempt = self.latest_attempt
        if attempt is not None:
            return attempt.scheduler_reason or attempt.error_summary
        return self.record.last_error.summary if self.record.last_error is not None else ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def job_reason(self) -> str:
        return (
            " / ".join(value for value in (self.build_job_id, self.scheduler_state, self.display_reason) if value)
            or "-"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_resources(self) -> Resources | None:
        return self.latest_attempt.resolved_resources if self.latest_attempt is not None else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stdout_path(self) -> str:
        return self.latest_attempt.stdout_path if self.latest_attempt is not None else ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stderr_path(self) -> str:
        return self.latest_attempt.stderr_path if self.latest_attempt is not None else ""


class EnvBinding(StrictModel):
    env_id: NameStr
    generation_id: str
    prefix: str
    attempt_id: str
    build_job_id: str
    wait_policy: EnvWaitPolicy = EnvWaitPolicy.READY

    @model_validator(mode="after")
    def _dependency_is_actionable(self) -> EnvBinding:
        if not self.prefix.strip():
            raise ValueError("prefix must not be empty")
        if self.wait_policy is EnvWaitPolicy.AFTEROK and (
            not self.generation_id or not self.attempt_id or not self.build_job_id.isdigit()
        ):
            raise ValueError("afterok bindings require generation, attempt, and numeric build job ids")
        return self


class EnvironmentPlan(StrictModel):
    schema_version: Literal[1] = ENV_REGISTRY_SCHEMA_VERSION
    action: EnvironmentPlanAction
    env_id: NameStr
    full_hash: FullHash
    backend: EnvBackend
    ownership: EnvOwnership
    environment_file: str | None = None
    environment_file_hash: FullHash | None = None
    prefix: str
    generation_root: str | None = None
    modules: list[str] = Field(default_factory=list)
    post_install: list[str] = Field(default_factory=list)
    smoke_test: str | None = None
    channels: list[str] = Field(default_factory=list)
    channel_priority: CondaChannelPriority | None = None
    solver: CondaSolver | None = None
    platform: str | None = None
    conda_executable: str = ""
    module_initialization: list[str] = Field(default_factory=list)
    resolved_resources: Resources | None = None
    requested_executor: BuildExecutor | None = None
    executor: BuildExecutor | None = None
    effective_partition: str | None = None
    afterok_eligible: bool = False
    registry_status: EnvironmentStatus | None = None
    current_attempt: NameStr | None = None
    build_job_id: str = ""
    registry_record: EnvironmentRecord | None = None
    complete: bool
    missing: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EnvironmentBuildRequest(StrictModel):
    """Validated input handed to the remote executor under the hash lock."""

    schema_version: Literal[1] = ENV_REGISTRY_SCHEMA_VERSION
    env_id: NameStr
    full_hash: FullHash
    rebuild: bool = False
    executor: BuildExecutor
    attempt_id: NameStr
    generation_id: NameStr
    prefix: str
    created_at: str
    environment_file_name: NameStr
    isolated_environment_file_name: NameStr
    environment_file_hash: FullHash
    modules: list[str] = Field(default_factory=list)
    module_initialization: list[str] = Field(default_factory=list)
    post_install: list[str] = Field(default_factory=list)
    smoke_test: str | None = None
    channels: list[str]
    channel_priority: CondaChannelPriority
    solver: CondaSolver
    platform: str
    conda_executable: str
    resolved_resources: Resources
    provenance: EnvironmentProvenance

    @model_validator(mode="after")
    def _identity_is_consistent(self) -> EnvironmentBuildRequest:
        if not self.env_id.endswith("-" + self.full_hash[:12]):
            raise ValueError("env_id must match full_hash")
        if self.provenance.canonical_spec_hash != self.full_hash:
            raise ValueError("provenance canonical_spec_hash must match full_hash")
        if self.provenance.environment_file_hash != self.environment_file_hash:
            raise ValueError("provenance environment_file_hash must match request")
        if not self.channels:
            raise ValueError("channels must not be empty")
        if not self.conda_executable.strip():
            raise ValueError("conda_executable must not be empty")
        return self


class EnvironmentExistingRequest(StrictModel):
    schema_version: Literal[1] = ENV_REGISTRY_SCHEMA_VERSION
    env_id: NameStr
    full_hash: FullHash
    prefix: str
    created_at: str
    modules: list[str] = Field(default_factory=list)
    module_initialization: list[str] = Field(default_factory=list)
    smoke_test: str | None = None
    provenance: EnvironmentProvenance

    @model_validator(mode="after")
    def _identity_is_consistent(self) -> EnvironmentExistingRequest:
        if not self.env_id.endswith("-" + self.full_hash[:12]):
            raise ValueError("env_id must match full_hash")
        if self.provenance.canonical_spec_hash != self.full_hash:
            raise ValueError("provenance canonical_spec_hash must match full_hash")
        if not self.prefix.strip():
            raise ValueError("prefix must not be empty")
        return self


class CondaEnvSpec(StrictModel):
    """A conda environment slurmdeck builds on the cluster from a spec file."""

    type: Literal["conda"] = "conda"
    name: str = "default"
    spec_file: str = "environment.yml"
    modules: list[str] = Field(default_factory=list)
    post_install: list[str] = Field(default_factory=list)
    smoke_test: str | None = None
    channel_priority: CondaChannelPriority = CondaChannelPriority.STRICT
    solver: CondaSolver = CondaSolver.LIBMAMBA
    build_resources: ResourceOverrides = Field(default_factory=ResourceOverrides)


class ExistingEnvSpec(StrictModel):
    """A pre-existing environment (conda prefix or venv) the user manages."""

    type: Literal["existing"] = "existing"
    name: str = "existing"
    prefix: str
    modules: list[str] = Field(default_factory=list)
    smoke_test: str | None = None


EnvSpec = Annotated[CondaEnvSpec | ExistingEnvSpec, Field(discriminator="type")]
