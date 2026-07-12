from __future__ import annotations

import json
import subprocess
import threading

import pytest

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.status import RunSummary
from slurmdeck.models.sweep import Sweep
from slurmdeck.operations import OperationPhase
from slurmdeck.services.run_recovery import RunRecoveryService
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import StatusService
from slurmdeck.storage.db import connect
from slurmdeck.storage.repos import RunRepo, TaskRepo
from slurmdeck.transport import ExecResult
from slurmdeck.transport.errors import TransportError
from slurmdeck.transport.ssh import SshTransport

SWEEP = Sweep.model_validate({"version": 1, "parameters": {"seed": [0, 1, 2]}})
COMMAND = CommandTemplateSpec(argv=["python3", "-c", "print(1)"])


def _submitted_run(ctx, remote, fake_transport, *, sweep=SWEEP, simulate_execution=True):
    fake_transport.simulate_execution = simulate_execution
    runs = RunService(ctx)
    row = runs.plan(command=COMMAND, sweep=sweep, overrides=ResourceOverrides(), remote=remote)
    return runs.submit(fake_transport, row.id)


class TestRefresh:
    def test_reconciles_interrupted_run_commits_before_remote_refresh(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
    ):
        row = _submitted_run(ctx, remote, fake_transport)
        fake_transport.calls.clear()
        events = []

        def fail_reconcile(_service):
            raise UserError("injected recovery failure")

        monkeypatch.setattr(RunRecoveryService, "reconcile", fail_reconcile)

        with pytest.raises(UserError, match="injected recovery failure"):
            StatusService(ctx).refresh(
                fake_transport,
                ctx.layout(remote),
                [row.id],
                operation_sink=events.append,
            )

        assert fake_transport.calls == []
        assert events[0].phase is OperationPhase.RECONCILE
        assert events[-1].phase is OperationPhase.RECONCILE

    def test_exactly_one_remote_call_for_any_number_of_runs(self, ctx, remote, fake_transport):
        first = _submitted_run(ctx, remote, fake_transport)
        second = _submitted_run(ctx, remote, fake_transport)
        fake_transport.calls.clear()

        StatusService(ctx).refresh(fake_transport, ctx.layout(remote), [first.id, second.id])

        remote_calls = list(fake_transport.calls)
        assert len(remote_calls) == 1  # the agent queries squeue/sacct on the cluster side
        assert remote_calls[0].startswith("python3 - scan")
        # both job ids batched into the single scan's --jobs argument
        assert f"{first.slurm_job_id},{second.slurm_job_id}" in remote_calls[0]

    def test_scheduler_live_state_is_authoritative_over_artifact_state(self, ctx, remote, fake_transport):
        row = _submitted_run(ctx, remote, fake_transport)
        job = row.slurm_job_id
        fake_transport.squeue_output = f"{job}_0|RUNNING|node1\n"
        StatusService(ctx).refresh(fake_transport, ctx.layout(remote), [row.id])
        rows = {item.task_id: item for item in StatusService(ctx).rows(row.id)}
        assert rows["000"].scheduler_state == "RUNNING"
        assert rows["000"].local_state == "COMPLETED"
        assert rows["000"].effective_state == "RUNNING"

    def test_snapshot_owns_precedence_reasons_summary_and_sources(self, ctx, remote, fake_transport):
        sweep = Sweep.model_validate({"version": 1, "parameters": {"seed": [0, 1, 2, 3, 4]}})
        row = _submitted_run(ctx, remote, fake_transport, sweep=sweep, simulate_execution=False)
        TaskRepo(ctx.db()).apply_artifact(
            row.id,
            [
                {"task_id": "000", "state": "FAILED", "exit_code": 3, "reason": "python failed"},
                {"task_id": "001", "state": "FAILED", "exit_code": 4, "reason": "old local failure"},
                {"task_id": "002", "state": "FAILED", "exit_code": 5, "reason": "artifact failure"},
                {"task_id": "003", "state": "RUNNING", "exit_code": None, "reason": ""},
            ],
        )
        job = row.slurm_job_id
        fake_transport.squeue_output = f"{job}_0|RUNNING|Resources\n{job}_1|PENDING|Priority\n"
        fake_transport.sacct_output = f"{job}_0|TIMEOUT|0:9|TimeLimit\n"

        StatusService(ctx).refresh(fake_transport, ctx.layout(remote), [row.id])
        snapshot = StatusService(ctx).snapshot(row.id)
        tasks = {task.task_id: task for task in snapshot.tasks}

        assert tasks["000"].local_state == "FAILED"
        assert tasks["000"].scheduler_state == "TIMEOUT"
        assert tasks["000"].effective_state == "TIMEOUT"
        assert tasks["000"].scheduler_reason == "TimeLimit"
        assert tasks["000"].failure_reason == "python failed"
        assert tasks["000"].display_reason == "python failed"
        assert tasks["000"].exit_code == "0:9"
        assert tasks["000"].observed_at is not None

        assert tasks["001"].effective_state == "PENDING"
        assert tasks["001"].scheduler_reason == "Priority"
        assert tasks["001"].failure_reason == "old local failure"
        assert tasks["001"].display_reason == "Priority"
        assert tasks["002"].effective_state == "FAILED"
        assert tasks["002"].display_reason == "artifact failure"
        assert tasks["003"].effective_state == "RUNNING"
        assert tasks["003"].display_reason is None
        assert tasks["004"].effective_state == "PENDING"

        assert snapshot.summary.counts == {"FAILED": 1, "PENDING": 2, "RUNNING": 1, "TIMEOUT": 1}
        assert [source.value for source in snapshot.sources] == ["squeue", "sacct"]
        assert snapshot.refreshed_at is not None
        assert snapshot.is_stale is False
        assert all(task.is_stale is False for task in snapshot.tasks)

    def test_watermark_skips_unchanged_scans(self, ctx, remote, fake_transport):
        row = _submitted_run(ctx, remote, fake_transport)
        service = StatusService(ctx)
        first = service.refresh(fake_transport, ctx.layout(remote), [row.id])
        assert first.changed >= 3
        second = service.refresh(fake_transport, ctx.layout(remote), [row.id])
        assert second.changed == 0  # nothing re-applied thanks to --since watermark

    def test_planned_runs_are_never_refreshed(self, ctx, remote, fake_transport):
        runs = RunService(ctx)
        row = runs.plan(command=COMMAND, sweep=SWEEP, overrides=ResourceOverrides(), remote=remote)
        fake_transport.calls.clear()
        service = StatusService(ctx)
        report = service.refresh(fake_transport, ctx.layout(remote))
        snapshot = service.snapshot(row.id)

        assert report.refreshed == []
        assert fake_transport.calls == []
        assert snapshot.summary.counts == {"PENDING": 3}
        assert snapshot.summary.total == 3
        assert snapshot.refreshed_at is None
        assert snapshot.is_stale is False
        assert all(task.effective_state == "PENDING" for task in snapshot.tasks)

    def test_transport_failure_preserves_last_good_snapshot_as_stale(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        fake_transport.squeue_output = f"{row.slurm_job_id}_0|PENDING|Priority\n"
        service = StatusService(ctx)
        service.refresh(fake_transport, ctx.layout(remote), [row.id])
        good = service.snapshot(row.id)

        def transport_failure(*_args, **_kwargs):
            raise TransportError("connection lost")

        monkeypatch.setattr(fake_transport, "exec_python", transport_failure)
        report = service.refresh(fake_transport, ctx.layout(remote), [row.id])
        stale = service.snapshot(row.id)

        assert report.refreshed == []
        assert report.is_stale is True
        assert report.refresh_error is not None
        assert report.refresh_error.context["source"] == "transport"
        assert stale.tasks[0].scheduler_state == good.tasks[0].scheduler_state
        assert stale.tasks[0].scheduler_reason == "Priority"
        assert stale.summary == good.summary
        assert stale.refreshed_at == good.refreshed_at
        assert stale.is_stale is True
        assert all(task.is_stale is True for task in stale.tasks)
        assert stale.refresh_failed_at is not None
        assert stale.refresh_error is not None
        assert stale.refresh_error.operation == "status.refresh"
        assert stale.refresh_error.phase is OperationPhase.REFRESH
        assert stale.refresh_error.retryable is True
        assert stale.refresh_error.context["source"] == "transport"

    def test_ssh_launch_failure_preserves_last_good_snapshot_as_stale(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
        tmp_path,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        service = StatusService(ctx)
        service.refresh(fake_transport, ctx.layout(remote), [row.id])
        good = service.snapshot(row.id)

        def missing_ssh(*_args, **_kwargs):
            raise FileNotFoundError("ssh executable missing")

        monkeypatch.setattr(subprocess, "run", missing_ssh)
        transport = SshTransport(remote, control_dir=tmp_path / "cm")
        report = service.refresh(transport, ctx.layout(remote), [row.id])
        stale = service.snapshot(row.id)

        assert report.is_stale is True
        assert report.refresh_error is not None
        assert report.refresh_error.context["source"] == "transport"
        assert "Could not launch ssh" in str(report.refresh_error.context["error"])
        assert stale.summary == good.summary
        assert stale.refreshed_at == good.refreshed_at
        assert stale.is_stale is True

    def test_first_ssh_launch_failure_raises_structured_transport_status_error(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
        tmp_path,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)

        def missing_ssh(*_args, **_kwargs):
            raise PermissionError("ssh is not executable")

        monkeypatch.setattr(subprocess, "run", missing_ssh)
        transport = SshTransport(remote, control_dir=tmp_path / "cm")
        service = StatusService(ctx)

        with pytest.raises(UserError) as caught:
            service.refresh(transport, ctx.layout(remote), [row.id])

        error = caught.value.error
        assert error.operation == "status.refresh"
        assert error.phase is OperationPhase.REFRESH
        assert error.retryable is True
        assert error.context["source"] == "transport"
        assert "Could not launch ssh" in str(error.context["error"])
        failed = service.snapshot(row.id)
        assert failed.is_stale is True
        assert failed.refresh_error == error

    def test_scheduler_query_failure_preserves_last_good_snapshot_as_stale(self, ctx, remote, fake_transport):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        fake_transport.squeue_output = f"{row.slurm_job_id}_0|PENDING|Priority\n"
        service = StatusService(ctx)
        service.refresh(fake_transport, ctx.layout(remote), [row.id])
        good = service.snapshot(row.id)

        fake_transport.squeue_output = ""
        fake_transport.squeue_stderr = "slurmctld unavailable"
        fake_transport.squeue_returncode = 7
        report = service.refresh(fake_transport, ctx.layout(remote), [row.id])
        stale = service.snapshot(row.id)

        assert report.refreshed == []
        assert report.is_stale is True
        assert report.refresh_error is not None
        assert stale.summary == good.summary
        assert stale.tasks[0].scheduler_reason == "Priority"
        assert stale.is_stale is True
        assert stale.refresh_error is not None
        assert stale.refresh_error.context["source"] == "squeue"
        assert "slurmctld unavailable" in str(stale.refresh_error.context["error"])

    def test_legacy_squeue_invalid_job_is_an_empty_queue_when_sacct_has_terminal_state(
        self,
        ctx,
        remote,
        fake_transport,
    ):
        row = _submitted_run(ctx, remote, fake_transport, sweep=None, simulate_execution=False)
        fake_transport.squeue_output = ""
        fake_transport.squeue_stderr = "slurm_load_jobs error: Invalid job id specified\n"
        fake_transport.squeue_returncode = 1
        fake_transport.sacct_output = f"{row.slurm_job_id}_0|COMPLETED|0:0|None\n"

        report = StatusService(ctx).refresh(fake_transport, ctx.layout(remote), [row.id])
        snapshot = StatusService(ctx).snapshot(row.id)

        assert report.is_stale is False
        assert snapshot.is_stale is False
        assert snapshot.summary.counts == {"COMPLETED": 1}
        assert snapshot.tasks[0].effective_state == "COMPLETED"
        assert snapshot.tasks[0].scheduler_reason == ""
        assert snapshot.tasks[0].display_reason is None
        assert RunService(ctx).get(row.id).state == "terminal"

    def test_first_scheduler_query_failure_is_structured_and_recovery_clears_it(
        self,
        ctx,
        remote,
        fake_transport,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        fake_transport.sacct_stderr = "accounting database unavailable"
        fake_transport.sacct_returncode = 2
        service = StatusService(ctx)

        with pytest.raises(UserError) as caught:
            service.refresh(fake_transport, ctx.layout(remote), [row.id])

        error = caught.value.error
        assert error.operation == "status.refresh"
        assert error.phase is OperationPhase.REFRESH
        assert error.retryable is True
        assert error.context["source"] == "sacct"
        failed = service.snapshot(row.id)
        assert failed.refreshed_at is None
        assert failed.refresh_failed_at is not None
        assert failed.refresh_error == error
        assert failed.is_stale is True

        fake_transport.sacct_stderr = ""
        fake_transport.sacct_returncode = 0
        service.refresh(fake_transport, ctx.layout(remote), [row.id])
        recovered = service.snapshot(row.id)
        assert recovered.is_stale is False
        assert recovered.refresh_failed_at is None
        assert recovered.refresh_error is None

    def test_nonzero_agent_exit_uses_check_false_and_persists_agent_context(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        service = StatusService(ctx)
        service.refresh(fake_transport, ctx.layout(remote), [row.id])
        called: dict[str, object] = {}

        def failed_agent(_script, _args=(), *, timeout=60.0, check=True):
            called.update(timeout=timeout, check=check)
            return ExecResult(23, "partial output", "agent crashed")

        monkeypatch.setattr(fake_transport, "exec_python", failed_agent)
        report = service.refresh(fake_transport, ctx.layout(remote), [row.id])
        snapshot = service.snapshot(row.id)

        assert called == {"timeout": 300, "check": False}
        assert report.is_stale is True
        assert report.refresh_error is not None
        assert report.refresh_error.context == {
            "source": "agent",
            "error": "agent crashed",
            "returncode": 23,
            "stderr": "agent crashed",
        }
        assert snapshot.refresh_error == report.refresh_error

    def test_programming_error_is_not_recorded_as_a_stale_remote_failure(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        service = StatusService(ctx)
        service.refresh(fake_transport, ctx.layout(remote), [row.id])

        def bug(*_args, **_kwargs):
            raise RuntimeError("programming bug")

        monkeypatch.setattr(fake_transport, "exec_python", bug)
        with pytest.raises(RuntimeError, match="programming bug"):
            service.refresh(fake_transport, ctx.layout(remote), [row.id])

        snapshot = service.snapshot(row.id)
        assert snapshot.is_stale is False
        assert snapshot.refresh_error is None

    def test_snapshot_reads_run_metadata_and_tasks_from_one_sqlite_snapshot(
        self,
        ctx,
        remote,
        fake_transport,
        monkeypatch,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        writer_may_commit = threading.Event()
        writer_committed = threading.Event()
        writer_errors: list[BaseException] = []
        original_status_records = TaskRepo.status_records

        def interleaved_status_records(repo: TaskRepo, run_id: str):
            writer_may_commit.set()
            assert writer_committed.wait(timeout=5)
            return original_status_records(repo, run_id)

        monkeypatch.setattr(TaskRepo, "status_records", interleaved_status_records)

        def write_new_snapshot() -> None:
            try:
                assert writer_may_commit.wait(timeout=5)
                writer = connect(ctx.require_project().paths.db_path)
                with writer:
                    writer.execute(
                        """
                        UPDATE tasks
                        SET scheduler_state = 'RUNNING', scheduler_observed_at = 200
                        WHERE run_id = ? AND task_id = '000'
                        """,
                        (row.id,),
                    )
                    writer.execute(
                        "UPDATE runs SET summary_json = ? WHERE id = ?",
                        (RunSummary(total=3, counts={"RUNNING": 1, "PENDING": 2}).model_dump_json(), row.id),
                    )
                writer.close()
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                writer_committed.set()

        writer = threading.Thread(target=write_new_snapshot)
        writer.start()
        snapshot = StatusService(ctx).snapshot(row.id)
        writer.join(timeout=5)

        assert not writer.is_alive()
        assert writer_errors == []
        assert snapshot.summary.counts == {"PENDING": 3}
        assert snapshot.tasks[0].effective_state == "PENDING"
        updated = StatusService(ctx).snapshot(row.id)
        assert updated.summary.counts == {"RUNNING": 1, "PENDING": 2}
        assert updated.tasks[0].effective_state == "RUNNING"

    def test_out_of_order_concurrent_refresh_cannot_regress_terminal_facts_or_watermark(
        self,
        ctx,
        remote,
        fake_transport,
    ):
        row = _submitted_run(ctx, remote, fake_transport, simulate_execution=False)
        older_started = threading.Event()
        release_older = threading.Event()
        errors: list[BaseException] = []

        def encoded_scan(*payloads: dict[str, object]) -> str:
            return "".join(f"{protocol.JSON_PREFIX}{json.dumps(payload)}\n" for payload in payloads)

        def scheduler(kind: str, observed_at: float, output: str) -> dict[str, object]:
            return {
                "kind": kind,
                "source": kind,
                "observed_at": observed_at,
                "returncode": 0,
                "stderr": "",
                "error": "",
                "output": output,
            }

        newer_stdout = encoded_scan(
            scheduler(protocol.SCAN_KIND_SQUEUE, 200.0, ""),
            scheduler(protocol.SCAN_KIND_SACCT, 200.0, f"{row.slurm_job_id}_0|COMPLETED|0:0|None\n"),
            {
                "kind": protocol.SCAN_KIND_TASK,
                "run_id": row.id,
                "task_id": "000",
                "state": "COMPLETED",
                "exit_code": 0,
                "reason": "",
                "mtime": 200.0,
            },
        )
        older_stdout = encoded_scan(
            scheduler(
                protocol.SCAN_KIND_SQUEUE,
                100.0,
                f"{row.slurm_job_id}_0|RUNNING|node01\n",
            ),
            scheduler(protocol.SCAN_KIND_SACCT, 100.0, ""),
            {
                "kind": protocol.SCAN_KIND_TASK,
                "run_id": row.id,
                "task_id": "000",
                "state": "RUNNING",
                "exit_code": None,
                "reason": "",
                "mtime": 100.0,
            },
        )

        class ScanTransport:
            def __init__(self, stdout: str, *, block: bool = False) -> None:
                self.stdout = stdout
                self.block = block

            def exec_python(self, _script, _args=(), *, timeout=60.0, check=True):
                assert timeout == 300
                if self.block:
                    older_started.set()
                    assert release_older.wait(timeout=5)
                return ExecResult(0, self.stdout, "")

        def apply_older() -> None:
            try:
                StatusService(ctx).refresh(ScanTransport(older_stdout, block=True), ctx.layout(remote), [row.id])
            except BaseException as exc:
                errors.append(exc)

        older = threading.Thread(target=apply_older)
        older.start()
        assert older_started.wait(timeout=5)
        StatusService(ctx).refresh(ScanTransport(newer_stdout), ctx.layout(remote), [row.id])
        release_older.set()
        older.join(timeout=5)

        assert not older.is_alive()
        assert errors == []
        snapshot = StatusService(ctx).snapshot(row.id)
        stored_run = RunRepo(ctx.db()).get(row.id)
        stored_task = TaskRepo(ctx.db()).status_records(row.id)[0]
        assert stored_run is not None
        assert snapshot.tasks[0].scheduler_state == "COMPLETED"
        assert snapshot.tasks[0].local_state == "COMPLETED"
        assert snapshot.summary.counts == {"COMPLETED": 1, "PENDING": 2}
        assert stored_task.scheduler_observed_at == 200.0
        assert stored_task.artifact_observed_at == 200.0
        assert stored_run.scan_watermark == 200.0
