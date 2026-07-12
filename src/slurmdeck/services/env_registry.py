"""Typed client for the first-format remote environment registry helper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from pydantic import ValidationError

from slurmdeck.agent import protocol
from slurmdeck.models.env import EnvironmentRecord
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import Transport, TransportError, parse_json_lines

RegistryPrepareAction = Literal["create", "reuse"]


@dataclass(frozen=True)
class EnvRegistryPrepareResult:
    record: EnvironmentRecord
    action: RegistryPrepareAction


class EnvRegistryClient:
    """Invoke and strictly validate the stdlib-only registry protocol."""

    def inspect(self, transport: Transport, layout: RemoteLayout) -> list[EnvironmentRecord]:
        payload = self._invoke(transport, ["inspect", "--base", layout.base], operation="inspect")
        invalid = payload.get("invalid")
        if not isinstance(invalid, list):
            raise TransportError("Remote environment registry returned invalid corruption metadata.")
        if invalid:
            names = [str(item.get("name", "unknown")) for item in invalid if isinstance(item, dict)]
            suffix = ", ".join(names) if names else "unknown record"
            raise TransportError(f"Remote environment registry contains corrupt records: {suffix}.")

        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise TransportError("Remote environment registry returned no record list.")
        try:
            return [EnvironmentRecord.model_validate(item) for item in raw_records]
        except (TypeError, ValidationError, ValueError) as exc:
            raise TransportError(
                "Remote environment registry returned an invalid record.",
                underlying_cause=exc,
            ) from exc

    def prepare(
        self,
        transport: Transport,
        layout: RemoteLayout,
        record: EnvironmentRecord,
    ) -> EnvRegistryPrepareResult:
        payload = self._invoke(
            transport,
            [
                "prepare",
                "--base",
                layout.base,
                "--record-json",
                record.model_dump_json(),
            ],
            operation="prepare",
        )
        action = payload.get("action")
        if action not in {"create", "reuse"}:
            raise TransportError("Remote environment registry returned an invalid prepare action.")
        try:
            stored = EnvironmentRecord.model_validate(payload.get("record"))
        except (TypeError, ValidationError, ValueError) as exc:
            raise TransportError(
                "Remote environment registry returned an invalid prepared record.",
                underlying_cause=exc,
            ) from exc
        if stored.env_id != record.env_id or stored.full_hash != record.full_hash:
            raise TransportError("Remote environment registry returned a conflicting identity.")
        return EnvRegistryPrepareResult(record=stored, action=action)

    @staticmethod
    def _invoke(
        transport: Transport,
        args: list[str],
        *,
        operation: str,
    ) -> dict[str, object]:
        result = transport.exec_python(protocol.env_agent_source(), args, timeout=300, check=False)
        try:
            payloads = parse_json_lines(result.stdout)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                "Remote environment registry helper returned malformed JSON.",
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
                "Remote environment registry helper produced no valid structured result.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        payload = cast(dict[str, object], raw)
        if payload["ok"] is not True:
            error = payload.get("error")
            detail = error if isinstance(error, str) and error else "unknown registry error"
            raise TransportError(
                f"Remote environment registry operation failed: {detail}",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        if result.returncode != 0:
            raise TransportError(
                "Remote environment registry helper exited unsuccessfully.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return payload
