"""SSH/rsync transport using the system binaries and user-managed auth.

Connection reuse is via OpenSSH ControlMaster multiplexing. Every call has a
timeout; idempotent callers may request one retry on ssh's transport failure
code (255).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from slurmdeck.models.remote import HostKeyPolicy, Remote
from slurmdeck.transport import JSON_PREFIX, ExecResult, StreamHandle, SyncStats
from slurmdeck.transport.errors import ConnectError, RemoteTimeout, TransportError

_SSH_TRANSPORT_RC = 255
_RSYNC_INTEGER = r"([0-9][0-9,]*)"
_RSYNC_FAILED_PATH = re.compile(r"rsync:.*?[\"']([^\"']+)[\"']", re.IGNORECASE)

#: Loader and OpenSSL overrides that Python environments (conda in particular)
#: commonly export. System ssh/rsync must load *system* libraries — e.g. a
#: conda env's ``LD_LIBRARY_PATH`` pointing at its own libcrypto makes OpenSSH
#: abort with "OpenSSL version mismatch. Built against X, you have Y".
_TRANSPORT_ENV_BLOCKLIST = (
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "OPENSSL_CONF",
    "OPENSSL_MODULES",
)


def _stat_value(output: str, label: str) -> int:
    match = re.search(rf"^{re.escape(label)}:\s*{_RSYNC_INTEGER}", output, re.MULTILINE)
    return int(match.group(1).replace(",", "")) if match is not None else 0


def parse_rsync_stats(stdout: str, stderr: str = "", *, returncode: int = 0) -> SyncStats:
    """Parse rsync's stable English stats plus partial-transfer paths."""
    regular = re.search(r"^Number of files:.*?\(.*?reg:\s*([0-9][0-9,]*)", stdout, re.MULTILINE)
    matched = int(regular.group(1).replace(",", "")) if regular is not None else 0
    transferred = _stat_value(stdout, "Number of regular files transferred")
    failed_paths = tuple(dict.fromkeys(_RSYNC_FAILED_PATH.findall(stderr)))
    failed = len(failed_paths)
    if returncode in {23, 24} and failed == 0:
        failed = 1
    return SyncStats(
        transferred=transferred > 0,
        matched_files=matched,
        transferred_files=transferred,
        skipped_files=max(0, matched - transferred - failed),
        failed_files=failed,
        bytes_transferred=_stat_value(stdout, "Total transferred file size"),
        failed_paths=failed_paths,
        returncode=returncode,
        raw_output=stdout,
        raw_error=stderr,
    )


def clean_child_env() -> dict[str, str]:
    """Return the environment safe for spawning system ssh/rsync binaries."""
    return {key: value for key, value in os.environ.items() if key not in _TRANSPORT_ENV_BLOCKLIST}


def _stdin_kwargs(input_text: str | None) -> dict[str, Any]:
    """Never inherit the parent's stdin: under the TUI, a child ssh/rsync
    reading the terminal steals keystrokes from the application."""
    if input_text is None:
        return {"stdin": subprocess.DEVNULL}
    return {"input": input_text}


class SshTransport:
    def __init__(self, remote: Remote, *, control_dir: Path, control_persist: str = "600") -> None:
        self.remote = remote
        self._control_dir = control_dir
        self._control_persist = control_persist

    # -- option plumbing -------------------------------------------------------

    @property
    def _control_path(self) -> Path:
        digest = hashlib.sha256(self.remote.destination.encode("utf-8")).hexdigest()[:16]
        return self._control_dir / f"cm-{digest}"

    def _ssh_options(self) -> list[str]:
        self._control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._control_dir.chmod(0o700)
        options: list[str] = []
        if self.remote.host_key_policy == HostKeyPolicy.STRICT:
            options.extend(["-o", "StrictHostKeyChecking=yes"])
        elif self.remote.host_key_policy == HostKeyPolicy.ACCEPT_NEW:
            options.extend(["-o", "StrictHostKeyChecking=accept-new"])
        options.extend(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={self._control_path}",
                "-o",
                f"ControlPersist={self._control_persist}",
            ]
        )
        return options

    def _ssh_argv(self, command: str) -> list[str]:
        return ["ssh", *self._ssh_options(), self.remote.destination, command]

    # -- exec -------------------------------------------------------------------

    def exec(
        self,
        command: str,
        *,
        input_text: str | None = None,
        timeout: float = 60.0,
        check: bool = True,
        retries: int = 0,
    ) -> ExecResult:
        argv = self._ssh_argv(command)
        attempts = retries + 1
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(attempts):
            try:
                result = subprocess.run(
                    argv,
                    **_stdin_kwargs(input_text),
                    env=clean_child_env(),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RemoteTimeout(
                    f"Remote command timed out after {timeout:.0f}s.",
                    command=command,
                ) from exc
            except OSError as exc:
                raise TransportError(
                    f"Could not launch ssh: {exc}",
                    command=command,
                    underlying_cause=exc,
                ) from exc
            if result.returncode != _SSH_TRANSPORT_RC or attempt == attempts - 1:
                break
        assert result is not None
        if check and result.returncode != 0:
            raise TransportError(
                f"Remote command failed (rc={result.returncode}).",
                command=command,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return ExecResult(result.returncode, result.stdout, result.stderr)

    def exec_python(
        self,
        script: str,
        args: Sequence[str] = (),
        *,
        timeout: float = 60.0,
        check: bool = True,
    ) -> ExecResult:
        command = "python3 - " + " ".join(shlex.quote(str(arg)) for arg in args)
        return self.exec(command, input_text=script, timeout=timeout, check=check)

    def exec_json(self, script: str, args: Sequence[str] = (), *, timeout: float = 60.0) -> Any:
        result = self.exec_python(script, args, timeout=timeout)
        for line in result.stdout.splitlines():
            if line.startswith(JSON_PREFIX):
                return json.loads(line[len(JSON_PREFIX) :])
        raise TransportError(
            "Remote script produced no structured result.",
            command="python3 -",
            returncode=result.returncode,
            stderr=result.stderr,
        )

    # -- transfers ---------------------------------------------------------------

    def _rsync(
        self,
        source: str,
        dest: str,
        extra: list[str],
        filters: Sequence[str],
        timeout: float,
        files_from: Sequence[str] | None = None,
        allow_partial: bool = False,
    ) -> SyncStats:
        rsh = "ssh " + " ".join(self._ssh_options())
        argv = [
            "rsync",
            "-az",
            "--stats",
            "--out-format=SLURMDECK_RSYNC\t%i\t%l\t%n",
            f"--timeout={int(max(timeout, 1))}",
            "-e",
            rsh,
            *extra,
        ]
        input_text: str | None = None
        if files_from is not None:
            argv.append("--files-from=-")
            input_text = "\n".join(files_from) + "\n"
        for rule in filters:
            argv.extend(["--filter", rule])
        argv.extend([source, dest])
        try:
            child_env = clean_child_env()
            child_env["LC_ALL"] = "C"
            result = subprocess.run(
                argv,
                **_stdin_kwargs(input_text),
                env=child_env,
                capture_output=True,
                text=True,
                timeout=timeout + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteTimeout(f"rsync timed out after {timeout:.0f}s.", command=" ".join(argv)) from exc
        if result.returncode != 0 and not (allow_partial and result.returncode in {23, 24}):
            raise TransportError(
                f"rsync failed (rc={result.returncode}).",
                command=" ".join(argv),
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return parse_rsync_stats(result.stdout, result.stderr, returncode=result.returncode)

    def upload(
        self,
        source: str,
        dest: str,
        *,
        delete: bool = False,
        filters: Sequence[str] = (),
        timeout: float = 600.0,
        files_from: Sequence[str] | None = None,
    ) -> SyncStats:
        extra = ["--delete"] if delete else []
        remote_dir = dest.rstrip("/") or "/"
        extra.append(f"--rsync-path=mkdir -p {shlex.quote(remote_dir)} && rsync")
        return self._rsync(source, f"{self.remote.destination}:{dest}", extra, filters, timeout, files_from)

    def download(
        self,
        source: str,
        dest: str,
        *,
        filters: Sequence[str] = (),
        timeout: float = 600.0,
    ) -> SyncStats:
        return self._rsync(
            f"{self.remote.destination}:{source}",
            dest,
            [],
            filters,
            timeout,
            allow_partial=True,
        )

    # -- streaming ----------------------------------------------------------------

    def stream(self, command: str, *, on_line: Callable[[str], None]) -> StreamHandle:
        process = subprocess.Popen(
            self._ssh_argv(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=clean_child_env(),
            text=True,
        )

        def _pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                on_line(line.rstrip("\n"))

        thread = threading.Thread(target=_pump, daemon=True)
        thread.start()

        def _close() -> None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

        return StreamHandle(_close=_close, _wait=lambda: process.wait())

    # -- connection lifecycle ------------------------------------------------------

    def connect(self) -> None:
        if self.alive():
            return
        argv = [
            "ssh",
            *self._ssh_options(),
            "-o",
            "ControlMaster=yes",
            "-N",
            "-f",
            self.remote.destination,
        ]
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                env=clean_child_env(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConnectError(f"Timed out connecting to {self.remote.destination}.") from exc
        if result.returncode != 0:
            raise ConnectError(
                f"Could not connect to {self.remote.destination}.",
                command=" ".join(argv),
                returncode=result.returncode,
                stderr=result.stderr,
            )

    def disconnect(self) -> None:
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={self._control_path}", self.remote.destination],
            stdin=subprocess.DEVNULL,
            env=clean_child_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def alive(self) -> bool:
        result = subprocess.run(
            ["ssh", "-O", "check", "-o", f"ControlPath={self._control_path}", self.remote.destination],
            stdin=subprocess.DEVNULL,
            env=clean_child_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0
