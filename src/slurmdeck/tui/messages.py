"""Messages the DeckController posts to the app (thread-safe via post_message)."""

from __future__ import annotations

from textual.message import Message

from slurmdeck.operations import OperationEvent


class RefreshStarted(Message):
    """A background status refresh began."""


class RefreshFinished(Message):
    """A background status refresh ended (successfully or not)."""

    def __init__(
        self,
        *,
        ok: bool,
        changed: int = 0,
        error: str = "",
        transport_error: bool = False,
        stale: bool = False,
    ) -> None:
        self.ok = ok
        self.changed = changed
        self.error = error
        self.transport_error = transport_error
        self.stale = stale
        super().__init__()


class OperationStarted(Message):
    """A guarded mutation (submit/cancel/retry/pull/...) began."""

    def __init__(self, operation_id: str, label: str, *, mutation: bool, started_at: float) -> None:
        self.operation_id = operation_id
        self.label = label
        self.mutation = mutation
        self.started_at = started_at
        super().__init__()


class OperationProgressed(Message):
    """Progress detail reported by the running operation's service."""

    def __init__(self, operation_id: str, event: OperationEvent) -> None:
        self.operation_id = operation_id
        self.event = event
        super().__init__()


class OperationFinished(Message):
    """The running operation ended (successfully or not)."""

    def __init__(
        self,
        operation_id: str,
        label: str,
        *,
        ok: bool,
        message: str = "",
        error: str = "",
        transport_error: bool = False,
        elapsed: float = 0.0,
    ) -> None:
        self.operation_id = operation_id
        self.label = label
        self.ok = ok
        self.message = message
        self.error = error
        self.transport_error = transport_error
        self.elapsed = elapsed
        super().__init__()


class DataRefreshed(Message):
    """Local state changed; screens should re-read SQLite and re-render."""
