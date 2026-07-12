"""Remote management: add/list/use/remove and connection lifecycle."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import ClusterProfile
from slurmdeck.models.remote import HostKeyPolicy, Remote
from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.context import AppContext
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.storage.yamlio import (
    dump_yaml_model_atomic,
    format_validation_error,
    load_yaml_mapping,
)
from slurmdeck.transport import TransportError

_RESOLVE_BASE_SCRIPT = """
import json, os, pathlib, sys

base = os.path.expandvars(os.path.expanduser(sys.argv[1]))
path = pathlib.Path(base)
for child in sys.argv[2:]:
    (path / child).mkdir(parents=True, exist_ok=True)
print("SLURMDECK_JSON\\t" + json.dumps({"base": str(path)}))
"""


@dataclass(frozen=True)
class RemoteInfo:
    name: str
    destination: str
    base: str
    resolved_base: str | None
    current: bool


@dataclass(frozen=True)
class ConnectReport:
    remote: str
    destination: str
    resolved_base: str


@dataclass(frozen=True)
class RemoteConnectionView:
    remote: str
    destination: str
    connected: bool


class RemoteService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def add(
        self,
        name: str,
        *,
        host: str | None,
        ssh_alias: str | None,
        base: str,
        host_key_policy: HostKeyPolicy = HostKeyPolicy.INHERIT,
        use: bool = False,
    ) -> Remote:
        try:
            remote = Remote(
                name=name,
                host=host,
                ssh_alias=ssh_alias,
                base=base,
                host_key_policy=host_key_policy,
            )
        except ValidationError as exc:
            raise UserError(format_validation_error(f"remote {name!r}", exc)) from exc
        self._ctx.user_store.add_remote(remote)
        if use or self._ctx.user_store.current_remote_name() is None:
            self._ctx.user_store.set_current_remote(remote.name)
        return remote

    def list_remotes(self) -> list[RemoteInfo]:
        current = self._ctx.user_store.current_remote_name()
        infos = []
        for name in self._ctx.user_store.list_remote_names():
            remote = self._ctx.user_store.read_remote(name)
            infos.append(
                RemoteInfo(
                    name=name,
                    destination=remote.destination,
                    base=remote.base,
                    resolved_base=remote.resolved_base,
                    current=name == current,
                )
            )
        return infos

    def use(self, name: str) -> Remote:
        remote = self._ctx.user_store.read_remote(name)
        self._ctx.user_store.set_current_remote(name)
        return remote

    def remove(self, name: str) -> None:
        self._ctx.user_store.remove_remote(name)

    def show_profile(self, name: str | None = None) -> ClusterProfile | None:
        return self._ctx.resolve_remote(name).cluster

    @staticmethod
    def validate_profile_file(profile_file: Path) -> ClusterProfile:
        data = load_yaml_mapping(profile_file)
        try:
            return ClusterProfile.model_validate(data)
        except ValidationError as exc:
            raise UserError(format_validation_error(str(profile_file), exc)) from exc

    def replace_profile(self, name: str | None, profile: ClusterProfile) -> Remote:
        remote = self._ctx.resolve_remote(name)
        updated = remote.model_copy(update={"cluster": profile})
        path = self._ctx.user_paths.remotes_dir / f"{remote.name}.yaml"
        dump_yaml_model_atomic(path, updated)
        return updated

    def set_profile(self, name: str | None, profile_file: Path) -> Remote:
        return self.replace_profile(name, self.validate_profile_file(profile_file))

    def profile_diff(self, name: str | None, profile: ClusterProfile) -> str:
        current = self._ctx.resolve_remote(name).cluster
        before = yaml.safe_dump(current.model_dump(mode="json") if current is not None else {}, sort_keys=False)
        after = yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False)
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="configured",
                tofile="proposed",
            )
        )

    def connect(self, name: str | None = None) -> ConnectReport:
        """Open the SSH master, resolve the base path remotely, and create the layout."""
        remote = self._ctx.resolve_remote(name)
        transport = self._ctx.transport(remote)
        transport.connect()
        layout_children = ["runs", "snapshots", "envs", "receipts", "locks"]
        payload = transport.exec_json(_RESOLVE_BASE_SCRIPT, [remote.base, *layout_children], timeout=60)
        resolved = str(payload["base"]).rstrip("/")
        if not resolved or resolved == "/":
            raise UserError(f"Remote base resolved to {resolved!r}, refusing to use it.")
        updated = remote.model_copy(update={"resolved_base": resolved})
        path = self._ctx.user_paths.remotes_dir / f"{remote.name}.yaml"
        dump_yaml_model_atomic(path, updated)
        try:
            observation = ClusterCapabilityService().observe(transport, updated)
        except TransportError:
            # The layout connection succeeded.  A failed optional capability
            # probe is retried by the next mutating prepare and must not turn a
            # usable SSH connection into a false failure.
            pass
        else:
            EnvironmentCache(self._ctx.user_paths).remember_observation(updated, observation)
        return ConnectReport(remote=remote.name, destination=remote.destination, resolved_base=resolved)

    def disconnect(self, name: str | None = None) -> str:
        remote = self._ctx.resolve_remote(name)
        self._ctx.transport(remote).disconnect()
        return remote.name

    def status(self, name: str | None = None) -> RemoteConnectionView:
        remote = self._ctx.resolve_remote(name)
        return RemoteConnectionView(
            remote=remote.name,
            destination=remote.destination,
            connected=self._ctx.transport(remote).alive(),
        )

    def layout(self, remote: Remote) -> RemoteLayout:
        return self._ctx.layout(remote)
