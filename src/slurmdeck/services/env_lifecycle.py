"""Environment views and lifecycle operations over the first-format registry."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.common import validate_name
from slurmdeck.models.env import EnvBuildAttempt, EnvironmentRecord, EnvironmentStatus, EnvironmentView
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import StreamHandle, Transport, TransportError, parse_json_lines


@dataclass(frozen=True)
class EnvironmentScan:
    views: tuple[EnvironmentView, ...]
    observed_at: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class EnvironmentLog:
    env_id: str
    attempt_id: str
    stream: str
    path: str
    text: str
    handle: StreamHandle | None = None


@dataclass(frozen=True)
class EnvironmentRemoveResult:
    record: EnvironmentRecord
    references: tuple[str, ...]
    external_unregistered: bool
    trash_path: str


@dataclass(frozen=True)
class EnvironmentGcCandidate:
    kind: str
    path: str
    env_id: str
    reason: str
    size_bytes: int


@dataclass(frozen=True)
class EnvironmentGcReport:
    dry_run: bool
    candidates: tuple[EnvironmentGcCandidate, ...]
    deleted: tuple[str, ...]
    failed: tuple[str, ...]


class EnvironmentLifecycleService:
    def scan(
        self,
        transport: Transport,
        layout: RemoteLayout,
        *,
        desired_env_id: str | None = None,
    ) -> EnvironmentScan:
        payload = self._invoke(transport, ["scan", "--base", layout.base], operation="scan")
        invalid = payload.get("invalid")
        if not isinstance(invalid, list):
            raise TransportError("Remote environment scan returned invalid corruption metadata.")
        if invalid:
            names = [str(item.get("name", "unknown")) for item in invalid if isinstance(item, dict)]
            raise TransportError("Remote environment registry contains corrupt records: " + ", ".join(names))
        raw_records = payload.get("records")
        raw_references = payload.get("references")
        if not isinstance(raw_records, list) or not isinstance(raw_references, dict):
            raise TransportError("Remote environment scan returned an invalid registry view.")
        try:
            records = [EnvironmentRecord.model_validate(item) for item in raw_records]
        except (TypeError, ValidationError, ValueError) as exc:
            raise TransportError("Remote environment scan returned an invalid record.", underlying_cause=exc) from exc
        views = []
        for record in records:
            raw = raw_references.get(record.env_id, [])
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise TransportError("Remote environment scan returned invalid run references.")
            views.append(
                EnvironmentView(
                    record=record,
                    references=raw,
                    desired_by_project=record.env_id == desired_env_id,
                )
            )
        raw_warnings = payload.get("scheduler_errors", [])
        if not isinstance(raw_warnings, list) or not all(isinstance(item, str) for item in raw_warnings):
            raise TransportError("Remote environment scan returned invalid scheduler warnings.")
        observed_at = payload.get("observed_at")
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
            raise TransportError("Remote environment scan returned an invalid observation time.")
        views.sort(key=lambda view: (not view.desired_by_project, view.record.env_id))
        return EnvironmentScan(views=tuple(views), observed_at=float(observed_at), warnings=tuple(raw_warnings))

    def list(
        self,
        transport: Transport,
        layout: RemoteLayout,
        *,
        desired_env_id: str | None = None,
    ) -> list[EnvironmentView]:
        return [
            view
            for view in self.scan(transport, layout, desired_env_id=desired_env_id).views
            if view.record.status is not EnvironmentStatus.REMOVED
        ]

    def show(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        *,
        desired_env_id: str | None = None,
    ) -> EnvironmentView:
        validate_name(env_id, what="environment id")
        scan = self.scan(transport, layout, desired_env_id=desired_env_id)
        view = next((item for item in scan.views if item.record.env_id == env_id), None)
        if view is None:
            raise UserError(f"Environment {env_id!r} was not found in the remote registry.")
        return view

    def status(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        *,
        desired_env_id: str | None = None,
    ) -> EnvironmentView:
        return self.show(transport, layout, env_id, desired_env_id=desired_env_id)

    def logs(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        *,
        lines: int = 100,
        stream: str | None = None,
    ) -> EnvironmentLog:
        if lines < 1:
            raise UserError("Log line count must be at least 1.")
        view = self.status(transport, layout, env_id)
        attempt = self._latest_attempt(view.record)
        selected = stream or (
            "stderr" if view.record.status.value in {"FAILED", "CANCELLED", "BUILD_UNKNOWN"} else "stdout"
        )
        if selected not in {"stdout", "stderr"}:
            raise UserError("Environment log stream must be 'stdout' or 'stderr'.")
        path = attempt.stderr_path if selected == "stderr" else attempt.stdout_path
        result = transport.exec(f"tail -n {int(lines)} {shlex.quote(path)}", check=False, retries=1)
        text = result.stdout
        if stream is None and selected == "stderr" and not text.strip():
            selected = "stdout"
            path = attempt.stdout_path
            result = transport.exec(f"tail -n {int(lines)} {shlex.quote(path)}", check=False, retries=1)
            text = result.stdout
        if result.returncode != 0:
            raise UserError(f"Environment log is unavailable: {path}")
        return EnvironmentLog(
            env_id=view.record.env_id,
            attempt_id=attempt.attempt_id,
            stream=selected,
            path=path,
            text=text,
        )

    def follow_logs(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        *,
        stream: str | None = None,
        on_line: Callable[[str], None],
    ) -> EnvironmentLog:
        view = self.status(transport, layout, env_id)
        attempt = self._latest_attempt(view.record)
        selected = stream or (
            "stderr" if view.record.status.value in {"FAILED", "CANCELLED", "BUILD_UNKNOWN"} else "stdout"
        )
        if selected not in {"stdout", "stderr"}:
            raise UserError("Environment log stream must be 'stdout' or 'stderr'.")
        path = attempt.stderr_path if selected == "stderr" else attempt.stdout_path
        handle = transport.stream(f"tail -n 50 -F {shlex.quote(path)}", on_line=on_line)
        return EnvironmentLog(
            env_id=view.record.env_id,
            attempt_id=attempt.attempt_id,
            stream=selected,
            path=path,
            text="",
            handle=handle,
        )

    def cancel(self, transport: Transport, layout: RemoteLayout, env_id: str) -> EnvironmentRecord:
        validate_name(env_id, what="environment id")
        payload = self._invoke(
            transport,
            ["cancel", "--base", layout.base, "--env-id", env_id],
            operation="cancel",
            user_errors=True,
        )
        return self._record(payload)

    def remove(
        self,
        transport: Transport,
        layout: RemoteLayout,
        env_id: str,
        *,
        force: bool = False,
    ) -> EnvironmentRemoveResult:
        validate_name(env_id, what="environment id")
        args = ["remove", "--base", layout.base, "--env-id", env_id]
        if force:
            args.append("--force")
        payload = self._invoke(transport, args, operation="remove", user_errors=True)
        references = self._strings(payload, "references")
        external = payload.get("external_unregistered")
        trash_path = payload.get("trash_path")
        if not isinstance(external, bool) or not isinstance(trash_path, str):
            raise TransportError("Remote environment removal returned an invalid result.")
        return EnvironmentRemoveResult(
            record=self._record(payload),
            references=references,
            external_unregistered=external,
            trash_path=trash_path,
        )

    def gc(
        self,
        transport: Transport,
        layout: RemoteLayout,
        *,
        delete: bool = False,
    ) -> EnvironmentGcReport:
        args = ["gc", "--base", layout.base]
        if delete:
            args.append("--delete")
        payload = self._invoke(transport, args, operation="gc", user_errors=True)
        raw = payload.get("candidates")
        if not isinstance(raw, list):
            raise TransportError("Remote environment GC returned invalid candidates.")
        candidates = []
        for item in raw:
            if not isinstance(item, dict):
                raise TransportError("Remote environment GC returned an invalid candidate.")
            kind = item.get("kind")
            path = item.get("path")
            env_id = item.get("env_id")
            reason = item.get("reason")
            size_bytes = item.get("size_bytes")
            if (
                not isinstance(kind, str)
                or not isinstance(path, str)
                or not isinstance(env_id, str)
                or not isinstance(reason, str)
                or isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes < 0
            ):
                raise TransportError("Remote environment GC returned an invalid candidate contract.")
            candidates.append(EnvironmentGcCandidate(kind, path, env_id, reason, size_bytes))
        dry_run = payload.get("dry_run")
        if not isinstance(dry_run, bool):
            raise TransportError("Remote environment GC returned an invalid dry-run marker.")
        return EnvironmentGcReport(
            dry_run=dry_run,
            candidates=tuple(candidates),
            deleted=self._strings(payload, "deleted"),
            failed=self._strings(payload, "failed"),
        )

    @staticmethod
    def _latest_attempt(record: EnvironmentRecord) -> EnvBuildAttempt:
        if record.current_attempt:
            active = next((item for item in record.attempts if item.attempt_id == record.current_attempt), None)
            if active is not None:
                return active
        if not record.attempts:
            raise UserError(f"Environment {record.env_id!r} has no build attempt or logs.")
        return record.attempts[-1]

    @staticmethod
    def _record(payload: dict[str, object]) -> EnvironmentRecord:
        try:
            return EnvironmentRecord.model_validate(payload.get("record"))
        except (TypeError, ValidationError, ValueError) as exc:
            raise TransportError(
                "Remote environment operation returned an invalid record.", underlying_cause=exc
            ) from exc

    @staticmethod
    def _strings(payload: dict[str, object], key: str) -> tuple[str, ...]:
        raw = payload.get(key)
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise TransportError(f"Remote environment operation returned invalid {key} data.")
        return tuple(raw)

    @staticmethod
    def _invoke(
        transport: Transport,
        args: Sequence[str],
        *,
        operation: str,
        user_errors: bool = False,
    ) -> dict[str, object]:
        result = transport.exec_python(protocol.env_agent_source(), args, timeout=300, check=False)
        try:
            payloads = parse_json_lines(result.stdout)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                "Remote environment helper returned malformed JSON.",
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
                "Remote environment helper produced no valid structured result.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        payload = cast(dict[str, object], raw)
        if payload["ok"] is not True:
            detail = payload.get("error")
            message = str(detail or "unknown environment operation error")
            if user_errors:
                raise UserError(message)
            raise TransportError(message, returncode=result.returncode, stderr=result.stderr)
        if result.returncode != 0:
            raise TransportError(
                "Remote environment helper exited unsuccessfully.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return payload
