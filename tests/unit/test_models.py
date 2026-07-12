from __future__ import annotations

import pytest
from pydantic import ValidationError

from slurmdeck.errors import SchemaVersionError, UserError
from slurmdeck.models.common import safe_name, validate_name
from slurmdeck.models.env import CondaEnvSpec, EnvBinding, EnvWaitPolicy, ExistingEnvSpec
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.remote import HostKeyPolicy, Remote
from slurmdeck.models.resources import ResourceOverrides, Resources
from slurmdeck.models.run import CommandTemplateSpec, RunManifest
from slurmdeck.models.sweep import Sweep

PROJECT_IDENTITY = {"project_id": "project-1", "display_name": "Research Project"}


class TestRemote:
    def test_requires_exactly_one_destination(self):
        with pytest.raises(ValidationError):
            Remote(name="a", base="/x")
        with pytest.raises(ValidationError):
            Remote(name="a", host="u@h", ssh_alias="h", base="/x")
        assert Remote(name="a", ssh_alias="cluster", base="/x").destination == "cluster"

    def test_rejects_unknown_keys(self):
        with pytest.raises(ValidationError):
            Remote.model_validate({"name": "a", "host": "u@h", "base": "/x", "hots": "typo"})

    def test_host_key_policy_defaults_to_inheriting_openssh_config(self):
        remote = Remote.model_validate({"name": "a", "host": "u@h", "base": "/x"})

        assert remote.host_key_policy == HostKeyPolicy.INHERIT
        assert Remote(name="a", host="u@h", base="/x", host_key_policy="strict").host_key_policy == "strict"


class TestNames:
    def test_validate_name_rejects_traversal(self):
        with pytest.raises(UserError):
            validate_name("../etc", what="remote name")
        with pytest.raises(UserError):
            validate_name("a/b")
        assert validate_name("run-1.2_ok") == "run-1.2_ok"

    def test_safe_name(self):
        assert safe_name("lr=0.001 & fun") == "lr-0.001-fun"
        assert safe_name("///") == "task"


class TestResources:
    def test_merge_applies_only_set_overrides(self):
        merged = Resources(time="12:00:00", cpus=4).merged(ResourceOverrides(time="01:00:00"))
        assert merged.time == "01:00:00"
        assert merged.cpus == 4


class TestProjectConfig:
    def test_identity_fields_are_required(self):
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({})

    def test_env_discriminated_union(self):
        config = ProjectConfig.model_validate(
            {
                **PROJECT_IDENTITY,
                "env": {"type": "conda", "name": "ml", "spec_file": "env.yml", "modules": ["cuda"]},
            }
        )
        assert isinstance(config.env, CondaEnvSpec)
        config = ProjectConfig.model_validate({**PROJECT_IDENTITY, "env": {"type": "existing", "prefix": "/opt/env"}})
        assert isinstance(config.env, ExistingEnvSpec)
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({**PROJECT_IDENTITY, "env": {"type": "docker"}})

    def test_managed_environment_prefix_is_not_configurable(self):
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate(
                {
                    **PROJECT_IDENTITY,
                    "env": {"type": "conda", "name": "ml", "prefix": "/legacy/custom-root"},
                }
            )

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({**PROJECT_IDENTITY, "resource": {}})


class TestSweepSchema:
    def test_unknown_top_level_key_rejected(self):
        # regression: the legacy loader silently ignored unknown keys and
        # submitted a single empty task
        with pytest.raises(ValidationError):
            Sweep.model_validate({"version": 1, "global": {"seed": [1]}})
        with pytest.raises(ValidationError):
            Sweep.model_validate({"version": 1, "parametrs": {"seed": [1]}})

    def test_version_required(self):
        with pytest.raises(ValidationError):
            Sweep.model_validate({"parameters": {"seed": [1]}})

    def test_tasks_and_matrix_are_exclusive(self):
        with pytest.raises(ValidationError, match="cannot be combined"):
            Sweep.model_validate({"version": 1, "tasks": [{"config": {}}], "parameters": {"a": [1]}})

    def test_empty_axis_rejected(self):
        with pytest.raises(ValidationError, match="non-empty"):
            Sweep.model_validate({"version": 1, "parameters": {"seed": []}})

    def test_env_scalars_coerced(self):
        sweep = Sweep.model_validate({"version": 1, "parameters": {"a": [1]}, "env": {"N": 1, "B": True, "X": None}})
        assert sweep.env == {"N": "1", "B": "true", "X": ""}


class TestRunManifest:
    def test_round_trip(self):
        manifest = RunManifest(
            project_id="project-1",
            project_display_name="Research Project",
            run_id="r1",
            name="r1",
            created_at="2026-01-01T00:00:00Z",
            remote="cluster",
            remote_root="/base/runs/r1",
            env_binding=EnvBinding(
                env_id="env-1",
                generation_id="generation-1",
                prefix="/base/envs/generations/env-1/generation-1",
                attempt_id="attempt-1",
                build_job_id="42",
                wait_policy=EnvWaitPolicy.READY,
            ),
            env_dependency_state="ready",
            env_dependency_reason="environment available",
            resources=Resources(),
            command=CommandTemplateSpec(argv=["python", "x.py"]),
            task_count=1,
        )
        again = RunManifest.model_validate_json(manifest.model_dump_json())
        assert again == manifest

    def test_future_schema_version_rejected_with_hint(self):
        payload = {
            "schema_version": 99,
            "project_id": "project-1",
            "project_display_name": "Research Project",
            "run_id": "r1",
            "name": "r1",
            "created_at": "t",
            "remote": "c",
            "remote_root": "/x",
            "resources": {},
            "command": {"argv": ["x"]},
            "task_count": 1,
        }
        with pytest.raises(SchemaVersionError, match="schema version 99"):
            RunManifest.model_validate(payload)

    def test_command_exactly_one_form(self):
        with pytest.raises(ValidationError):
            CommandTemplateSpec(argv=["x"], shell="x")
        with pytest.raises(ValidationError):
            CommandTemplateSpec()
