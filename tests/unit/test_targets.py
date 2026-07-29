from __future__ import annotations

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.env import ExistingEnvSpec
from slurmdeck.models.project import ProjectConfig, ProjectTarget
from slurmdeck.models.remote import Remote
from slurmdeck.models.resources import Resources


def _configure_targets(ctx) -> None:
    project = ctx.require_project()
    ctx.user_store.add_remote(
        Remote(
            name="jade",
            host="user@jade.example.com",
            base="/jade/slurmdeck",
            resolved_base="/jade/slurmdeck",
        )
    )
    project.config = ProjectConfig(
        project_id=project.config.project_id,
        display_name=project.config.display_name,
        default_target="jade",
        targets={
            "jade": ProjectTarget(
                remote="jade",
                resources=Resources(account="jade-beta", reservation="jade"),
                env=ExistingEnvSpec(name="rocm", prefix="/jade/rocm"),
            ),
            "htc": ProjectTarget(
                remote="cluster",
                resources=Resources(account="oerc-grn", constraint="gpu_sku:H100"),
                env=ExistingEnvSpec(name="cuda", prefix="/htc/cuda"),
            ),
        },
    )


def test_project_target_precedence_is_explicit_then_current_then_default(ctx):
    _configure_targets(ctx)
    project = ctx.require_project()

    default = ctx.resolve_project_target()
    ctx.user_store.set_current_target(project.config.project_id, "htc")
    current = ctx.resolve_project_target()
    explicit = ctx.resolve_project_target("jade")

    assert (default.name, default.remote.name, default.config.env.name) == (
        "jade",
        "jade",
        "rocm",
    )
    assert (current.name, current.remote.name, current.config.resources.account) == (
        "htc",
        "cluster",
        "oerc-grn",
    )
    assert (explicit.name, explicit.remote.name, explicit.config.resources.reservation) == (
        "jade",
        "jade",
        "jade",
    )
    assert ctx.resolve_remote().name == "cluster"


def test_target_project_rejects_shallow_remote_override(ctx):
    _configure_targets(ctx)

    with pytest.raises(UserError, match="--remote cannot select"):
        ctx.resolve_project_target(remote_name="cluster")
    with pytest.raises(UserError, match="either --target or --remote"):
        ctx.resolve_project_target("jade", remote_name="cluster")


def test_legacy_project_keeps_remote_selection_and_rejects_target(ctx):
    selection = ctx.resolve_project_target(remote_name="cluster")

    assert selection.name is None
    assert selection.remote.name == "cluster"
    assert selection.config.target is None
    with pytest.raises(UserError, match="no named targets"):
        ctx.resolve_project_target("jade")


def test_stale_saved_target_falls_back_to_project_default(ctx):
    _configure_targets(ctx)
    project = ctx.require_project()
    ctx.user_store.set_current_target(project.config.project_id, "missing")

    selection = ctx.resolve_project_target()

    assert selection.name == "jade"
    assert selection.remote.name == "jade"
