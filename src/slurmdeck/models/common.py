"""Shared model primitives: names, states, and base model settings."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

#: Names for remotes, runs, envs, and tasks: must be safe as a single path
#: component on both the local and remote filesystem.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"

NameStr = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]

_NAME_RE = re.compile(NAME_PATTERN)


def validate_name(value: str, *, what: str = "name") -> str:
    from slurmdeck.errors import UserError

    if not _NAME_RE.fullmatch(value) or value in {".", ".."}:
        raise UserError(
            f"Invalid {what}: {value!r}.",
            hint="Use letters, digits, '.', '_' or '-'; must start with a letter or digit (max 64 chars).",
        )
    return value


def safe_name(text: str, *, fallback: str = "task") -> str:
    """Coerce arbitrary text into a valid name (for generated task names)."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-._")
    cleaned = cleaned[:64].rstrip("-._")
    return cleaned if _NAME_RE.fullmatch(cleaned) else fallback


class StrictModel(BaseModel):
    """Base model: unknown keys are errors everywhere (typos never pass silently)."""

    model_config = ConfigDict(extra="forbid")


class RunState(StrEnum):
    """Lifecycle of a run row in the local database."""

    PLANNED = "planned"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    SUBMIT_FAILED = "submit_failed"
    SUBMIT_UNKNOWN = "submit_unknown"
    CANCELLED = "cancelled"
    TERMINAL = "terminal"  # all tasks reached a terminal state


class TaskState(StrEnum):
    """Task states written by the remote agent (artifact truth)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    KILLED = "KILLED"
    UNKNOWN = "UNKNOWN"
