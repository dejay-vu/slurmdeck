from __future__ import annotations

from pathlib import Path

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.env import EnvBinding, ExistingEnvSpec
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.operations import OperationPhase, OperationStatus
from slurmdeck.services.doctor import DoctorService
from slurmdeck.services.env_binding import activation_script_for_binding
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.logs import LogService, LogStream
from slurmdeck.services.remotes import RemoteService
from slurmdeck.services.results import pull_filters
from slurmdeck.services.runs import RunService
from slurmdeck.storage.repos import TaskRepo

COMMAND = CommandTemplateSpec(argv=["python3", "train.py"])


class TestPullFilters:
    def test_default_pull_has_no_rules(self):
        assert pull_filters() == ["- .keep"]

    def test_excludes_always_first(self):
        rules = pull_filters(task_ids=["000"], excludes=["*.tmp"])
        assert rules[0] == "- *.tmp"

    def test_logs_only(self):
        assert pull_filters(logs_only=True) == ["- .keep", "+ /logs/", "+ /logs/**", "- *"]

    def test_task_selection_narrows_results(self):
        rules = pull_filters(task_ids=["001", "003"])
        assert "+ /results/001/**" in rules
        assert "+ /results/003/**" in rules
        assert "- /results/*" in rules


class TestActivationScript:
    @staticmethod
    def _binding(prefix: str = "/opt/env") -> EnvBinding:
        return EnvBinding(
            env_id="existing-123456789abc",
            generation_id="",
            prefix=prefix,
            attempt_id="",
            build_job_id="",
        )

    def test_modules_bootstrap_without_login_shell(self):
        script = activation_script_for_binding(
            ExistingEnvSpec(prefix="/opt/env", modules=["cuda/12"]),
            None,
            self._binding(),
        )
        assert "command -v module" in script
        assert "/etc/profile.d/modules.sh" in script  # explicit bootstrap, no bash -l
        assert "module load cuda/12" in script

    def test_prefix_activation_covers_conda_and_venv(self):
        script = activation_script_for_binding(ExistingEnvSpec(prefix="/opt/env"), None, self._binding())
        assert "conda-meta" in script
        assert "bin/activate" in script
        assert "exit 127" in script  # fails loudly if the prefix is missing


class TestLogs:
    def test_fetch_reads_task_log(self, ctx, remote, fake_transport):
        runs = RunService(ctx)
        row = runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote)
        row = runs.submit(fake_transport, row.id)
        log_path = Path(row.remote_root) / "logs" / f"task_{row.slurm_job_id}_0.out"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("line1\nline2\n")
        log = LogService(ctx).fetch(
            fake_transport,
            row.id,
            task_id="000",
            stream=LogStream.STDOUT,
            lines=1,
        )
        assert log.text.strip() == "line2"
        assert (log.run_id, log.task_id, log.stream) == (row.id, "000", LogStream.STDOUT)

    def test_failed_task_defaults_to_non_empty_stderr(self, ctx, remote, fake_transport):
        runs = RunService(ctx)
        row = runs.submit(fake_transport, runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote).id)
        TaskRepo(ctx.db()).apply_artifact(
            row.id,
            [{"task_id": "000", "state": "FAILED", "exit_code": 2, "reason": "boom"}],
        )
        log_dir = Path(row.remote_root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"task_{row.slurm_job_id}_0.err").write_text("traceback\n")
        (log_dir / f"task_{row.slurm_job_id}_0.out").write_text("ordinary output\n")

        log = LogService(ctx).fetch(fake_transport, row.id, task_id="000")

        assert log.task_id == "000"
        assert log.stream is LogStream.STDERR
        assert log.text == "traceback\n"

    def test_empty_failed_stderr_falls_back_to_stdout(self, ctx, remote, fake_transport):
        runs = RunService(ctx)
        row = runs.submit(fake_transport, runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote).id)
        TaskRepo(ctx.db()).apply_artifact(
            row.id,
            [{"task_id": "000", "state": "FAILED", "exit_code": 2, "reason": "boom"}],
        )
        log_dir = Path(row.remote_root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"task_{row.slurm_job_id}_0.err").write_text("")
        (log_dir / f"task_{row.slurm_job_id}_0.out").write_text("ordinary output\n")

        log = LogService(ctx).fetch(fake_transport, row.id, task_id="000")

        assert log.stream is LogStream.STDOUT
        assert log.text == "ordinary output\n"

    def test_unknown_task_is_actionable(self, ctx, remote, fake_transport):
        runs = RunService(ctx)
        row = runs.submit(fake_transport, runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote).id)
        with pytest.raises(UserError, match="Unknown task"):
            LogService(ctx).fetch(fake_transport, row.id, task_id="999")

    def test_missing_explicit_log_is_an_actionable_error(self, ctx, remote, fake_transport):
        runs = RunService(ctx)
        row = runs.submit(fake_transport, runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote).id)

        with pytest.raises(UserError, match=r"stdout log for task '000'.*unavailable"):
            LogService(ctx).fetch(fake_transport, row.id, task_id="000", stream=LogStream.STDOUT)


class TestRemoteService:
    def test_connect_resolves_base_and_persists(self, ctx, remote_root):
        service = RemoteService(ctx)
        report = service.connect("cluster")
        assert report.resolved_base == str(remote_root)
        assert (remote_root / "receipts").is_dir()
        assert (remote_root / "locks").is_dir()
        assert Path(report.resolved_base, "runs").is_dir()
        stored = ctx.user_store.read_remote("cluster")
        assert stored.resolved_base == str(remote_root)
        assert EnvironmentCache(ctx.user_paths).observation(stored) is not None

    def test_add_requires_one_destination(self, ctx):
        with pytest.raises(UserError, match="exactly one"):
            RemoteService(ctx).add("bad", host=None, ssh_alias=None, base="/x")


class TestDoctor:
    def test_reports_each_potentially_slow_diagnosis_phase(self, ctx):
        events = []

        DoctorService(ctx).run(operation_sink=events.append)

        assert events[0].status is OperationStatus.STARTED
        assert events[0].phase is OperationPhase.PROBE
        assert events[0].message == "Checking local tools"
        assert any(
            event.phase is OperationPhase.CONNECT and event.message == "Resolving remote configuration"
            for event in events
        )
        assert any(
            event.phase is OperationPhase.PROBE and event.message.startswith("Probing cluster capabilities on ")
            for event in events
        )
        assert events[-1].status is OperationStatus.COMPLETED
        assert events[-1].phase is OperationPhase.VALIDATE
        assert events[-1].message == "Diagnosis complete"

    def test_reports_ok_for_working_setup(self, ctx):
        checks = {check.name: check for check in DoctorService(ctx).run()}
        assert checks["remote"].state == "OK"
        assert checks["connection"].state == "OK"
        assert checks["slurm"].state == "OK"
        assert checks["base"].state == "OK"
        assert checks["project"].state == "OK"
        assert checks["database"].state == "OK"

    def test_skip_reasons_are_specific(self, tmp_path, user_paths, fake_transport):
        from slurmdeck.services.context import AppContext

        empty_ctx = AppContext.create(cwd=tmp_path, user_paths=user_paths, transport_factory=lambda _r: fake_transport)
        # remove all remotes so resolution fails
        for name in empty_ctx.user_store.list_remote_names():
            empty_ctx.user_store.remove_remote(name)
        checks = {check.name: check for check in DoctorService(empty_ctx).run()}
        assert checks["remote"].state == "FAILED"
        assert checks["connection"].state == "SKIPPED"
        assert checks["connection"].detail == "no remote configured"  # not a misleading message
        assert checks["project"].state == "SKIPPED"
