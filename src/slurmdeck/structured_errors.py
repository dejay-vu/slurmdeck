"""Validated error payloads, isolated from the exception import boundary."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from slurmdeck.operations import OperationPhase

ErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]*$")]


class StructuredError(BaseModel):
    """Serializable user-facing error data with an opt-in debug cause."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    code: ErrorCode
    summary: str
    detail: str = ""
    operation: str = ""
    phase: OperationPhase | None = None
    retryable: bool = False
    remediation: str = ""
    context: dict[str, object] = Field(default_factory=dict)
    underlying_cause: BaseException | None = Field(default=None, exclude=True)

    def model_dump_debug(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        payload["underlying_cause"] = repr(self.underlying_cause) if self.underlying_cause is not None else None
        return payload

    def model_dump_json_debug(self, *, indent: int | None = None) -> str:
        return json.dumps(self.model_dump_debug(), indent=indent)
