"""Project target discovery and selection."""

from __future__ import annotations

from slurmdeck.errors import UserError
from slurmdeck.models.common import NameStr, StrictModel
from slurmdeck.models.env import EnvSpec
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.resources import Resources
from slurmdeck.services.context import AppContext


class TargetView(StrictModel):
    """One configured project target as presented by the CLI."""

    name: NameStr
    remote: NameStr
    current: bool
    default: bool
    resources: Resources
    env: EnvSpec | None = None


class TargetService:
    """List, inspect, and atomically select project execution targets."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def _config(self) -> ProjectConfig:
        config = self._ctx.require_project().config
        if not config.targets:
            raise UserError(
                "This project has no named targets.",
                hint=(
                    "Add `default_target` and `targets` to .slurmdeck/project.yaml before using `slurmdeck target ...`."
                ),
            )
        return config

    def _current_name(self, config: ProjectConfig) -> str:
        current = self._ctx.user_store.current_target_name(config.project_id)
        if current in config.targets:
            return current
        assert config.default_target is not None
        return config.default_target

    @staticmethod
    def _view(config: ProjectConfig, name: str, current_name: str) -> TargetView:
        target = config.targets[name]
        return TargetView(
            name=name,
            remote=target.remote,
            current=name == current_name,
            default=name == config.default_target,
            resources=target.resources,
            env=target.env,
        )

    def list_targets(self) -> list[TargetView]:
        """Return all targets in stable name order."""
        config = self._config()
        current = self._current_name(config)
        return [self._view(config, name, current) for name in sorted(config.targets)]

    def show(self, name: str | None = None) -> TargetView:
        """Show one target, defaulting to the effective current selection."""
        config = self._config()
        current = self._current_name(config)
        selected = name if name is not None else current
        resolved = self._ctx.resolve_project_target(selected)
        assert resolved.name is not None
        return self._view(config, resolved.name, current)

    def use(self, name: str) -> TargetView:
        """Validate and persist one project's current target."""
        config = self._config()
        resolved = self._ctx.resolve_project_target(name)
        assert resolved.name is not None
        self._ctx.user_store.set_current_target(config.project_id, resolved.name)
        return self._view(config, resolved.name, resolved.name)
