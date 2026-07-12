"""Resolve and validate exact environment generations used by runs."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.cluster import BuildExecutor, ClusterProfile, ModuleInitializationStrategy
from slurmdeck.models.env import (
    CondaEnvSpec,
    EnvBinding,
    EnvBuildAttempt,
    EnvironmentPlan,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvWaitPolicy,
    ExistingEnvSpec,
)
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.remote import Remote
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_planning import EnvironmentPlanningService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import Transport, TransportError, parse_json_lines

_ACTIVE = {
    EnvironmentStatus.STAGING,
    EnvironmentStatus.QUEUED,
    EnvironmentStatus.BUILDING,
    EnvironmentStatus.VERIFYING,
    EnvironmentStatus.BUILD_UNKNOWN,
}


@dataclass(frozen=True)
class BindingCheck:
    state: str
    reason: str
    snapshot_exists: bool | None = None


class EnvironmentRunBindingService:
    def __init__(
        self,
        planning: EnvironmentPlanningService | None = None,
        *,
        cache: EnvironmentCache | None = None,
    ) -> None:
        self._planning = planning or EnvironmentPlanningService(cache=cache)
        self._use_cache = cache is not None

    def resolve(
        self,
        *,
        transport: Transport,
        remote: Remote,
        layout: RemoteLayout,
        project: ProjectConfig,
        project_dir: Path,
        wait_policy: EnvWaitPolicy = EnvWaitPolicy.READY,
    ) -> EnvBinding | None:
        if project.env is None:
            return None
        planner = self._planning.plan_for_prepare if self._use_cache else self._planning.plan
        plan = planner(
            transport=transport,
            remote=remote,
            layout=layout,
            project=project,
            project_dir=project_dir,
        )
        try:
            return self._resolve_plan(plan, wait_policy)
        except UserError:
            if not self._use_cache:
                raise
        # A cache miss or stale non-ready record is not authoritative.  Pay
        # for the full read-only plan only on this fallback path.
        plan = self._planning.plan(
            transport=transport,
            remote=remote,
            layout=layout,
            project=project,
            project_dir=project_dir,
        )
        return self._resolve_plan(plan, wait_policy)

    def _resolve_plan(self, plan: EnvironmentPlan, wait_policy: EnvWaitPolicy) -> EnvBinding:
        record = plan.registry_record
        if record is None:
            raise UserError(
                f"Environment {plan.env_id} is not prepared.",
                hint="Run `slurmdeck env prepare` before submitting this run.",
            )
        ready = self._ready_binding(record)
        if ready is not None:
            return ready
        if wait_policy is EnvWaitPolicy.READY:
            raise UserError(
                f"Environment {record.env_id} is {record.status.value}, not READY.",
                hint=f"Wait for `slurmdeck env status {record.env_id}` to report READY, or use --env-wait afterok.",
            )
        if not plan.complete or not plan.afterok_eligible:
            detail = "; ".join([*plan.missing, *plan.conflicts])
            raise UserError(
                f"afterok is not safe for environment {record.env_id}." + (f" {detail}" if detail else ""),
                hint="Use --env-wait ready or complete the cluster dependency policy.",
            )
        attempt = self._current_attempt(record)
        if (
            attempt is None
            or attempt.executor is not BuildExecutor.SLURM
            or attempt.status not in _ACTIVE
            or attempt.generation_id is None
            or not attempt.prefix
            or not attempt.job_id.isdigit()
        ):
            if record.status in {EnvironmentStatus.FAILED, EnvironmentStatus.CANCELLED}:
                raise UserError(f"Environment build is {record.status.value}; a dependent run cannot be submitted.")
            raise UserError(
                f"Environment {record.env_id} has no active Slurm build eligible for afterok.",
                hint="Use --env-wait ready.",
            )
        return EnvBinding(
            env_id=record.env_id,
            generation_id=attempt.generation_id,
            prefix=attempt.prefix,
            attempt_id=attempt.attempt_id,
            build_job_id=attempt.job_id,
            wait_policy=EnvWaitPolicy.AFTEROK,
        )

    def check(
        self,
        transport: Transport,
        layout: RemoteLayout,
        binding: EnvBinding,
        *,
        snapshot_hash: str = "",
    ) -> BindingCheck:
        args = [
            "binding-check",
            "--base",
            layout.base,
            "--binding-json",
            binding.model_dump_json(),
        ]
        if snapshot_hash:
            args += ["--snapshot-hash", snapshot_hash]
        result = transport.exec_python(
            protocol.env_agent_source(),
            args,
            timeout=120,
            check=False,
        )
        try:
            payloads = parse_json_lines(result.stdout)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                "Remote environment binding check returned malformed JSON.",
                returncode=result.returncode,
                stderr=result.stderr,
                underlying_cause=exc,
            ) from exc
        raw = next(
            (
                item
                for item in reversed(payloads)
                if isinstance(item, dict)
                and item.get("kind") == protocol.ENV_HELPER_KIND
                and item.get("operation") == "binding-check"
            ),
            None,
        )
        if raw is None or raw.get("schema_version") != 1 or raw.get("ok") is not True:
            raise TransportError(
                f"Remote environment binding check failed: {(raw or {}).get('error', 'invalid result')}",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        state = raw.get("state")
        reason = raw.get("reason")
        if state not in {"ready", "waiting", "failed", "cancelled", "unknown", "missing"} or not isinstance(
            reason, str
        ):
            raise TransportError("Remote environment binding check returned an invalid state.")
        snapshot_exists = raw.get("snapshot_exists")
        if snapshot_hash:
            if not isinstance(snapshot_exists, bool):
                raise TransportError("Remote environment binding check returned an invalid snapshot state.")
        elif snapshot_exists is not None:
            raise TransportError("Remote environment binding check returned an unexpected snapshot state.")
        return BindingCheck(
            state=cast(str, state),
            reason=reason,
            snapshot_exists=snapshot_exists,
        )

    @staticmethod
    def require_for_submit(check: BindingCheck, binding: EnvBinding) -> None:
        if check.state == "ready":
            return
        if binding.wait_policy is EnvWaitPolicy.AFTEROK and check.state == "waiting":
            return
        code = {
            "failed": "ENV_BUILD_FAILED",
            "cancelled": "ENV_BUILD_CANCELLED",
        }.get(check.state, "ENV_NOT_READY")
        raise UserError(
            f"{code}: environment {binding.env_id} cannot be used: {check.reason}",
            hint=f"Inspect `slurmdeck env status {binding.env_id}` before submitting.",
        )

    @staticmethod
    def _ready_binding(record: EnvironmentRecord) -> EnvBinding | None:
        if record.status is not EnvironmentStatus.READY or not record.active_prefix:
            return None
        if record.active_generation:
            generation = next(
                (item for item in record.generations if item.generation_id == record.active_generation),
                None,
            )
            if generation is None:
                return None
            attempt = next((item for item in record.attempts if item.attempt_id == generation.attempt_id), None)
            return EnvBinding(
                env_id=record.env_id,
                generation_id=generation.generation_id,
                prefix=generation.prefix,
                attempt_id=generation.attempt_id,
                build_job_id=attempt.job_id if attempt else "",
                wait_policy=EnvWaitPolicy.READY,
            )
        return EnvBinding(
            env_id=record.env_id,
            generation_id="",
            prefix=record.active_prefix,
            attempt_id="",
            build_job_id="",
            wait_policy=EnvWaitPolicy.READY,
        )

    @staticmethod
    def _current_attempt(record: EnvironmentRecord) -> EnvBuildAttempt | None:
        return next((item for item in record.attempts if item.attempt_id == record.current_attempt), None)


def activation_script_for_binding(
    spec: CondaEnvSpec | ExistingEnvSpec,
    profile: ClusterProfile | None,
    binding: EnvBinding,
) -> str:
    lines = ["# generated by slurmdeck", "set -e"]
    if profile is not None:
        initialization = profile.module_initialization
        if initialization.strategy is ModuleInitializationStrategy.SOURCE and initialization.source:
            lines.append(f". {shlex.quote(initialization.source)}")
        elif initialization.strategy is ModuleInitializationStrategy.COMMANDS:
            lines.extend(initialization.commands)
    modules = []
    if isinstance(spec, CondaEnvSpec) and profile is not None:
        modules.extend(profile.conda.modules)
    modules.extend(spec.modules)
    modules = list(dict.fromkeys(modules))
    if modules and (profile is None or profile.module_initialization.strategy is None):
        lines.extend(
            [
                "if ! command -v module >/dev/null 2>&1; then",
                "  for _profile in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh /usr/share/lmod/lmod/init/bash; do",
                '    if [ -r "$_profile" ]; then . "$_profile"; break; fi',
                "  done",
                "fi",
            ]
        )
    lines.extend(f"module load {shlex.quote(module)}" for module in modules)
    quoted = shlex.quote(binding.prefix)
    lines.extend(
        [
            f"if [ -d {quoted}/conda-meta ]; then",
            f"  export CONDA_PREFIX={quoted}",
            "  export PATH={}:$PATH".format(shlex.quote(binding.prefix + "/bin")),
            f"  if [ -d {quoted}/etc/conda/activate.d ]; then",
            f"    for _script in {quoted}/etc/conda/activate.d/*.sh; do",
            '      [ -r "$_script" ] && . "$_script"',
            "    done",
            "  fi",
            f"elif [ -f {quoted}/bin/activate ]; then",
            f"  . {quoted}/bin/activate",
            "else",
            f"  echo 'slurmdeck: bound environment prefix not found: {binding.prefix}' >&2",
            "  exit 127",
            "fi",
        ]
    )
    return "\n".join(lines) + "\n"
