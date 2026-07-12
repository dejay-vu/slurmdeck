from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from slurmdeck import __version__
from slurmdeck.agent import protocol
from slurmdeck.cli import _deps
from slurmdeck.cli.main import app
from slurmdeck.errors import UserError
from slurmdeck.models.cluster import ClusterProfile
from slurmdeck.models.env import (
    CondaEnvSpec,
    EnvBackend,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvOwnership,
)
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.resources import ResourceOverrides, Resources
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.services.env_registry import EnvRegistryClient
from slurmdeck.services.runs import RunService
from slurmdeck.storage.paths import ProjectPaths, RemoteLayout
from slurmdeck.storage.repos import RunRepo, TaskRepo
from slurmdeck.storage.yamlio import dump_yaml_model, load_yaml_model
from slurmdeck.transport import ExecResult, SyncStats
from slurmdeck.transport.errors import RemoteTimeout

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _json_data(result):
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["error"] is None
    return document["data"]


@pytest.fixture(autouse=True)
def _wire_context(ctx):
    _deps.set_context_factory(lambda: ctx)
    yield
    _deps.set_context_factory(None)


class TestBasics:
    def test_init_persists_project_identity(self, tmp_path, monkeypatch):
        project = tmp_path / "research-project"
        project.mkdir()
        monkeypatch.chdir(project)

        result = runner.invoke(app, ["init"])

        assert result.exit_code == 0, result.output
        config = load_yaml_model(ProjectPaths(project).config_path, ProjectConfig)
        assert str(uuid.UUID(config.project_id)) == config.project_id
        assert config.display_name == "research-project"

    def test_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_saved_ui_theme_is_the_shared_cli_default(self, ctx, monkeypatch):
        ctx.user_store.set_ui_theme("monokai")
        configured: list[str | None] = []

        def capture_theme(_theme_name=None, *, persisted=None):
            configured.append(persisted)

        monkeypatch.setattr("slurmdeck.cli.main.configure_output_theme", capture_theme)

        result = runner.invoke(app, ["remote", "list", "--json"])

        assert result.exit_code == 0, result.output
        assert configured == ["monokai"]

    @pytest.mark.parametrize(
        "args",
        [
            ["--help"],
            ["remote", "--help"],
            ["remote", "profile", "--help"],
            ["run", "--help"],
            ["snapshot", "--help"],
            ["env", "--help"],
            ["sweep", "--help"],
            ["submit", "--help"],
        ],
    )
    def test_help_screens(self, args):
        result = runner.invoke(app, args)
        assert result.exit_code == 0

    def test_unknown_option_is_usage_error(self):
        result = runner.invoke(app, ["submit", "--tme", "1:00", "--", "python", "x.py"])
        assert result.exit_code == 2

    def test_removed_legacy_commands_fail(self):
        for legacy in (["push"], ["checkout", "x"], ["submission", "list"]):
            result = runner.invoke(app, legacy)
            assert result.exit_code == 2


class TestRemoteCommands:
    def test_list_json(self):
        result = runner.invoke(app, ["remote", "list", "--json"])
        assert result.exit_code == 0
        payload = _json_data(result)
        assert payload[0]["name"] == "cluster"
        assert payload[0]["current"] is True

    def test_add_persists_explicit_host_key_policy(self, ctx):
        result = runner.invoke(
            app,
            [
                "remote",
                "add",
                "new-cluster",
                "--host",
                "user@login.example.com",
                "--base",
                "/remote/base",
                "--host-key-policy",
                "accept-new",
            ],
        )

        assert result.exit_code == 0, result.output
        assert ctx.user_store.read_remote("new-cluster").host_key_policy == "accept-new"

    def test_remove_requires_yes_when_not_interactive(self):
        result = runner.invoke(app, ["remote", "remove", "cluster"])
        assert result.exit_code != 0
        assert isinstance(result.exception, UserError)
        assert "--yes" in str(result.exception)

    def test_profile_set_and_show_json(self, tmp_path):
        profile = tmp_path / "cluster-profile.yaml"
        profile.write_text(
            """schema_version: 1
allowed_build_executors: [slurm]
default_build_executor: slurm
login_build_policy: forbidden
shared_filesystem:
  login_to_compute: true
module_initialization:
  strategy: none
conda:
  executable: conda
network:
  compute_access: full
  channel_access: direct
slurm:
  partition: short
  afterok_dependency: true
  kill_invalid_dependency: per_job
platform:
  system: Linux
  machine: x86_64
  conda_subdir: linux-64
""",
            encoding="utf-8",
        )

        saved = runner.invoke(app, ["remote", "profile", "set", "cluster", "--file", str(profile)])
        shown = runner.invoke(app, ["remote", "profile", "show", "cluster", "--json"])

        assert saved.exit_code == 0, saved.output
        assert shown.exit_code == 0, shown.output
        payload = _json_data(shown)
        assert payload["default_build_executor"] == "slurm"
        assert payload["slurm"]["kill_invalid_dependency"] == "per_job"


class TestRunFlow:
    def test_run_reconcile_recovers_an_unknown_submission_without_resubmitting(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
    ):
        runs = RunService(ctx)
        row = runs.plan(
            command=CommandTemplateSpec(argv=["python3", "train.py"]),
            overrides=ResourceOverrides(),
            remote=remote,
        )
        real_exec_python = fake_transport.exec_python

        def lose_response(script, args=(), *, timeout=60.0, check=True):
            result = real_exec_python(script, args, timeout=timeout, check=check)
            if args and args[0] == "submit-run":
                raise RemoteTimeout("lost response")
            return result

        monkeypatch.setattr(fake_transport, "exec_python", lose_response)
        failed = runner.invoke(app, ["run", "submit", row.id])
        assert isinstance(failed.exception, UserError)
        assert failed.exception.error.code == "run_submit_unknown"

        monkeypatch.setattr(fake_transport, "exec_python", real_exec_python)
        reconciled = runner.invoke(app, ["run", "reconcile", row.id])

        assert reconciled.exit_code == 0, reconciled.output
        assert "999001" in reconciled.output
        assert runs.get(row.id).state == "submitted"
        assert fake_transport.next_job_id == 999002

    def test_status_displays_scheduler_and_failure_reasons_from_status_views(self, ctx, remote, fake_transport):
        fake_transport.simulate_execution = False
        runs = RunService(ctx)
        row = runs.plan(
            command=CommandTemplateSpec(argv=["python3", "train.py"]),
            sweep=Sweep.model_validate({"version": 1, "parameters": {"seed": [0, 1]}}),
            overrides=ResourceOverrides(),
            remote=remote,
        )
        row = runs.submit(fake_transport, row.id)
        TaskRepo(ctx.db()).apply_artifact(
            row.id,
            [{"task_id": "001", "state": "FAILED", "exit_code": 3, "reason": "command exploded"}],
        )
        fake_transport.squeue_output = f"{row.slurm_job_id}_0|PENDING|Priority\n"

        result = runner.invoke(app, ["run", "status", row.id])

        assert result.exit_code == 0, result.output
        assert "Priority" in result.output
        assert "command exploded" in result.output
        assert "PENDING" in result.output
        assert "FAILED" in result.output

    def test_run_list_uses_service_owned_environment_dependency_state(self, ctx, remote):
        row = RunService(ctx).plan(
            command=CommandTemplateSpec(argv=["python3", "train.py"]),
            overrides=ResourceOverrides(),
            remote=remote,
        )
        RunRepo(ctx.db()).set_env_dependency(row.id, state="waiting", reason="Waiting for build 42")

        result = runner.invoke(app, ["run", "list", "--json"])

        assert result.exit_code == 0, result.output
        data = _json_data(result)
        assert data[0]["state"] == "WAITING_FOR_ENV"
        assert data[0]["env_dependency_reason"] == "Waiting for build 42"

    def test_submit_plan_only_then_run_commands(self, ctx, fake_transport):
        result = runner.invoke(app, ["submit", "--plan-only", "--name", "demo", "--", "python3", "train.py"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["run", "list", "--json"])
        rows = _json_data(result)
        assert len(rows) == 1
        run_id = rows[0]["id"]
        assert rows[0]["state"] == "planned"
        assert rows[0]["resources"]["time"] == "12:00:00"
        assert rows[0]["summary"] == {"total": 1, "counts": {"PENDING": 1}}

        result = runner.invoke(app, ["run", "submit", run_id])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["run", "status", "--json"])
        assert result.exit_code == 0, result.output
        payload = _json_data(result)
        assert payload["run_id"] == run_id
        assert payload["summary"]["counts"] == {"COMPLETED": 1}
        assert payload["refreshed_at"] is not None
        assert payload["is_stale"] is False
        assert "sources" in payload

        result = runner.invoke(app, ["run", "show", run_id, "--json"])
        assert _json_data(result)["slurm_job_id"] == "999001"

    def test_submit_plan_only_supports_json_envelope(self):
        result = runner.invoke(
            app,
            ["submit", "--plan-only", "--json", "--", "python3", "train.py"],
        )

        assert result.exit_code == 0, result.output
        assert result.stderr == ""
        data = _json_data(result)
        assert data["state"] == "planned"
        assert data["summary"] == {"total": 1, "counts": {"PENDING": 1}}

    def test_logs_report_selected_run_task_stream_and_json(self, ctx, remote, fake_transport):
        runs = RunService(ctx)
        row = runs.submit(
            fake_transport,
            runs.plan(
                command=CommandTemplateSpec(argv=["python3", "train.py"]),
                overrides=ResourceOverrides(),
                remote=remote,
            ).id,
        )
        TaskRepo(ctx.db()).apply_artifact(
            row.id,
            [{"task_id": "000", "state": "FAILED", "exit_code": 2, "reason": "boom"}],
        )
        log_path = Path(row.remote_root) / "logs" / f"task_{row.slurm_job_id}_0.err"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("traceback\n", encoding="utf-8")

        human = runner.invoke(app, ["run", "logs", row.id])
        machine = runner.invoke(app, ["run", "logs", row.id, "--json"])

        assert human.exit_code == 0, human.output
        assert human.stdout == "traceback\n"
        assert row.id in human.stderr
        assert "task 000" in human.stderr
        assert "stderr" in human.stderr
        assert machine.exit_code == 0, machine.output
        assert machine.stderr == ""
        data = _json_data(machine)
        assert (data["run_id"], data["task_id"], data["stream"]) == (row.id, "000", "stderr")

    def test_pull_reports_file_outcomes_and_partial_paths(self, ctx, remote, fake_transport, tmp_path, monkeypatch):
        runs = RunService(ctx)
        row = runs.submit(
            fake_transport,
            runs.plan(
                command=CommandTemplateSpec(argv=["python3", "train.py"]),
                overrides=ResourceOverrides(),
                remote=remote,
            ).id,
        )

        def partial_download(*_args, **_kwargs):
            return SyncStats(
                matched_files=4,
                transferred_files=2,
                skipped_files=1,
                failed_files=1,
                bytes_transferred=1234,
                failed_paths=(f"{row.remote_root}/results/000/bad.dat",),
                returncode=23,
            )

        monkeypatch.setattr(fake_transport, "download", partial_download)
        result = runner.invoke(app, ["run", "pull", row.id, "--into", str(tmp_path / "pulled")])
        machine = runner.invoke(
            app,
            ["run", "pull", row.id, "--into", str(tmp_path / "pulled-json"), "--json"],
        )

        assert result.exit_code == 4, result.output
        assert "Matched: 4" in result.stdout
        assert "Transferred: 2" in result.stdout
        assert "Skipped: 1" in result.stdout
        assert "Failed: 1" in result.stdout
        assert "Bytes: 1234" in result.stdout
        assert "results/000/bad.dat" in result.stderr
        assert machine.exit_code == 4, machine.output
        assert machine.stderr == ""
        document = json.loads(machine.stdout)
        assert document["meta"] == {"partial": True}
        assert document["data"]["failed_paths"] == ["results/000/bad.dat"]

    def test_empty_pull_is_reported_as_incomplete_instead_of_success(
        self,
        ctx,
        remote,
        fake_transport,
        tmp_path,
    ):
        runs = RunService(ctx)
        row = runs.submit(
            fake_transport,
            runs.plan(
                command=CommandTemplateSpec(argv=["python3", "train.py"]),
                overrides=ResourceOverrides(),
                remote=remote,
            ).id,
        )
        fake_transport.script_call("download", SyncStats())

        result = runner.invoke(
            app,
            ["run", "pull", row.id, "--into", str(tmp_path / "empty"), "--json"],
        )

        assert result.exit_code == 4, result.output
        assert result.stderr == ""
        document = json.loads(result.stdout)
        assert document["ok"] is True
        assert document["meta"] == {"partial": False, "empty": True}
        assert document["data"]["matched"] == 0
        assert document["data"]["transferred"] == 0

    def test_cancel_requires_yes(self, ctx, fake_transport):
        runner.invoke(app, ["submit", "--", "python3", "train.py"])
        result = runner.invoke(app, ["run", "cancel"])
        assert isinstance(result.exception, UserError)
        result = runner.invoke(app, ["run", "cancel", "--yes"])
        assert result.exit_code == 0, result.output
        assert any(call.startswith("scancel") for call in fake_transport.calls)

    def test_submit_without_command_gives_hint(self):
        result = runner.invoke(app, ["submit"])
        assert isinstance(result.exception, UserError)
        assert "after `--`" in str(result.exception)

    def test_clean_removes_remote_run_dir_by_default(self, ctx, fake_transport):
        from pathlib import Path

        runner.invoke(app, ["submit", "--", "python3", "train.py"])
        run_id = _json_data(runner.invoke(app, ["run", "list", "--json"]))[0]["id"]
        remote_root = _json_data(runner.invoke(app, ["run", "show", run_id, "--json"]))["remote_root"]
        assert Path(remote_root).exists()
        result = runner.invoke(app, ["run", "clean", run_id, "--yes"])
        assert result.exit_code == 0, result.output
        assert "on the remote" in result.stderr
        assert not Path(remote_root).exists()

    def test_clean_keep_remote_leaves_remote_dir(self, ctx, fake_transport):
        from pathlib import Path

        runner.invoke(app, ["submit", "--", "python3", "train.py"])
        run_id = _json_data(runner.invoke(app, ["run", "list", "--json"]))[0]["id"]
        remote_root = _json_data(runner.invoke(app, ["run", "show", run_id, "--json"]))["remote_root"]
        result = runner.invoke(app, ["run", "clean", run_id, "--keep-remote", "--yes"])
        assert result.exit_code == 0, result.output
        assert Path(remote_root).exists()
        assert _json_data(runner.invoke(app, ["run", "list", "--json"])) == []

    def test_partial_clean_json_reports_each_outcome_and_retains_local_retry_handle(
        self,
        ctx,
        fake_transport,
    ):
        runner.invoke(app, ["submit", "--", "python3", "train.py"])
        row = RunService(ctx).get()
        shutil.rmtree(row.remote_root)
        payload = {
            "kind": protocol.RUN_CLEAN_KIND,
            "schema_version": 1,
            "ok": False,
            "run_id": row.id,
            "token": row.submission_token,
            "removed_run": True,
            "removed_receipt": False,
            "error": "injected receipt cleanup failure",
        }
        fake_transport.script_call(
            "helper:clean-run",
            ExecResult(0, protocol.JSON_PREFIX + json.dumps(payload) + "\n", ""),
        )

        result = runner.invoke(app, ["run", "clean", row.id, "--yes", "--json"])

        assert result.exit_code == 4, result.output
        assert result.stderr == ""
        document = json.loads(result.stdout)
        assert document["ok"] is True
        assert document["meta"] == {"partial": True}
        assert document["data"]["local_removed"] is False
        assert document["data"]["remote_removed"] is True
        assert document["data"]["receipt_removed"] is False
        assert document["data"]["snapshot_reference_released"] is False
        assert RunService(ctx).get(row.id).id == row.id


class TestEnvironmentCommands:
    def test_prepare_has_clean_break_rebuild_option_and_no_force_alias(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("TERM", "xterm")
        help_result = runner.invoke(app, ["env", "prepare", "--help"])
        removed = runner.invoke(app, ["env", "prepare", "--force"])
        plain_help = _ANSI.sub("", help_result.output)

        assert help_result.exit_code == 0
        assert "--rebuild" in plain_help
        assert "--force" not in plain_help
        assert removed.exit_code == 2

    def test_plan_json_reports_read_only_resolution(self, ctx, remote, fake_transport, project_dir):
        (project_dir / "environment.yml").write_text(
            "channels: [conda-forge]\ndependencies: [python=3.12]\n",
            encoding="utf-8",
        )
        assert ctx.project is not None
        ctx.project.config = ctx.project.config.model_copy(
            update={
                "resources": Resources(time="01:00:00", cpus=2, partition="short"),
                "env": CondaEnvSpec(
                    name="analysis",
                    build_resources=ResourceOverrides(cpus=4, mem="16G"),
                ),
            }
        )
        profile = ClusterProfile.model_validate(
            {
                "allowed_build_executors": ["slurm"],
                "default_build_executor": "slurm",
                "login_build_policy": "forbidden",
                "shared_filesystem": {"login_to_compute": True},
                "module_initialization": {"strategy": "none"},
                "conda": {"executable": "conda"},
                "network": {"compute_access": "full", "channel_access": "direct"},
                "slurm": {
                    "partition": "short",
                    "afterok_dependency": True,
                    "kill_invalid_dependency": "per_job",
                },
                "platform": {"system": "Linux", "machine": "x86_64", "conda_subdir": "linux-64"},
            }
        )
        dump_yaml_model(ctx.user_paths.remotes_dir / "cluster.yaml", remote.model_copy(update={"cluster": profile}))
        fake_transport._slurm_shims()
        fake_transport.calls.clear()

        result = runner.invoke(app, ["env", "plan", "--json"])

        assert result.exit_code == 0, result.output
        payload = _json_data(result)
        assert payload["action"] == "create"
        assert payload["complete"] is True
        assert payload["env_id"].endswith("-" + payload["full_hash"][:12])
        assert payload["channels"] == ["conda-forge"]
        assert payload["resolved_resources"]["cpus"] == 4
        assert payload["resolved_resources"]["mem"] == "16G"
        assert len(fake_transport.calls) == 2

    def test_registry_list_status_remove_and_not_found_contracts(self, fake_transport, remote_root):
        prefix = remote_root / "external-prefix"
        prefix.mkdir()
        full_hash = "d" * 64
        record = EnvironmentRecord(
            env_id=f"external-{full_hash[:12]}",
            full_hash=full_hash,
            backend=EnvBackend.EXISTING,
            ownership=EnvOwnership.EXTERNAL,
            status=EnvironmentStatus.READY,
            active_prefix=str(prefix),
            created_at="2026-07-11T00:00:00Z",
            updated_at="2026-07-11T00:00:00Z",
            verified_at="2026-07-11T00:00:00Z",
            provenance=EnvironmentProvenance(canonical_spec_hash=full_hash),
        )
        EnvRegistryClient().prepare(fake_transport, RemoteLayout(str(remote_root)), record)

        listed = runner.invoke(app, ["env", "list", "--json"])
        status = runner.invoke(app, ["env", "status", record.env_id, "--json"])
        missing = runner.invoke(app, ["env", "show", "missing-000000000000", "--json"])
        removed = runner.invoke(app, ["env", "remove", record.env_id, "--yes", "--json"])

        assert listed.exit_code == 0, listed.output
        assert listed.stderr == ""
        listed_data = _json_data(listed)
        assert listed_data[0]["record"]["env_id"] == record.env_id
        assert listed_data[0]["job_reason"] == "-"
        assert status.exit_code == 0, status.output
        assert _json_data(status)["record"]["status"] == "READY"
        assert isinstance(missing.exception, UserError)
        assert "not found" in str(missing.exception)
        assert removed.exit_code == 0, removed.output
        assert removed.stderr == ""
        assert _json_data(removed)["external_unregistered"] is True
        assert prefix.is_dir()


class TestSnapshotCommands:
    def test_preview_lists_exact_local_files_without_remote_calls(self, project_dir, fake_transport):
        fake_transport.calls.clear()

        result = runner.invoke(app, ["snapshot", "preview", "--json"])

        assert result.exit_code == 0, result.output
        assert result.stderr == ""
        payload = _json_data(result)
        assert payload["files"] == ["train.py"]
        assert payload["file_count"] == 1
        assert payload["size_bytes"] == (project_dir / "train.py").stat().st_size
        assert len(payload["hash"]) == 64
        assert fake_transport.calls == []

    def test_preview_rejects_a_sensitive_local_file(self, project_dir):
        (project_dir / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

        result = runner.invoke(app, ["snapshot", "preview"])

        assert isinstance(result.exception, UserError)
        assert ".env" in str(result.exception)
        assert ".slurmdeckignore" in str(result.exception)

    def test_list_and_dry_run_then_confirmed_gc(self, remote_root):
        digest = "f" * 64
        snapshot = remote_root / "snapshots" / digest
        (snapshot / "code").mkdir(parents=True)
        (snapshot / "code" / "payload").write_text("data", encoding="utf-8")
        (snapshot / ".complete.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "hash": digest,
                    "created_at": "2000-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

        listed = runner.invoke(app, ["snapshot", "list", "--json"])
        preview = runner.invoke(app, ["snapshot", "gc", "--json"])

        assert listed.exit_code == 0, listed.output
        assert listed.stderr == ""
        assert preview.stderr == ""
        assert _json_data(listed)[0]["hash"] == digest
        assert _json_data(preview)["dry_run"] is True
        assert _json_data(preview)["candidates"] == [digest]
        assert snapshot.is_dir()

        applied = runner.invoke(app, ["snapshot", "gc", "--yes", "--json"])

        assert applied.exit_code == 0, applied.output
        assert _json_data(applied)["deleted"] == [digest]
        assert not snapshot.exists()


class TestSweepCommands:
    def test_validate_and_preview(self, tmp_path):
        sweep = tmp_path / "sweep.yaml"
        sweep.write_text("version: 1\nparameters:\n  lr: [0.1, 0.01]\nconfig:\n  lr: '{lr}'\n", encoding="utf-8")
        result = runner.invoke(app, ["sweep", "validate", str(sweep)])
        assert result.exit_code == 0

        result = runner.invoke(app, ["sweep", "preview", str(sweep), "--json"])
        payload = _json_data(result)
        assert payload["total"] == 2
        assert payload["tasks"][0]["config"] == {"lr": 0.1}

    def test_validate_rejects_legacy_schema_with_location(self, tmp_path):
        sweep = tmp_path / "old.yaml"
        sweep.write_text("global:\n  seed: [1, 2]\n", encoding="utf-8")
        result = runner.invoke(app, ["sweep", "validate", str(sweep)])
        assert isinstance(result.exception, UserError)
        assert "version" in str(result.exception)


class TestDoctor:
    def test_doctor_human_output_reports_phase_progress(self):
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0, result.output
        assert "operation: Running doctor" in result.stderr
        assert "probe: Checking local tools" in result.stderr
        assert "connect: Resolving remote configuration" in result.stderr
        assert "validate: Checking project state" in result.stderr

    def test_doctor_json(self):
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code == 0, result.output
        assert result.stderr == ""
        checks = {item["name"]: item for item in _json_data(result)}
        assert checks["project"]["state"] == "OK"
