"""Shared error types.

``UserError`` is the single error type surfaced to users by both the CLI and
the TUI. Every layer may raise it; ``cli.main`` converts it to an exit code and
a formatted message, so raising a ``UserError`` with a good ``hint`` is the
way to make an error actionable.
"""

from __future__ import annotations

from slurmdeck.structured_errors import StructuredError

__all__ = ["SchemaVersionError", "StructuredError", "UserError"]


class UserError(Exception):
    """An error caused by user input or user-visible state, with a remedy hint."""

    exit_code = 1

    def __init__(self, message: str | StructuredError, *, hint: str | None = None) -> None:
        if isinstance(message, StructuredError):
            structured = message
            if hint is not None:
                structured = structured.model_copy(update={"remediation": hint})
        else:
            structured = StructuredError(code="user_error", summary=message, remediation=hint or "")
        super().__init__(structured.summary)
        self.error = structured
        self.message = structured.summary
        self.hint = structured.remediation or None

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class SchemaVersionError(UserError):
    """State written by a different slurmdeck version than this one supports."""

    def __init__(self, what: str, found: object, supported: int) -> None:
        super().__init__(
            f"{what} has schema version {found!r}, but this slurmdeck supports version {supported}.",
            hint="Upgrade slurmdeck (pip install -U slurmdeck), or remove the stale state if it is disposable.",
        )
