"""Application context: the dependency-injection root shared by CLI and TUI."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from slurmdeck.errors import UserError
from slurmdeck.models.project import ProjectConfig, ProjectExecutionConfig
from slurmdeck.models.remote import Remote
from slurmdeck.storage.db import connect
from slurmdeck.storage.paths import ProjectPaths, RemoteLayout, UserPaths
from slurmdeck.storage.user_store import UserStore
from slurmdeck.storage.yamlio import load_yaml_model
from slurmdeck.transport import Transport
from slurmdeck.transport.ssh import SshTransport


@dataclass
class ProjectHandle:
    paths: ProjectPaths
    config: ProjectConfig


@dataclass(frozen=True)
class ProjectTargetSelection:
    """One resolved project execution target and its registered remote."""

    name: str | None
    config: ProjectExecutionConfig
    remote: Remote


@dataclass
class AppContext:
    user_paths: UserPaths
    user_store: UserStore
    project: ProjectHandle | None
    transport_factory: Callable[[Remote], Transport]
    # One connection per thread: sharing a sqlite3 connection across the TUI's
    # UI thread and worker threads produces torn reads (the module's statement
    # cache interleaves identical concurrent queries). WAL mode makes
    # per-thread connections safe and cheap.
    _db_local: threading.local = field(default_factory=threading.local, repr=False)

    @classmethod
    def create(
        cls,
        *,
        cwd: Path | None = None,
        user_paths: UserPaths | None = None,
        transport_factory: Callable[[Remote], Transport] | None = None,
    ) -> AppContext:
        paths = user_paths or UserPaths()
        store = UserStore(paths)
        project_paths = ProjectPaths.discover(cwd or Path.cwd())
        project: ProjectHandle | None = None
        if project_paths is not None:
            config = load_yaml_model(project_paths.config_path, ProjectConfig)
            project = ProjectHandle(paths=project_paths, config=config)

        def default_factory(remote: Remote) -> Transport:
            return SshTransport(remote, control_dir=paths.ssh_control_dir)

        return cls(
            user_paths=paths,
            user_store=store,
            project=project,
            transport_factory=transport_factory or default_factory,
        )

    def require_project(self) -> ProjectHandle:
        if self.project is None:
            raise UserError(
                "Not inside a slurmdeck project.",
                hint="Run `slurmdeck init` in your project directory first.",
            )
        return self.project

    def db(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = getattr(self._db_local, "connection", None)
        if connection is None:
            connection = connect(self.require_project().paths.db_path)
            self._db_local.connection = connection
        return connection

    def resolve_remote(self, name: str | None = None) -> Remote:
        """Resolve a remote by explicit name, project override, or user default."""
        chosen = name
        if chosen is None and self.project is not None:
            config = self.project.config
            if config.targets:
                selected = self.user_store.current_target_name(config.project_id)
                if selected not in config.targets:
                    selected = config.default_target
                if selected is not None:
                    chosen = config.targets[selected].remote
            else:
                chosen = config.remote
        if chosen is None:
            chosen = self.user_store.current_remote_name()
        if chosen is None:
            raise UserError(
                "No remote selected.",
                hint="Add one with `slurmdeck remote add ...` or select one with `slurmdeck remote use <name>`.",
            )
        return self.user_store.read_remote(chosen)

    def resolve_project_target(
        self,
        target_name: str | None = None,
        *,
        remote_name: str | None = None,
    ) -> ProjectTargetSelection:
        """Resolve an atomic project target, retaining legacy ``--remote`` behavior."""
        project = self.require_project()
        config = project.config
        if target_name is not None and remote_name is not None:
            raise UserError("Use either --target or --remote, not both.")

        if config.targets:
            if remote_name is not None:
                raise UserError(
                    "--remote cannot select a target-based project.",
                    hint=f"Use `--target <name>` (known: {', '.join(sorted(config.targets))}).",
                )
            selected = (
                target_name if target_name is not None else self.user_store.current_target_name(config.project_id)
            )
            if selected not in config.targets:
                selected = target_name if target_name is not None else config.default_target
            try:
                execution = config.execution(selected)
            except ValueError as exc:
                raise UserError(str(exc), hint=f"Known targets: {', '.join(sorted(config.targets))}.") from exc
            assert execution.remote is not None
            return ProjectTargetSelection(
                name=execution.target,
                config=execution,
                remote=self.user_store.read_remote(execution.remote),
            )

        if target_name is not None:
            raise UserError(
                "This project has no named targets.",
                hint="Add default_target and targets to .slurmdeck/project.yaml, or keep using --remote.",
            )
        chosen_remote = (
            remote_name if remote_name is not None else config.remote or self.user_store.current_remote_name()
        )
        if chosen_remote is None:
            raise UserError(
                "No remote selected.",
                hint="Add one with `slurmdeck remote add ...` or select one with `slurmdeck remote use <name>`.",
            )
        remote = self.user_store.read_remote(chosen_remote)
        execution = config.execution(remote=remote.name)
        return ProjectTargetSelection(
            name=None,
            config=execution,
            remote=remote,
        )

    def transport(self, remote: Remote) -> Transport:
        return self.transport_factory(remote)

    def layout(self, remote: Remote) -> RemoteLayout:
        if not remote.resolved_base:
            raise UserError(
                f"Remote {remote.name!r} has not been connected yet, so its base path is unresolved.",
                hint=f"Run `slurmdeck remote connect {remote.name}` once.",
            )
        return RemoteLayout(remote.resolved_base)
