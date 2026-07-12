"""Side-effect-free environment identity and capability planning."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Sequence
from pathlib import Path

import yaml

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import (
    BuildExecutor,
    ClusterObservation,
    ClusterProfile,
    ModuleInitializationStrategy,
)
from slurmdeck.models.common import safe_name
from slurmdeck.models.env import (
    CondaEnvSpec,
    EnvBackend,
    EnvBuildAttempt,
    EnvironmentPlan,
    EnvironmentPlanAction,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvOwnership,
    ExistingEnvSpec,
)
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.remote import Remote
from slurmdeck.models.resources import Resources
from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_registry import EnvRegistryClient
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import Transport

_ACTIVE_BUILD_STATES = {
    EnvironmentStatus.STAGING,
    EnvironmentStatus.QUEUED,
    EnvironmentStatus.BUILDING,
    EnvironmentStatus.VERIFYING,
    EnvironmentStatus.BUILD_UNKNOWN,
}
_UNLIMITED_TIMES = {"UNLIMITED", "INFINITE", "N/A", "NOT_SET"}


class EnvironmentPlanner:
    """Resolve one project environment entirely in memory."""

    def __init__(self, capabilities: ClusterCapabilityService | None = None) -> None:
        self._capabilities = capabilities or ClusterCapabilityService()

    def plan(
        self,
        *,
        project: ProjectConfig,
        project_dir: Path,
        layout: RemoteLayout,
        profile: ClusterProfile | None,
        observation: ClusterObservation | None,
        registry: Sequence[EnvironmentRecord],
        requested_executor: BuildExecutor | None = None,
        rebuild: bool = False,
    ) -> EnvironmentPlan:
        spec = project.env
        if spec is None:
            raise UserError(
                "This project has no environment configured.",
                hint="Add an `env:` section to .slurmdeck/project.yaml.",
            )
        if isinstance(spec, CondaEnvSpec):
            return self._plan_conda(
                spec=spec,
                project=project,
                project_dir=project_dir,
                layout=layout,
                profile=profile,
                observation=observation,
                registry=registry,
                requested_executor=requested_executor,
                rebuild=rebuild,
            )
        return self._plan_existing(
            spec=spec,
            layout=layout,
            profile=profile,
            registry=registry,
            requested_executor=requested_executor,
            rebuild=rebuild,
        )

    def _plan_conda(
        self,
        *,
        spec: CondaEnvSpec,
        project: ProjectConfig,
        project_dir: Path,
        layout: RemoteLayout,
        profile: ClusterProfile | None,
        observation: ClusterObservation | None,
        registry: Sequence[EnvironmentRecord],
        requested_executor: BuildExecutor | None,
        rebuild: bool,
    ) -> EnvironmentPlan:
        path, file_bytes = _environment_file(project_dir, spec.spec_file)
        environment_file_hash = hashlib.sha256(file_bytes).hexdigest()
        channels, file_conflicts = _channels_from_environment_file(file_bytes, path)
        modules = _unique([*(profile.conda.modules if profile else []), *spec.modules])
        identity = {
            "schema_version": 1,
            "backend": EnvBackend.CONDA.value,
            "name": spec.name,
            "spec_file": spec.spec_file,
            "environment_file_hash": environment_file_hash,
            "modules": modules,
            "post_install": spec.post_install,
            "smoke_test": spec.smoke_test,
            "channels": channels,
            "channel_priority": spec.channel_priority.value,
            "solver": spec.solver.value,
        }
        full_hash = _canonical_hash(identity)
        env_id = _environment_id(spec.name, full_hash)
        contract = self._capabilities.resolve(
            profile,
            observation,
            requested_executor=requested_executor,
        )
        resources = project.resources.merged(spec.build_resources)
        conflicts = [*contract.conflicts, *file_conflicts]
        warnings = list(contract.warnings)
        effective_partition = resources.partition or contract.effective_partition
        resource_conflicts, resource_warnings = _resource_diagnostics(
            resources,
            effective_partition,
            observation,
        )
        conflicts.extend(resource_conflicts)
        warnings.extend(resource_warnings)
        action, record, attempt, registry_conflicts = _registry_action(
            env_id=env_id,
            full_hash=full_hash,
            ownership=EnvOwnership.MANAGED,
            registry=registry,
            rebuild=rebuild,
        )
        conflicts.extend(registry_conflicts)
        generation_root = layout.env_generations_dir(env_id)
        prefix = _planned_prefix(generation_root, action, record, attempt)
        missing = _unique(contract.missing)
        conflicts = _unique(conflicts)
        warnings = _unique(warnings)
        return EnvironmentPlan(
            action=action,
            env_id=env_id,
            full_hash=full_hash,
            backend=EnvBackend.CONDA,
            ownership=EnvOwnership.MANAGED,
            environment_file=str(path),
            environment_file_hash=environment_file_hash,
            prefix=prefix,
            generation_root=generation_root,
            modules=modules,
            post_install=spec.post_install,
            smoke_test=spec.smoke_test,
            channels=channels,
            channel_priority=spec.channel_priority,
            solver=spec.solver,
            platform=profile.platform.conda_subdir if profile else None,
            conda_executable=_conda_executable(profile, observation),
            module_initialization=_module_initialization(profile),
            resolved_resources=resources,
            requested_executor=requested_executor,
            executor=contract.executor,
            effective_partition=effective_partition,
            afterok_eligible=contract.afterok_eligible,
            registry_status=record.status if record else None,
            current_attempt=record.current_attempt if record else None,
            build_job_id=attempt.job_id if attempt else "",
            registry_record=record,
            complete=not missing and not conflicts,
            missing=missing,
            conflicts=conflicts,
            warnings=warnings,
        )

    def _plan_existing(
        self,
        *,
        spec: ExistingEnvSpec,
        layout: RemoteLayout,
        profile: ClusterProfile | None,
        registry: Sequence[EnvironmentRecord],
        requested_executor: BuildExecutor | None,
        rebuild: bool,
    ) -> EnvironmentPlan:
        identity = {
            "schema_version": 1,
            "backend": EnvBackend.EXISTING.value,
            "name": spec.name,
            "prefix": spec.prefix,
            "modules": spec.modules,
            "smoke_test": spec.smoke_test,
        }
        full_hash = _canonical_hash(identity)
        env_id = _environment_id(spec.name, full_hash)
        action, record, attempt, conflicts = _registry_action(
            env_id=env_id,
            full_hash=full_hash,
            ownership=EnvOwnership.EXTERNAL,
            registry=registry,
            rebuild=rebuild,
        )
        warnings: list[str] = []
        if requested_executor is not None:
            warnings.append("build executor selection is ignored for an existing environment")
        if rebuild:
            conflicts.append("external environments cannot be rebuilt; verify the prefix instead")
            action = EnvironmentPlanAction.VERIFY
        prefix = record.active_prefix if action is EnvironmentPlanAction.REUSE and record else spec.prefix
        return EnvironmentPlan(
            action=action,
            env_id=env_id,
            full_hash=full_hash,
            backend=EnvBackend.EXISTING,
            ownership=EnvOwnership.EXTERNAL,
            prefix=prefix or spec.prefix,
            modules=spec.modules,
            module_initialization=_module_initialization(profile),
            smoke_test=spec.smoke_test,
            registry_status=record.status if record else None,
            current_attempt=record.current_attempt if record else None,
            build_job_id=attempt.job_id if attempt else "",
            registry_record=record,
            complete=not conflicts,
            conflicts=_unique(conflicts),
            warnings=warnings,
        )


class EnvironmentPlanningService:
    """Gather read-only remote inputs and delegate to the pure planner."""

    def __init__(
        self,
        *,
        capabilities: ClusterCapabilityService | None = None,
        registry: EnvRegistryClient | None = None,
        planner: EnvironmentPlanner | None = None,
        cache: EnvironmentCache | None = None,
    ) -> None:
        self._capabilities = capabilities or ClusterCapabilityService()
        self._registry = registry or EnvRegistryClient()
        self._planner = planner or EnvironmentPlanner(self._capabilities)
        self._cache = cache

    def plan(
        self,
        *,
        transport: Transport,
        remote: Remote,
        layout: RemoteLayout,
        project: ProjectConfig,
        project_dir: Path,
        requested_executor: BuildExecutor | None = None,
        rebuild: bool = False,
    ) -> EnvironmentPlan:
        observation = self._capabilities.observe(transport, remote)
        registry = self._registry.inspect(transport, layout)
        return self._resolved_plan(
            observation=observation,
            registry=registry,
            remote=remote,
            layout=layout,
            project=project,
            project_dir=project_dir,
            requested_executor=requested_executor,
            rebuild=rebuild,
        )

    def plan_for_prepare(
        self,
        *,
        transport: Transport,
        remote: Remote,
        layout: RemoteLayout,
        project: ProjectConfig,
        project_dir: Path,
        requested_executor: BuildExecutor | None = None,
        rebuild: bool = False,
    ) -> EnvironmentPlan:
        """Plan from advisory local inputs before one authoritative helper call.

        Without an explicit cache this preserves the fully fresh, two-call
        planning behavior.  Mutating prepare may refresh and persist a stale
        observation, while the remote prepare/candidate helper remains the
        authority for registry state.
        """
        if self._cache is None:
            return self.plan(
                transport=transport,
                remote=remote,
                layout=layout,
                project=project,
                project_dir=project_dir,
                requested_executor=requested_executor,
                rebuild=rebuild,
            )
        observation = self._cache.observation(remote)
        if observation is None:
            observation = self._capabilities.observe(transport, remote)
            self._cache.remember_observation(remote, observation)
        return self._resolved_plan(
            observation=observation,
            registry=self._cache.registry(remote),
            remote=remote,
            layout=layout,
            project=project,
            project_dir=project_dir,
            requested_executor=requested_executor,
            rebuild=rebuild,
        )

    def _resolved_plan(
        self,
        *,
        observation: ClusterObservation,
        registry: Sequence[EnvironmentRecord],
        remote: Remote,
        layout: RemoteLayout,
        project: ProjectConfig,
        project_dir: Path,
        requested_executor: BuildExecutor | None,
        rebuild: bool,
    ) -> EnvironmentPlan:
        return self._planner.plan(
            project=project,
            project_dir=project_dir,
            layout=layout,
            profile=remote.cluster,
            observation=observation,
            registry=registry,
            requested_executor=requested_executor,
            rebuild=rebuild,
        )


def _environment_file(project_dir: Path, configured_path: str) -> tuple[Path, bytes]:
    root = project_dir.resolve()
    candidate = (root / configured_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise UserError(
            f"Environment spec file must stay inside the project: {configured_path}",
            hint="Point env.spec_file at a project-relative environment YAML file.",
        ) from None
    try:
        return candidate, candidate.read_bytes()
    except FileNotFoundError:
        raise UserError(
            f"Environment spec file not found: {candidate}",
            hint="Point env.spec_file in .slurmdeck/project.yaml at your environment.yml.",
        ) from None
    except OSError as exc:
        raise UserError(f"Could not read environment spec file {candidate}: {exc}") from exc


def _channels_from_environment_file(file_bytes: bytes, path: Path) -> tuple[list[str], list[str]]:
    try:
        document = yaml.safe_load(file_bytes)
    except yaml.YAMLError as exc:
        return [], [f"{path.name} is invalid YAML: {exc}"]
    if not isinstance(document, dict):
        return [], [f"{path.name} must contain a top-level mapping"]
    raw_channels = document.get("channels")
    if raw_channels is None:
        return [], [f"{path.name} must declare channels explicitly"]
    if not isinstance(raw_channels, list):
        return [], [f"{path.name} channels must be a list of non-empty strings"]
    channels: list[str] = []
    conflicts: list[str] = []
    for index, value in enumerate(raw_channels):
        if not isinstance(value, str) or not value.strip():
            conflicts.append(f"{path.name} channels[{index}] must be a non-empty string")
            continue
        channel = value.strip()
        if channel in channels:
            conflicts.append(f"{path.name} declares duplicate channel {channel!r}")
            continue
        channels.append(channel)
    if not channels and not conflicts:
        conflicts.append(f"{path.name} must declare at least one channel")
    return channels, conflicts


def _canonical_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _conda_executable(profile: ClusterProfile | None, observation: ClusterObservation | None) -> str:
    if profile is not None:
        if profile.conda.executable:
            return profile.conda.executable
        if profile.conda.modules:
            return "conda"
    return observation.conda_path if observation and observation.conda_path else ""


def _module_initialization(profile: ClusterProfile | None) -> list[str]:
    if profile is None:
        return []
    initialization = profile.module_initialization
    strategy = initialization.strategy
    if strategy is ModuleInitializationStrategy.SOURCE and initialization.source:
        return [f". {shlex.quote(initialization.source)}"]
    if strategy is ModuleInitializationStrategy.COMMANDS:
        return list(initialization.commands)
    return []


def _environment_id(name: str, full_hash: str) -> str:
    label = safe_name(name, fallback="env")[:51].rstrip("-._") or "env"
    return f"{label}-{full_hash[:12]}"


def _registry_action(
    *,
    env_id: str,
    full_hash: str,
    ownership: EnvOwnership,
    registry: Sequence[EnvironmentRecord],
    rebuild: bool,
) -> tuple[EnvironmentPlanAction, EnvironmentRecord | None, EnvBuildAttempt | None, list[str]]:
    default = EnvironmentPlanAction.CREATE if ownership is EnvOwnership.MANAGED else EnvironmentPlanAction.VERIFY
    candidates = [record for record in registry if record.env_id == env_id]
    conflicts: list[str] = []
    if len(candidates) > 1:
        conflicts.append(f"environment registry contains duplicate records for {env_id}")
    record = candidates[0] if candidates else None
    if record is None:
        return default, None, None, conflicts
    if record.full_hash != full_hash:
        conflicts.append(f"environment id hash collision for {env_id}; full registry hash does not match")
        return default, record, None, conflicts
    if record.ownership is not ownership:
        conflicts.append(f"environment registry ownership for {env_id} does not match the project backend")
        return default, record, None, conflicts
    attempt = next(
        (attempt for attempt in record.attempts if attempt.attempt_id == record.current_attempt),
        None,
    )
    if rebuild and ownership is EnvOwnership.MANAGED:
        if attempt is not None and record.status in _ACTIVE_BUILD_STATES:
            conflicts.append(f"environment {env_id} already has active attempt {attempt.attempt_id}")
            return EnvironmentPlanAction.ATTACH, record, attempt, conflicts
        return EnvironmentPlanAction.REBUILD, record, attempt, conflicts
    if record.status is EnvironmentStatus.READY:
        return EnvironmentPlanAction.REUSE, record, attempt, conflicts
    if attempt is not None and record.status in _ACTIVE_BUILD_STATES:
        return EnvironmentPlanAction.ATTACH, record, attempt, conflicts
    if record.status in _ACTIVE_BUILD_STATES:
        conflicts.append(f"environment {env_id} is {record.status.value} without a current attempt")
    if record.active_generation is not None and ownership is EnvOwnership.MANAGED:
        return EnvironmentPlanAction.REBUILD, record, attempt, conflicts
    return default, record, attempt, conflicts


def _planned_prefix(
    generation_root: str,
    action: EnvironmentPlanAction,
    record: EnvironmentRecord | None,
    attempt: EnvBuildAttempt | None,
) -> str:
    if action is EnvironmentPlanAction.REUSE and record and record.active_prefix:
        return record.active_prefix
    if action is EnvironmentPlanAction.ATTACH and attempt and attempt.prefix:
        return attempt.prefix
    return f"{generation_root}/<generation_id>"


def _resource_diagnostics(
    resources: Resources,
    partition: str | None,
    observation: ClusterObservation | None,
) -> tuple[list[str], list[str]]:
    if observation is None or partition is None or not observation.partitions:
        return [], []
    observed = next((item for item in observation.partitions if item.name == partition), None)
    if observed is None:
        return [f"Slurm partition {partition!r} was not observed"], []
    requested_seconds = _slurm_time_seconds(resources.time)
    if requested_seconds is None:
        return [f"build resource time {resources.time!r} is not a valid Slurm duration"], []
    if observed.max_time.upper() in _UNLIMITED_TIMES:
        return [], []
    maximum_seconds = _slurm_time_seconds(observed.max_time)
    if maximum_seconds is None:
        return [], [f"observed partition {partition!r} has unrecognized max time {observed.max_time!r}"]
    if requested_seconds > maximum_seconds:
        return [f"build time {resources.time} exceeds partition {partition!r} limit {observed.max_time}"], []
    return [], []


def _slurm_time_seconds(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        if not day_text.isdigit():
            return None
        days = int(day_text)
    parts = text.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 1:
        hours, minutes, seconds = 0, numbers[0], 0
    elif len(numbers) == 2:
        hours, minutes, seconds = 0, numbers[0], numbers[1]
    elif len(numbers) == 3:
        hours, minutes, seconds = numbers
    else:
        return None
    if (len(numbers) > 1 and minutes >= 60) or seconds >= 60:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
