from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from slurmdeck.cli import _deps
from slurmdeck.cli.main import app
from slurmdeck.errors import UserError
from slurmdeck.models.env import ExistingEnvSpec
from slurmdeck.models.project import ProjectConfig, ProjectTarget
from slurmdeck.models.remote import Remote
from slurmdeck.models.resources import Resources

runner = CliRunner()


def _json_data(result):
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["error"] is None
    return document["data"]


@pytest.fixture()
def target_ctx(ctx):
    assert ctx.project is not None
    ctx.user_store.add_remote(
        Remote(
            name="jade",
            host="jade@login.example.com",
            base="/data/jade/slurmdeck",
            resolved_base="/data/jade/slurmdeck",
        )
    )
    ctx.project.config = ProjectConfig(
        project_id=ctx.project.config.project_id,
        display_name=ctx.project.config.display_name,
        default_target="jade",
        targets={
            "jade": ProjectTarget(
                remote="jade",
                resources=Resources(
                    partition="long",
                    account="jade-beta",
                    reservation="jade",
                    gres="gpu:1",
                ),
                env=ExistingEnvSpec(name="asterism-rocm", prefix="/data/jade/envs/asterism-rocm"),
            ),
            "htc": ProjectTarget(
                remote="cluster",
                resources=Resources(partition="gpu", gres="gpu:1"),
                env=ExistingEnvSpec(name="asterism-cuda", prefix="/data/htc/envs/asterism-cuda"),
            ),
        },
    )
    _deps.set_context_factory(lambda: ctx)
    yield ctx
    _deps.set_context_factory(None)


def test_target_list_json_marks_default_as_effective_current(target_ctx):
    result = runner.invoke(app, ["target", "list", "--json"])

    assert result.exit_code == 0, result.output
    data = _json_data(result)
    assert [target["name"] for target in data] == ["htc", "jade"]
    jade = next(target for target in data if target["name"] == "jade")
    assert jade["current"] is True
    assert jade["default"] is True
    assert jade["remote"] == "jade"
    assert jade["resources"]["reservation"] == "jade"
    assert jade["env"]["prefix"] == "/data/jade/envs/asterism-rocm"


def test_target_show_supports_named_human_and_current_json_views(target_ctx):
    named = runner.invoke(app, ["target", "show", "htc"])
    current = runner.invoke(app, ["target", "show", "--json"])

    assert named.exit_code == 0, named.output
    assert "Project target" in named.stdout
    assert "htc" in named.stdout
    assert "cluster" in named.stdout
    assert "asterism-cuda" in named.stdout
    assert current.exit_code == 0, current.output
    assert _json_data(current)["name"] == "jade"


def test_target_use_is_project_scoped_and_changes_resolution(target_ctx):
    result = runner.invoke(app, ["target", "use", "htc", "--json"])

    assert result.exit_code == 0, result.output
    data = _json_data(result)
    assert data["name"] == "htc"
    assert data["remote"] == "cluster"
    assert data["current"] is True
    assert target_ctx.user_store.current_target_name(target_ctx.project.config.project_id) == "htc"
    assert target_ctx.user_store.current_remote_name() == "cluster"
    assert target_ctx.resolve_project_target().name == "htc"
    listed = _json_data(runner.invoke(app, ["target", "list", "--json"]))
    assert next(target for target in listed if target["name"] == "htc")["current"] is True


def test_remote_use_explains_that_named_project_target_is_unchanged(target_ctx):
    target_ctx.user_store.set_current_target(target_ctx.project.config.project_id, "htc")

    result = runner.invoke(app, ["remote", "use", "jade"])

    assert result.exit_code == 0, result.output
    assert target_ctx.user_store.current_remote_name() == "jade"
    assert target_ctx.resolve_project_target().name == "htc"
    assert "user-level default" in result.output
    assert "continues to use htc@cluster" in result.output
    assert "slurmdeck target use NAME" in " ".join(result.output.split())


def test_remote_use_json_contract_is_unchanged_for_named_project(target_ctx):
    result = runner.invoke(app, ["remote", "use", "jade", "--json"])

    assert result.exit_code == 0, result.output
    data = _json_data(result)
    assert data["name"] == "jade"
    assert data["host"] == "jade@login.example.com"
    assert target_ctx.user_store.current_remote_name() == "jade"
    assert "continues to use" not in result.stdout


def test_target_use_rejects_unknown_target_without_changing_selection(target_ctx):
    result = runner.invoke(app, ["target", "use", "missing"])

    assert result.exit_code == 1
    assert isinstance(result.exception, UserError)
    assert "unknown project target" in str(result.exception)
    assert "htc, jade" in str(result.exception)
    assert target_ctx.user_store.current_target_name(target_ctx.project.config.project_id) is None


@pytest.mark.parametrize("command", [["show", ""], ["use", ""]])
def test_target_commands_reject_an_explicit_empty_name(target_ctx, command):
    result = runner.invoke(app, ["target", *command])

    assert result.exit_code == 1
    assert isinstance(result.exception, UserError)
    assert "unknown project target" in str(result.exception)
    assert target_ctx.user_store.current_target_name(target_ctx.project.config.project_id) is None


def test_submit_target_selects_remote_resources_and_records_provenance(target_ctx):
    project = target_ctx.project
    assert project is not None
    config = project.config
    project.config = ProjectConfig(
        project_id=config.project_id,
        display_name=config.display_name,
        default_target=config.default_target,
        targets={name: target.model_copy(update={"env": None}) for name, target in config.targets.items()},
        sync=config.sync,
    )

    htc = runner.invoke(
        app,
        ["submit", "--target", "htc", "--plan-only", "--json", "--", "python3", "train.py"],
    )
    jade = runner.invoke(
        app,
        ["submit", "--target", "jade", "--plan-only", "--json", "--", "python3", "train.py"],
    )

    assert htc.exit_code == 0, htc.output
    assert jade.exit_code == 0, jade.output
    htc_run = _json_data(htc)
    jade_run = _json_data(jade)
    assert (htc_run["target"], htc_run["remote"]) == ("htc", "cluster")
    assert htc_run["resources"]["partition"] == "gpu"
    assert htc_run["resources"]["reservation"] is None
    assert (jade_run["target"], jade_run["remote"]) == ("jade", "jade")
    assert jade_run["resources"]["partition"] == "long"
    assert jade_run["resources"]["reservation"] == "jade"


def test_submit_rejects_remote_override_for_target_project(target_ctx):
    result = runner.invoke(
        app,
        ["submit", "--remote", "cluster", "--plan-only", "--", "python3", "train.py"],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, UserError)
    assert "--remote cannot select a target-based project" in str(result.exception)


@pytest.mark.parametrize("command", [["list"], ["show"], ["use", "cluster"]])
def test_target_commands_explain_legacy_project_configuration(ctx, command):
    _deps.set_context_factory(lambda: ctx)
    try:
        result = runner.invoke(app, ["target", *command])
    finally:
        _deps.set_context_factory(None)

    assert result.exit_code == 1
    assert isinstance(result.exception, UserError)
    assert "no named targets" in str(result.exception)
    assert "default_target" in str(result.exception)
