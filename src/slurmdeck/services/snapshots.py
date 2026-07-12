"""Code snapshots: content-hashed, uploaded once, reused across runs.

The same file list feeds both the hash and the upload (``rsync --files-from``),
so what is hashed is exactly what lands on the cluster.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.project import SyncConfig
from slurmdeck.operations import OperationPhase, OperationReporter, OperationSink, noop_operation_sink
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import Transport, TransportError, parse_json_lines

DEFAULT_IGNORES = [
    ".git",
    ".slurmdeck",
    "pulled",  # the blessed `run pull` destination — results must not re-enter snapshots
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "dist",
    "build",
    "*.egg-info",
    ".DS_Store",
]

_SAFE_ENV_NAMES = {".env.example", ".env.sample", ".env.template"}
_SENSITIVE_NAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "service-account.json",
    "service_account.json",
}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_PRIVATE_KEY_HEADER_RE = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_KEY_SCAN_BYTES = 64 * 1024

_SNAPSHOT_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

_WRITE_MARKER_SCRIPT = """
import json, os, sys, time

path = sys.argv[1]
code_dir = sys.argv[3]
size_bytes = 0
for root, directories, files in os.walk(code_dir, topdown=True, followlinks=False):
    directories[:] = [name for name in directories if not os.path.islink(os.path.join(root, name))]
    for name in files:
        candidate = os.path.join(root, name)
        if not os.path.islink(candidate):
            size_bytes += os.path.getsize(candidate)
payload = {
    "schema_version": 1,
    "hash": sys.argv[2],
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "size_bytes": size_bytes,
}
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
os.replace(tmp, path)
"""


@dataclass(frozen=True)
class Snapshot:
    hash: str
    remote_path: str
    reused: bool


@dataclass(frozen=True)
class SnapshotPreview:
    hash: str
    file_count: int
    size_bytes: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotView:
    hash: str
    remote_path: str
    size_bytes: int
    created_at: str
    reference_count: int
    references: tuple[str, ...]
    age_seconds: float | None


@dataclass(frozen=True)
class SnapshotGcReport:
    dry_run: bool
    candidates: tuple[str, ...]
    deleted: tuple[str, ...]
    failed: tuple[str, ...]


def _ignore_patterns(project_dir: Path, sync: SyncConfig) -> list[str]:
    patterns = list(DEFAULT_IGNORES) + list(sync.extra_ignores)
    ignore_file = project_dir / sync.ignore_file
    if ignore_file.is_file():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line.rstrip("/"))
    return patterns


def _is_ignored(rel: Path, patterns: list[str]) -> bool:
    text = rel.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(text, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in rel.parts):
            return True
    return False


def _validated_sensitive_allowlist(sync: SyncConfig) -> set[str]:
    allowlist: set[str] = set()
    for raw in sync.allow_sensitive_files:
        path = PurePosixPath(raw)
        if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in raw:
            raise UserError(
                f"Invalid sync.allow_sensitive_files path: {raw!r}.",
                hint="Use an exact project-relative POSIX path without '..'.",
            )
        allowlist.add(path.as_posix())
    return allowlist


def _sensitive_name(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    name = path.name.lower()
    parts = {part.lower() for part in path.parts[:-1]}
    if name == ".env" or (name.startswith(".env.") and name not in _SAFE_ENV_NAMES):
        return True
    if name in _SENSITIVE_NAMES or path.suffix.lower() in _SENSITIVE_SUFFIXES:
        return True
    return (name == "config.json" and ".docker" in parts) or (name == "config" and ".kube" in parts)


def _contains_private_key_header(path: Path) -> bool:
    with path.open("rb") as stream:
        return _PRIVATE_KEY_HEADER_RE.search(stream.read(_PRIVATE_KEY_SCAN_BYTES)) is not None


def _reject_sensitive_files(project_dir: Path, files: list[str], sync: SyncConfig) -> None:
    allowlist = _validated_sensitive_allowlist(sync)
    blocked = [
        relative_path
        for relative_path in files
        if relative_path not in allowlist
        and (_sensitive_name(relative_path) or _contains_private_key_header(project_dir / relative_path))
    ]
    if not blocked:
        return
    shown = ", ".join(blocked[:8])
    remainder = f" and {len(blocked) - 8} more" if len(blocked) > 8 else ""
    raise UserError(
        f"Snapshot selection contains sensitive file(s): {shown}{remainder}.",
        hint=(
            "Exclude them with .slurmdeckignore. To intentionally upload a reviewed file, "
            "add its exact project-relative path to sync.allow_sensitive_files."
        ),
    )


def _git_files(project_dir: Path, *, include_untracked: bool) -> list[str] | None:
    """File list from git (tracked + optionally untracked-not-ignored), or None outside git."""
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None
    args = ["git", "ls-files"]
    if include_untracked:
        args += ["--cached", "--others", "--exclude-standard"]
    listing = subprocess.run(args, cwd=project_dir, capture_output=True, text=True, check=True)
    return [line for line in listing.stdout.splitlines() if line.strip()]


def select_files(project_dir: Path, sync: SyncConfig) -> list[str]:
    """The snapshot file list (project-relative POSIX paths, sorted)."""
    patterns = _ignore_patterns(project_dir, sync)
    from_git = _git_files(project_dir, include_untracked=sync.include_untracked)
    candidates = from_git if from_git is not None else _walk_files(project_dir, patterns)
    selected = [rel for rel in candidates if (project_dir / rel).is_file() and not _is_ignored(Path(rel), patterns)]
    files = sorted(selected)
    _reject_sensitive_files(project_dir, files, sync)
    return files


def _walk_files(project_dir: Path, patterns: list[str]) -> list[str]:
    """Walk top-down and remove ignored directories before ``os.walk`` enters them."""
    candidates: list[str] = []
    for root_text, directories, files in os.walk(project_dir, topdown=True, followlinks=False):
        root = Path(root_text)
        kept_directories: list[str] = []
        for name in directories:
            path = root / name
            relative = path.relative_to(project_dir)
            if not path.is_symlink() and not _is_ignored(relative, patterns):
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in files:
            path = root / name
            relative = path.relative_to(project_dir)
            if path.is_file() and not path.is_symlink() and not _is_ignored(relative, patterns):
                candidates.append(relative.as_posix())
    return candidates


class SnapshotService:
    def preview(self, project_dir: Path, sync: SyncConfig) -> SnapshotPreview:
        """Return the exact local file set that would be hashed and uploaded."""
        digest, files = self.compute(project_dir, sync)
        return SnapshotPreview(
            hash=digest,
            file_count=len(files),
            size_bytes=sum((project_dir / relative_path).stat().st_size for relative_path in files),
            files=tuple(files),
        )

    def compute(self, project_dir: Path, sync: SyncConfig) -> tuple[str, list[str]]:
        """Content hash over the selected files; returns (hash, file list)."""
        files = select_files(project_dir, sync)
        digest = hashlib.sha256()
        for rel in files:
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update((project_dir / rel).read_bytes())
        return digest.hexdigest(), files

    def ensure(
        self,
        transport: Transport,
        layout: RemoteLayout,
        project_dir: Path,
        sync: SyncConfig,
        *,
        known_exists: bool | None = None,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> Snapshot:
        reporter = OperationReporter("snapshot.ensure", operation_sink)
        reporter.started(OperationPhase.SNAPSHOT, message="Computing code snapshot")
        try:
            digest, files = self.compute(project_dir, sync)
            marker = layout.snapshot_marker(digest)
            code_dir = layout.snapshot_code_dir(digest)
            reporter.progress(
                OperationPhase.PROBE,
                message=f"Checking for snapshot {digest[:12]} on the remote",
            )
            exists = (
                known_exists
                if known_exists is not None
                else transport.exec(f"test -f {_quote(marker)}", check=False, retries=1).returncode == 0
            )
            if exists:
                snapshot = Snapshot(hash=digest, remote_path=code_dir, reused=True)
                reporter.completed(
                    OperationPhase.SNAPSHOT,
                    message=f"Reusing snapshot {digest[:12]}",
                    result_counts={"files": len(files), "reused": 1},
                )
                return snapshot
            reporter.progress(
                OperationPhase.UPLOAD,
                current=0,
                total=len(files),
                message=f"Uploading {len(files)} files to {code_dir}",
            )
            transport.exec(f"rm -rf {_quote(code_dir)} && mkdir -p {_quote(code_dir)}")
            transport.upload(f"{project_dir}/", f"{code_dir}/", files_from=files, timeout=1800)
            transport.exec_python(_WRITE_MARKER_SCRIPT, [marker, digest, code_dir])
            snapshot = Snapshot(hash=digest, remote_path=code_dir, reused=False)
        except BaseException as exc:
            reporter.failed(message=str(exc))
            raise
        reporter.completed(
            OperationPhase.SNAPSHOT,
            message=f"Uploaded snapshot {digest[:12]}",
            result_counts={"files": len(files), "reused": 0},
        )
        return snapshot

    def list_snapshots(self, transport: Transport, layout: RemoteLayout) -> list[SnapshotView]:
        payload = self._lifecycle_call(transport, ["snapshot-list", "--base", layout.base])
        raw_snapshots = payload.get("snapshots")
        if not isinstance(raw_snapshots, list):
            raise TransportError("Remote snapshot helper returned no snapshot list.")
        snapshots: list[SnapshotView] = []
        for raw in raw_snapshots:
            if not isinstance(raw, dict):
                raise TransportError("Remote snapshot helper returned an invalid snapshot record.")
            digest = raw.get("hash")
            path = raw.get("path")
            size_bytes = raw.get("size_bytes")
            created_at = raw.get("created_at")
            if not isinstance(digest, str) or not _SNAPSHOT_HASH_RE.fullmatch(digest):
                raise TransportError("Remote snapshot helper returned an invalid snapshot hash.")
            if not isinstance(path, str) or not path:
                raise TransportError("Remote snapshot helper returned an invalid snapshot path.")
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
                raise TransportError("Remote snapshot helper returned an invalid snapshot size.")
            if not isinstance(created_at, str) or not created_at:
                raise TransportError("Remote snapshot helper returned an invalid creation time.")
            references = raw.get("references")
            if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
                raise TransportError("Remote snapshot helper returned invalid references.")
            age_seconds = raw.get("age_seconds")
            if age_seconds is not None and not isinstance(age_seconds, (int, float)):
                raise TransportError("Remote snapshot helper returned an invalid age.")
            snapshots.append(
                SnapshotView(
                    hash=digest,
                    remote_path=path,
                    size_bytes=size_bytes,
                    created_at=created_at,
                    reference_count=len(references),
                    references=tuple(references),
                    age_seconds=float(age_seconds) if age_seconds is not None else None,
                )
            )
        return snapshots

    def gc(
        self,
        transport: Transport,
        layout: RemoteLayout,
        *,
        confirmed: bool = False,
        operation_sink: OperationSink = noop_operation_sink,
    ) -> SnapshotGcReport:
        reporter = OperationReporter("snapshot.gc", operation_sink)
        reporter.started(OperationPhase.CLEANUP, message="Scanning snapshot references")
        args = ["snapshot-gc", "--base", layout.base, "--min-age", "86400"]
        if confirmed:
            args.append("--delete")
        try:
            payload = self._lifecycle_call(transport, args)
            dry_run = payload.get("dry_run")
            if not isinstance(dry_run, bool):
                raise TransportError("Remote snapshot helper returned an invalid dry-run flag.")
            report = SnapshotGcReport(
                dry_run=dry_run,
                candidates=self._string_tuple(payload, "candidates"),
                deleted=self._string_tuple(payload, "deleted"),
                failed=self._string_tuple(payload, "failed"),
            )
        except BaseException as exc:
            reporter.failed(message=str(exc))
            raise
        reporter.completed(
            OperationPhase.CLEANUP,
            result_counts={
                "candidates": len(report.candidates),
                "deleted": len(report.deleted),
                "failed": len(report.failed),
            },
        )
        return report

    @staticmethod
    def _string_tuple(payload: dict[str, object], key: str) -> tuple[str, ...]:
        value = payload.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TransportError(f"Remote snapshot helper returned invalid {key} data.")
        return tuple(value)

    @staticmethod
    def _lifecycle_call(transport: Transport, args: list[str]) -> dict[str, object]:
        result = transport.exec_python(protocol.agent_source(), args, timeout=300, check=False)
        try:
            payloads = parse_json_lines(result.stdout)
        except (TypeError, ValueError) as exc:
            raise TransportError(
                "Remote snapshot helper returned malformed JSON.",
                returncode=result.returncode,
                stderr=result.stderr,
                underlying_cause=exc,
            ) from exc
        payload = next(
            (
                item
                for item in reversed(payloads)
                if isinstance(item, dict) and item.get("kind") == protocol.SNAPSHOT_LIFECYCLE_KIND
            ),
            None,
        )
        if payload is None or payload.get("schema_version") != 1:
            raise TransportError(
                "Remote snapshot helper produced no valid structured result.",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return payload


def _quote(path: str) -> str:
    import shlex

    return shlex.quote(path)
