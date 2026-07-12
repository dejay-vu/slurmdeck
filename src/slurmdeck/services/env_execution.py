"""Policy-gated staging and invocation of remote environment executors."""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import ValidationError

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.cluster import BuildExecutor
from slurmdeck.models.env import (
    EnvBackend,
    EnvironmentBuildRequest,
    EnvironmentExistingRequest,
    EnvironmentPlan,
    EnvironmentPlanAction,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
)
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.remote import Remote
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_planning import EnvironmentPlanningService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.structured_errors import StructuredError
from slurmdeck.transport import Transport, TransportError, parse_json_lines

_PREPARE_ACTIONS = {item.value: item for item in EnvironmentPlanAction}
_TERMINAL_ENV_STATES = {EnvironmentStatus.READY, EnvironmentStatus.FAILED, EnvironmentStatus.CANCELLED}


@dataclass(frozen=True)
class EnvironmentExecutionResult:
    record: EnvironmentRecord
    action: EnvironmentPlanAction | str


EnvironmentCandidateAction = Literal["reuse", "attach", "retry", "missing"]


@dataclass(frozen=True)
class EnvironmentCandidateResult:
    record: EnvironmentRecord | None
    action: EnvironmentCandidateAction


class EnvironmentExecutorClient:
    """Strict client for executor operations in the stdlib-only helper."""

    def check_candidate(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        full_hash: str,
    ) -> EnvironmentCandidateResult:
        payload = self._invoke(
            transport,
            [
                "candidate-check",
                "--base",
                layout.base,
                "--env-id",
                env_id,
                "--full-hash",
                full_hash,
            ],
            operation="candidate-check",
        )
        action = payload.get("action")
        if action not in {"reuse", "attach", "retry", "missing"}:
            raise TransportError("Remote environment candidate check returned an invalid action.")
        raw_record = payload.get("record")
        record = None if raw_record is None else self._record(payload)
        if action != "missing" and record is None:
            raise TransportError("Remote environment candidate check omitted its registry record.")
        if record is not None and (record.env_id != env_id or record.full_hash != full_hash):
            raise TransportError("Remote environment candidate check returned a conflicting identity.")
        return EnvironmentCandidateResult(
            record=record,
            action=action,
        )

    def prepare(
        self,
        transport: Transport,
        layout: RemoteLayout,
        request: EnvironmentBuildRequest,
    ) -> EnvironmentExecutionResult:
        payload = self._invoke(
            transport,
            [
                "prepare-build",
                "--base",
                layout.base,
                "--request-json",
                request.model_dump_json(),
            ],
            operation="prepare-build",
        )
        action = payload.get("action")
        if not isinstance(action, str) or action not in _PREPARE_ACTIONS:
            raise TransportError("Remote environment executor returned an invalid prepare action.")
        return EnvironmentExecutionResult(
            record=self._record(payload),
            action=_PREPARE_ACTIONS[action],
        )

    def build(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        attempt_id: str,
    ) -> EnvironmentExecutionResult:
        payload = self._invoke(
            transport,
            [
                "build",
                "--base",
                layout.base,
                "--env-id",
                env_id,
                "--attempt-id",
                attempt_id,
            ],
            operation="build",
            timeout=7200,
        )
        return EnvironmentExecutionResult(record=self._record(payload), action=str(payload.get("action", "built")))

    def verify_existing(
        self,
        transport: Transport,
        layout: RemoteLayout,
        request: EnvironmentExistingRequest,
    ) -> EnvironmentExecutionResult:
        payload = self._invoke(
            transport,
            [
                "verify-existing",
                "--base",
                layout.base,
                "--request-json",
                request.model_dump_json(),
            ],
            operation="verify-existing",
        )
        action = payload.get("action")
        if action not in {"verify", "reuse"}:
            raise TransportError("Remote existing environment verifier returned an invalid action.")
        return EnvironmentExecutionResult(
            record=self._record(payload),
            action=EnvironmentPlanAction(str(action)),
        )

    def reconcile(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        *,
        heartbeat_timeout: float = 30.0,
    ) -> EnvironmentExecutionResult:
        payload = self._invoke(
            transport,
            [
                "reconcile",
                "--base",
                layout.base,
                "--env-id",
                env_id,
                "--heartbeat-timeout",
                str(heartbeat_timeout),
            ],
            operation="reconcile",
        )
        return EnvironmentExecutionResult(
            record=self._record(payload),
            action=str(payload.get("action", "reconciled")),
        )

    @staticmethod
    def _record(payload: dict[str, object]) -> EnvironmentRecord:
        try:
            return EnvironmentRecord.model_validate(payload.get("record"))
        except (TypeError, ValidationError, ValueError) as exc:
            raise TransportError(
                "Remote environment executor returned an invalid registry record.",
                underlying_cause=exc,
            ) from exc

    @staticmethod
    def _invoke(
        transport: Transport,
        args: list[str],
        *,
        operation: str,
        timeout: float = 300,
    ) -> dict[str, object]:
        result = transport.exec_python(protocol.env_agent_source(), args, timeout=timeout, check=False)
        try:
            payloads = parse_json_lines(result.stdout)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                "Remote environment executor returned malformed JSON.",
                returncode=result.returncode,
                stderr=result.stderr,
                underlying_cause=exc,
            ) from exc
        raw = next(
            (
                item
                for item in reversed(payloads)
                if isinstance(item, dict) and item.get("kind") == protocol.ENV_HELPER_KIND
            ),
            None,
        )
        if (
            raw is None
            or raw.get("schema_version") != 1
            or raw.get("operation") != operation
            or not isinstance(raw.get("ok"), bool)
        ):
            raise TransportError(
                "Remote environment executor produced no valid structured result.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        payload = cast(dict[str, object], raw)
        if payload["ok"] is not True:
            detail = payload.get("error")
            raise TransportError(
                f"Remote environment executor failed: {detail or 'unknown error'}",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        structured_build_failure = operation == "build" and payload.get("action") in {"failed", "cancelled"}
        if result.returncode != 0 and not structured_build_failure:
            raise TransportError(
                "Remote environment executor exited unsuccessfully.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return payload


class EnvironmentPreparationService:
    """Plan, capability-gate, stage once, and start or attach to one build."""

    def __init__(
        self,
        *,
        planning: EnvironmentPlanningService | None = None,
        executor: EnvironmentExecutorClient | None = None,
        cache: EnvironmentCache | None = None,
    ) -> None:
        self._planning = planning or EnvironmentPlanningService(cache=cache)
        self._executor = executor or EnvironmentExecutorClient()
        self._cache = cache

    def prepare(
        self,
        *,
        transport: Transport,
        remote: Remote,
        layout: RemoteLayout,
        project: ProjectConfig,
        project_dir: Path,
        requested_executor: BuildExecutor | None = None,
        rebuild: bool = False,
        wait: bool = True,
        timeout: float = 7200,
        poll_interval: float = 2.0,
    ) -> EnvironmentExecutionResult:
        plan = self._planning.plan_for_prepare(
            transport=transport,
            remote=remote,
            layout=layout,
            project=project,
            project_dir=project_dir,
            requested_executor=requested_executor,
            rebuild=rebuild,
        )
        self._require_preparable(plan)
        force_rebuild = rebuild
        if plan.action in {EnvironmentPlanAction.REUSE, EnvironmentPlanAction.ATTACH} and self._cache is not None:
            candidate = self._executor.check_candidate(transport, layout, plan.env_id, plan.full_hash)
            if candidate.record is not None:
                self._cache.remember_record(remote, candidate.record)
            if candidate.action in {"reuse", "attach"}:
                assert candidate.record is not None
                return EnvironmentExecutionResult(
                    record=candidate.record,
                    action=EnvironmentPlanAction(candidate.action),
                )
            force_rebuild = force_rebuild or (
                plan.backend is EnvBackend.CONDA and plan.action is EnvironmentPlanAction.REUSE
            )
        elif plan.action in {EnvironmentPlanAction.REUSE, EnvironmentPlanAction.ATTACH}:
            if plan.registry_record is None:
                raise TransportError("Environment plan omitted the registry record for a reusable environment.")
            return EnvironmentExecutionResult(record=plan.registry_record, action=plan.action)
        if plan.action is EnvironmentPlanAction.VERIFY or plan.backend is EnvBackend.EXISTING:
            result = self._executor.verify_existing(transport, layout, self._existing_request(plan))
            self._remember(remote, result.record)
            return result
        request = self._request(plan, rebuild=force_rebuild)
        self._upload_inbox(transport, layout, plan, request)
        result = self._executor.prepare(transport, layout, request)
        self._remember(remote, result.record)
        if not wait:
            return result
        if result.record.status in _TERMINAL_ENV_STATES:
            return self._require_success(result)
        deadline = time.monotonic() + timeout
        record = result.record
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            reconciled = self._executor.reconcile(transport, layout, record.env_id)
            record = reconciled.record
            self._remember(remote, record)
            if record.status in _TERMINAL_ENV_STATES:
                return self._require_success(EnvironmentExecutionResult(record=record, action=result.action))
        raise UserError(
            StructuredError(
                code="env_build_timeout",
                summary=f"Timed out waiting for environment {record.env_id}.",
                operation="env.prepare",
                phase="reconcile",
                retryable=True,
                remediation=f"Run `slurmdeck env status {record.env_id}`; the build was not resubmitted.",
                context={"env_id": record.env_id, "timeout_seconds": timeout},
            )
        )

    def _remember(self, remote: Remote, record: EnvironmentRecord) -> None:
        if self._cache is not None:
            self._cache.remember_record(remote, record)

    @staticmethod
    def _require_success(result: EnvironmentExecutionResult) -> EnvironmentExecutionResult:
        if result.record.status is EnvironmentStatus.READY:
            return result
        summary = result.record.last_error.summary if result.record.last_error else result.record.status.value
        raise UserError(
            StructuredError(
                code="environment_prepare_failed",
                summary=f"Environment {result.record.env_id} ended in {result.record.status.value}: {summary}",
                operation="env.prepare",
                phase="environment",
                retryable=result.record.status is EnvironmentStatus.FAILED,
                remediation=f"Inspect `slurmdeck env logs {result.record.env_id}` before preparing again.",
                context={"env_id": result.record.env_id, "status": result.record.status.value},
            )
        )

    @staticmethod
    def _require_preparable(plan: EnvironmentPlan) -> None:
        if plan.complete:
            return
        detail = []
        if plan.missing:
            detail.append("Missing: " + ", ".join(plan.missing))
        if plan.conflicts:
            detail.append("Conflicts: " + "; ".join(plan.conflicts))
        primary = (plan.conflicts or plan.missing or ["incomplete environment contract"])[0]
        raise UserError(
            StructuredError(
                code="environment_plan_incomplete",
                summary=f"The environment plan is not safe to execute: {primary}.",
                detail="\n".join(detail),
                operation="env.prepare",
                phase="validate",
                retryable=False,
                remediation="Correct the project environment or cluster profile, then run `slurmdeck env plan`.",
                context={"missing": plan.missing, "conflicts": plan.conflicts},
            )
        )

    @staticmethod
    def _existing_request(plan: EnvironmentPlan) -> EnvironmentExistingRequest:
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return EnvironmentExistingRequest(
            env_id=plan.env_id,
            full_hash=plan.full_hash,
            prefix=plan.prefix,
            created_at=created_at,
            modules=plan.modules,
            module_initialization=plan.module_initialization,
            smoke_test=plan.smoke_test,
            provenance=EnvironmentProvenance(canonical_spec_hash=plan.full_hash),
        )

    @staticmethod
    def _request(plan: EnvironmentPlan, *, rebuild: bool) -> EnvironmentBuildRequest:
        if (
            plan.executor is None
            or plan.environment_file is None
            or plan.environment_file_hash is None
            or plan.channel_priority is None
            or plan.solver is None
            or plan.platform is None
            or plan.resolved_resources is None
            or plan.generation_root is None
        ):
            raise TransportError("Managed environment plan omitted executor inputs.")
        suffix = uuid.uuid4().hex[:12]
        attempt_id = f"attempt-{suffix}"
        generation_id = f"gen-{suffix}"
        prefix = f"{plan.generation_root}/{generation_id}"
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        provenance = EnvironmentProvenance(
            canonical_spec_hash=plan.full_hash,
            environment_file_hash=plan.environment_file_hash,
            channels=plan.channels,
            channel_priority=plan.channel_priority.value,
            solver=plan.solver.value,
            platform=plan.platform,
        )
        return EnvironmentBuildRequest(
            env_id=plan.env_id,
            full_hash=plan.full_hash,
            rebuild=rebuild,
            executor=plan.executor,
            attempt_id=attempt_id,
            generation_id=generation_id,
            prefix=prefix,
            created_at=created_at,
            environment_file_name="environment.yml",
            isolated_environment_file_name="isolated-environment.yml",
            environment_file_hash=plan.environment_file_hash,
            modules=plan.modules,
            module_initialization=plan.module_initialization,
            post_install=plan.post_install,
            smoke_test=plan.smoke_test,
            channels=plan.channels,
            channel_priority=plan.channel_priority,
            solver=plan.solver,
            platform=plan.platform,
            conda_executable=plan.conda_executable,
            resolved_resources=plan.resolved_resources,
            provenance=provenance,
        )

    @staticmethod
    def _upload_inbox(
        transport: Transport,
        layout: RemoteLayout,
        plan: EnvironmentPlan,
        request: EnvironmentBuildRequest,
    ) -> None:
        assert plan.environment_file is not None
        with tempfile.TemporaryDirectory(prefix="slurmdeck-env-") as temporary:
            root = Path(temporary)
            source = Path(plan.environment_file)
            shutil.copy2(source, root / request.environment_file_name)
            document = yaml.safe_load(source.read_bytes())
            if not isinstance(document, dict):
                raise UserError(f"{source}: environment file must contain a mapping")
            raw_channels = document.get("channels")
            if not isinstance(raw_channels, list):
                raise UserError(f"{source}: channels must be a list")
            if "nodefaults" not in raw_channels:
                document["channels"] = [*raw_channels, "nodefaults"]
            (root / request.isolated_environment_file_name).write_text(
                yaml.safe_dump(document, sort_keys=False),
                encoding="utf-8",
            )
            declared_channels = [channel for channel in request.channels if channel != "nodefaults"]
            condarc = {
                "channels": declared_channels,
                "allowlist_channels": declared_channels,
                "channel_priority": request.channel_priority.value,
                "solver": request.solver.value,
                "show_channel_urls": True,
                "auto_activate_base": False,
                "create_default_packages": [],
            }
            if "defaults" not in declared_channels:
                # Conda merges the installation prefix's .condarc even when
                # CONDARC points at this attempt-local file.  Remap the
                # inherited ``defaults`` multichannel to the project's
                # declared channels so a site Anaconda install cannot contact
                # repo.anaconda.com behind ``nodefaults`` (or trigger its ToS
                # plugin before the environment file is evaluated).
                condarc["default_channels"] = declared_channels
            (root / ".condarc").write_text(yaml.safe_dump(condarc, sort_keys=False), encoding="utf-8")
            (root / protocol.ENV_AGENT_FILE).write_text(protocol.env_agent_source(), encoding="utf-8")
            (root / "request.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
            transport.upload(
                f"{root}/",
                f"{layout.env_inbox_dir(request.attempt_id)}/",
                delete=True,
                timeout=1800,
            )
