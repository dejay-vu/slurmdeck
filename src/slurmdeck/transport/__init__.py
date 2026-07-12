"""Transport abstraction: how slurmdeck talks to a remote host.

Everything above this layer (services, agent invocation) is written against
the ``Transport`` protocol, so tests substitute an in-process fake and never
spawn subprocesses.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from slurmdeck.transport.errors import ConnectError, RemoteTimeout, TransportError

#: Line prefix used by ``exec_json`` scripts to mark their JSON payload, so
#: shell/motd noise on stdout can never corrupt structured results.
JSON_PREFIX = "SLURMDECK_JSON\t"


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SyncStats:
    transferred: bool = True
    matched_files: int = 0
    transferred_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    bytes_transferred: int = 0
    failed_paths: tuple[str, ...] = ()
    returncode: int = 0
    raw_output: str = ""
    raw_error: str = ""


@dataclass
class StreamHandle:
    """A cancellable line stream (e.g. ``tail -f``)."""

    _close: Callable[[], None]
    _wait: Callable[[], int] = field(default=lambda: 0)

    def close(self) -> None:
        self._close()

    def wait(self) -> int:
        return self._wait()


class Transport(Protocol):
    def exec(
        self,
        command: str,
        *,
        input_text: str | None = None,
        timeout: float = 60.0,
        check: bool = True,
        retries: int = 0,
    ) -> ExecResult: ...

    def exec_python(
        self,
        script: str,
        args: Sequence[str] = (),
        *,
        timeout: float = 60.0,
        check: bool = True,
    ) -> ExecResult: ...

    def exec_json(self, script: str, args: Sequence[str] = (), *, timeout: float = 60.0) -> Any: ...

    def upload(
        self,
        source: str,
        dest: str,
        *,
        delete: bool = False,
        filters: Sequence[str] = (),
        timeout: float = 600.0,
        files_from: Sequence[str] | None = None,
    ) -> SyncStats: ...

    def download(
        self,
        source: str,
        dest: str,
        *,
        filters: Sequence[str] = (),
        timeout: float = 600.0,
    ) -> SyncStats: ...

    def stream(self, command: str, *, on_line: Callable[[str], None]) -> StreamHandle: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def alive(self) -> bool: ...


def parse_json_lines(stdout: str) -> list[Any]:
    """Extract all ``JSON_PREFIX``-marked payloads from command output."""
    import json

    payloads = []
    for line in stdout.splitlines():
        if line.startswith(JSON_PREFIX):
            payloads.append(json.loads(line[len(JSON_PREFIX) :]))
    return payloads


__all__ = [
    "JSON_PREFIX",
    "ConnectError",
    "ExecResult",
    "RemoteTimeout",
    "StreamHandle",
    "SyncStats",
    "Transport",
    "TransportError",
    "parse_json_lines",
]
