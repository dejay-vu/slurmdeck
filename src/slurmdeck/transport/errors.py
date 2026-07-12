"""Transport error types."""

from __future__ import annotations

from slurmdeck.structured_errors import StructuredError


class TransportError(Exception):
    """A remote command or transfer failed."""

    exit_code = 3

    def __init__(
        self,
        message: str,
        *,
        command: str = "",
        returncode: int | None = None,
        stderr: str = "",
        underlying_cause: BaseException | None = None,
    ) -> None:
        detail = message
        tail = stderr.strip().splitlines()[-3:] if stderr.strip() else []
        if tail:
            detail += "\n  " + "\n  ".join(tail)
        super().__init__(detail)
        self.error = StructuredError(
            code="transport_error",
            summary=detail,
            retryable=True,
            context={"command": command, "returncode": returncode},
            underlying_cause=underlying_cause,
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class ConnectError(TransportError):
    """Could not establish the SSH connection."""


class RemoteTimeout(TransportError):
    """A remote command exceeded its time budget."""
