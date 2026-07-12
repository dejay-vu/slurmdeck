"""Typed operation events independent from the eager domain-model package."""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Mapping
from copy import deepcopy
from enum import StrEnum
from typing import Any, Never, Self

from pydantic import BaseModel, ConfigDict, Field, PydanticDeprecatedSince20, field_serializer, field_validator


class _ImmutableDict(dict[str, int]):
    """A dict-compatible value whose public mutation paths all fail."""

    __slots__ = ()

    @staticmethod
    def _immutable() -> Never:
        raise TypeError("result_counts is immutable")

    def __setitem__(self, key: str, value: int) -> Never:
        self._immutable()

    def __delitem__(self, key: str) -> Never:
        self._immutable()

    def clear(self) -> Never:
        self._immutable()

    def pop(self, key: str, default: object = None) -> Never:
        self._immutable()

    def popitem(self) -> Never:
        self._immutable()

    def setdefault(self, key: str, default: int | None = None) -> Never:
        self._immutable()

    def update(self, *args: object, **kwargs: object) -> Never:
        self._immutable()

    def __ior__(self, value: object) -> Never:
        self._immutable()

    def copy(self) -> _ImmutableDict:
        return type(self)(self)

    def __copy__(self) -> _ImmutableDict:
        return self.copy()

    def __deepcopy__(self, memo: dict[int, Any]) -> _ImmutableDict:
        return type(self)(deepcopy(dict(self), memo))

    def __reduce__(self) -> tuple[type[_ImmutableDict], tuple[dict[str, int]]]:
        return type(self), (dict(self),)

    def __or__(self, value: dict[str, int]) -> _ImmutableDict:  # type: ignore[override]
        return type(self)(dict.__or__(self, value))

    def __ror__(self, value: dict[str, int]) -> _ImmutableDict:  # type: ignore[override]
        return type(self)(dict.__or__(value, self))


class OperationPhase(StrEnum):
    CONNECT = "connect"
    PROBE = "probe"
    VALIDATE = "validate"
    SNAPSHOT = "snapshot"
    UPLOAD = "upload"
    ENVIRONMENT = "environment"
    SUBMIT = "submit"
    RECONCILE = "reconcile"
    REFRESH = "refresh"
    DOWNLOAD = "download"
    CLEANUP = "cleanup"


class OperationStatus(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"


class OperationEvent(BaseModel):
    """One immutable observation from a running operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    phase: OperationPhase
    status: OperationStatus
    current: int | None = None
    total: int | None = None
    elapsed: float = 0.0
    message: str = ""
    result_counts: dict[str, int] = Field(default_factory=dict, validate_default=True)

    @field_validator("result_counts", mode="after")
    @classmethod
    def _freeze_result_counts(cls, value: dict[str, int]) -> dict[str, int]:
        return _ImmutableDict(value)

    @field_serializer("result_counts")
    def _serialize_result_counts(self, value: dict[str, int]) -> dict[str, int]:
        return dict(value)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        payload = self.model_dump(round_trip=True)
        if deep:
            payload = deepcopy(payload)
        if update:
            payload.update(deepcopy(dict(update)) if deep else update)
        return type(self).model_validate(payload)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        warnings.warn(
            "The `copy` method is deprecated; use `model_copy` instead.",
            category=PydanticDeprecatedSince20,
            stacklevel=2,
        )
        payload = self.model_dump(include=include, exclude=exclude, round_trip=True)
        if deep:
            payload = deepcopy(payload)
        if update:
            payload.update(deepcopy(update) if deep else update)
        return type(self).model_validate(payload)


OperationSink = Callable[[OperationEvent], None]


def noop_operation_sink(_event: OperationEvent) -> None:
    """Discard an operation event."""


class OperationReporter:
    """Create operation events with elapsed time from one monotonic origin."""

    def __init__(
        self,
        operation: str,
        sink: OperationSink = noop_operation_sink,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.operation = operation
        self._sink = sink
        self._clock = clock or time.monotonic
        self._started_at = self._clock()
        self._last_elapsed = 0.0
        self._last_phase: OperationPhase | None = None

    @property
    def last_phase(self) -> OperationPhase | None:
        return self._last_phase

    def emit(
        self,
        phase: OperationPhase,
        status: OperationStatus,
        *,
        current: int | None = None,
        total: int | None = None,
        message: str = "",
        result_counts: dict[str, int] | None = None,
    ) -> OperationEvent:
        self._last_elapsed = max(self._last_elapsed, self._clock() - self._started_at)
        self._last_phase = phase
        event = OperationEvent(
            operation=self.operation,
            phase=phase,
            status=status,
            current=current,
            total=total,
            elapsed=self._last_elapsed,
            message=message,
            result_counts=result_counts or {},
        )
        self._sink(event)
        return event

    def started(self, phase: OperationPhase, *, message: str = "") -> OperationEvent:
        return self.emit(phase, OperationStatus.STARTED, message=message)

    def progress(
        self,
        phase: OperationPhase,
        *,
        current: int | None = None,
        total: int | None = None,
        message: str = "",
    ) -> OperationEvent:
        return self.emit(phase, OperationStatus.PROGRESS, current=current, total=total, message=message)

    def completed(
        self,
        phase: OperationPhase,
        *,
        message: str = "",
        result_counts: dict[str, int] | None = None,
    ) -> OperationEvent:
        return self.emit(
            phase,
            OperationStatus.COMPLETED,
            message=message,
            result_counts=result_counts,
        )

    def failed(self, phase: OperationPhase | None = None, *, message: str = "") -> OperationEvent:
        failed_phase = phase or self._last_phase
        if failed_phase is None:
            raise ValueError("cannot report failure before an operation phase has started")
        return self.emit(failed_phase, OperationStatus.FAILED, message=message)
