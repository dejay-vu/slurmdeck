from __future__ import annotations

import json
from pathlib import Path

import pytest

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.services.run_planning import RunPlan, RunPlanner
from slurmdeck.services.runs import RunService

SWEEP = Sweep.model_validate(
    {
        "version": 1,
        "parameters": {"seed": [0, 1]},
        "config": {"model": "smoke", "seed": "{seed}"},
        "env": {"SEED": "{seed}"},
    }
)

COMMAND = CommandTemplateSpec(argv=["python3", "train.py", "--config", "{config}", "{args}"])


def _remove_database(ctx) -> None:
    paths = ctx.require_project().paths
    paths.db_path.unlink()
    Path(f"{paths.db_path}-shm").unlink(missing_ok=True)
    Path(f"{paths.db_path}-wal").unlink(missing_ok=True)


def _assert_no_run_state(ctx) -> None:
    paths = ctx.require_project().paths
    assert not paths.db_path.exists()
    assert not paths.runs_dir.exists()
    assert not paths.run_staging_dir.exists()


def test_planner_is_pure_and_renders_the_complete_run_payload(ctx, remote):
    _remove_database(ctx)

    plan = RunPlanner(ctx).plan(
        command=COMMAND,
        sweep=SWEEP,
        sweep_file="sweeps/smoke.yaml",
        name="smoke",
        overrides=ResourceOverrides(time="00:30:00"),
        remote=remote,
    )

    assert isinstance(plan, RunPlan)
    assert plan.manifest.run_id == plan.run_id
    assert plan.manifest.task_count == 2
    assert plan.manifest.resources.time == "00:30:00"
    assert len(plan.tasks) == 2
    assert plan.tasks[0].record.spec.env == {"SEED": "0"}
    assert plan.tasks[0].rendered_config == b"model: smoke\nseed: 0\n"

    config_paths = {task.record.spec.config for task in plan.tasks}
    assert None not in config_paths
    expected_paths = {
        protocol.TASKS_FILE,
        protocol.RUN_MANIFEST_FILE,
        protocol.AGENT_FILE,
        protocol.ACTIVATION_FILE,
        protocol.SBATCH_FILE,
        f"{protocol.CONFIGS_DIR}/.keep",
        f"{protocol.LOGS_DIR}/.keep",
        f"{protocol.RESULTS_DIR}/.keep",
        *config_paths,
    }
    assert set(plan.rendered_files) == expected_paths
    assert all(isinstance(payload, bytes) for payload in plan.rendered_files.values())
    task_lines = [json.loads(line) for line in plan.rendered_files[protocol.TASKS_FILE].splitlines()]
    assert len(task_lines) == 2
    assert task_lines[0]["argv"][:2] == ["python3", "train.py"]
    assert task_lines[0]["argv"][3].endswith(".yaml")
    assert b"#SBATCH --array=0-1" in plan.rendered_files[protocol.SBATCH_FILE]
    assert b"--time=00:30:00" in plan.rendered_files[protocol.SBATCH_FILE]
    assert json.loads(plan.rendered_files[protocol.RUN_MANIFEST_FILE]) == plan.manifest.model_dump(mode="json")
    _assert_no_run_state(ctx)


@pytest.mark.parametrize(
    ("command", "sweep", "overrides", "match"),
    [
        (
            CommandTemplateSpec(argv=["python3", "{missing}"]),
            None,
            ResourceOverrides(),
            "Unknown placeholder",
        ),
        (
            COMMAND,
            Sweep.model_validate(
                {
                    "version": 1,
                    "parameters": {"seed": [0]},
                    "exclude": [{"seed": 0}],
                }
            ),
            ResourceOverrides(),
            "produced no tasks",
        ),
        (COMMAND, None, ResourceOverrides(cpus=0), "cpus must be greater than zero"),
        (
            CommandTemplateSpec(argv=["python3", "train.py", "{args}"]),
            Sweep.model_validate(
                {
                    "version": 1,
                    "parameters": {"seed": [0]},
                    "config": {"nested": {}},
                }
            ),
            ResourceOverrides(),
            "empty mapping",
        ),
    ],
)
def test_invalid_plan_inputs_create_no_run_state(ctx, remote, command, sweep, overrides, match):
    _remove_database(ctx)

    with pytest.raises(UserError, match=match):
        RunPlanner(ctx).plan(command=command, sweep=sweep, overrides=overrides, remote=remote)

    _assert_no_run_state(ctx)


def test_render_failure_creates_no_run_state(ctx, remote, monkeypatch):
    _remove_database(ctx)

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("injected render failure")

    monkeypatch.setattr("slurmdeck.services.run_planning.render_template", fail_render)
    with pytest.raises(RuntimeError, match="injected render failure"):
        RunPlanner(ctx).plan(command=COMMAND, sweep=SWEEP, overrides=ResourceOverrides(), remote=remote)

    _assert_no_run_state(ctx)


def test_run_service_validates_purely_before_opening_storage(ctx, remote):
    _remove_database(ctx)
    command = CommandTemplateSpec(argv=["python3", "{missing}"])

    with pytest.raises(UserError, match="Unknown placeholder"):
        RunService(ctx).plan(command=command, overrides=ResourceOverrides(), remote=remote)

    _assert_no_run_state(ctx)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("time", "01:00:00\n#SBATCH --exclusive"),
        ("mem", "8G\r#SBATCH --exclusive"),
        ("gres", "gpu:1\n#SBATCH --exclusive"),
        ("partition", "short\n#SBATCH --exclusive"),
        ("account", "project\n#SBATCH --exclusive"),
        ("qos", "normal\n#SBATCH --exclusive"),
        ("constraint", "zen4\n#SBATCH --exclusive"),
    ],
)
def test_planner_rejects_control_characters_in_every_rendered_resource(ctx, remote, field, value):
    _remove_database(ctx)
    overrides = ResourceOverrides.model_validate({field: value})

    with pytest.raises(UserError, match=f"resources {field}"):
        RunPlanner(ctx).plan(command=CommandTemplateSpec(argv=["true"]), overrides=overrides, remote=remote)

    _assert_no_run_state(ctx)


def test_planner_validates_resolved_task_environment(ctx, remote):
    _remove_database(ctx)
    sweep = Sweep.model_validate(
        {
            "version": 1,
            "tasks": [{"name": "bad-env", "env": {"BAD=KEY": "value"}}],
        }
    )

    with pytest.raises(UserError, match="environment variable"):
        RunPlanner(ctx).plan(
            command=CommandTemplateSpec(argv=["true"]),
            sweep=sweep,
            overrides=ResourceOverrides(),
            remote=remote,
        )

    _assert_no_run_state(ctx)
