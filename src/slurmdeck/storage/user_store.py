"""User-level store: remote definitions, current remote, and UI preferences."""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from slurmdeck.errors import UserError
from slurmdeck.models.common import validate_name
from slurmdeck.models.remote import Remote
from slurmdeck.storage.paths import UserPaths
from slurmdeck.storage.permissions import ensure_private_directory
from slurmdeck.storage.yamlio import dump_yaml_mapping_atomic, dump_yaml_model, load_yaml_model


@dataclass(frozen=True)
class UserStore:
    paths: UserPaths

    def _remote_path(self, name: str) -> str:
        validate_name(name, what="remote name")
        return str(self.paths.remotes_dir / f"{name}.yaml")

    def add_remote(self, remote: Remote) -> None:
        ensure_private_directory(self.paths.config_dir)
        path = self.paths.remotes_dir / f"{remote.name}.yaml"
        if path.exists():
            raise UserError(
                f"Remote {remote.name!r} already exists.",
                hint=f"Remove it first with `slurmdeck remote remove {remote.name}`.",
            )
        dump_yaml_model(path, remote)

    def read_remote(self, name: str) -> Remote:
        validate_name(name, what="remote name")
        path = self.paths.remotes_dir / f"{name}.yaml"
        if not path.exists():
            known = ", ".join(self.list_remote_names()) or "none configured"
            raise UserError(
                f"Unknown remote: {name!r} (known: {known}).",
                hint="Add one with `slurmdeck remote add <name> --host user@host --base <path>`.",
            )
        return load_yaml_model(path, Remote)

    def list_remote_names(self) -> list[str]:
        if not self.paths.remotes_dir.is_dir():
            return []
        return sorted(path.stem for path in self.paths.remotes_dir.glob("*.yaml"))

    def remove_remote(self, name: str) -> None:
        validate_name(name, what="remote name")
        path = self.paths.remotes_dir / f"{name}.yaml"
        if not path.exists():
            raise UserError(f"Unknown remote: {name!r}.")
        path.unlink()
        if self.current_remote_name() == name:
            self.set_current_remote(None)

    # -- current-remote pointer ------------------------------------------------

    def _read_state(self) -> dict[str, object]:
        if not self.paths.state_path.exists():
            return {}
        data = yaml.safe_load(self.paths.state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _write_state(self, state: dict[str, object]) -> None:
        dump_yaml_mapping_atomic(self.paths.state_path, dict(sorted(state.items())))

    def current_remote_name(self) -> str | None:
        name = self._read_state().get("current_remote")
        return str(name) if name else None

    def set_current_remote(self, name: str | None) -> None:
        state = self._read_state()
        if name is None:
            state.pop("current_remote", None)
        else:
            validate_name(name, what="remote name")
            state["current_remote"] = name
        self._write_state(state)

    # -- presentation preferences ---------------------------------------------

    def ui_theme(self) -> str | None:
        """Return a valid saved theme, ignoring stale or hand-edited values."""
        theme = self._read_state().get("ui_theme")
        if not isinstance(theme, str):
            return None
        try:
            return validate_name(theme, what="UI theme")
        except UserError:
            return None

    def set_ui_theme(self, name: str | None) -> None:
        state = self._read_state()
        if name is None:
            state.pop("ui_theme", None)
        else:
            normalized = validate_name(name.strip().lower(), what="UI theme")
            state["ui_theme"] = normalized
        self._write_state(state)
