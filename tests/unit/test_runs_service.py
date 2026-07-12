from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.operations import OperationPhase
from slurmdeck.services.doctor import DoctorService
from slurmdeck.services.run_materialization import RunMaterializer
from slurmdeck.services.run_planning import RunPlanner
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import StatusService
from slurmdeck.storage.repos import RunRepo, TaskRepo
from slurmdeck.structured_errors import StructuredError
from slurmdeck.transport.errors import RemoteTimeout

SWEEP = Sweep.model_validate(
    {
        "version": 1,
        "parameters": {"seed": [0, 1]},
        "config": {"model": "smoke", "seed": "{seed}"},
        "env": {"SEED": "{seed}"},
    }
)

COMMAND = CommandTemplateSpec(argv=["python3", "train.py", "--config", "{config}", "{args}"])


def _plan(ctx, remote, **kwargs):
    defaults = {"command": COMMAND, "sweep": SWEEP, "overrides": ResourceOverrides(time="00:30:00"), "remote": remote}
    defaults.update(kwargs)
    return RunService(ctx).plan(**defaults)


def test_plan_materializes_run_dir_and_db(ctx, remote):
    row = _plan(ctx, remote)
    assert row.state == "planned"
    assert row.resources.time == "00:30:00"  # override captured (retry will reuse it)
    assert row.summary.total == 2
    assert row.summary.counts == {"PENDING": 2}

    run_dir = ctx.require_project().paths.run_dir(row.id)
    tasks = [json.loads(line) for line in (run_dir / "tasks.jsonl").read_text().splitlines()]
    assert len(tasks) == 2
    assert tasks[0]["argv"][:2] == ["python3", "train.py"]
    # {config} resolved to an absolute remote path; {args} spliced from config-derived args
    assert tasks[0]["argv"][3].endswith(".yaml")
    assert "--model" in tasks[0]["argv"]
    assert tasks[0]["env"] == {"SEED": "0"}
    assert (run_dir / "agent.py").exists()
    assert (run_dir / "submit.sbatch").exists()
    assert (run_dir / "run.json").exists()
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["task_count"] == 2

    sbatch = (run_dir / "submit.sbatch").read_text()
    assert "#SBATCH --array=0-1" in sbatch
    assert "--time=00:30:00" in sbatch
    assert "agent.py" in sbatch
    assert "eval" not in sbatch


def test_submit_executes_tasks_and_status_refresh_sees_them(ctx, remote, fake_transport):
    runs = RunService(ctx)
    row = _plan(ctx, remote)
    row = runs.submit(fake_transport, row.id)
    assert row.state == "submitted"
    assert row.slurm_job_id == "999001"

    # the fake cluster ran both tasks via the real agent; artifacts exist remotely
    remote_run = Path(row.remote_root)
    status = json.loads((remote_run / "results/000/status.json").read_text())
    assert status["state"] == "COMPLETED"
    assert status["slurm_job_id"] == "999001_0"
    assert (remote_run / "results/000/done.txt").read_text() == "ok"

    report = StatusService(ctx).refresh(fake_transport, ctx.layout(remote), [row.id])
    assert report.changed >= 2
    summary = StatusService(ctx).summary(row.id)
    assert summary.counts == {"COMPLETED": 2}
    assert RunService(ctx).get(row.id).state == "terminal"


def test_submit_failure_is_retryable_with_a_new_token_and_keeps_run_dir(ctx, remote, fake_transport):
    runs = RunService(ctx)
    row = _plan(ctx, remote)
    fake_transport.sbatch_returncode = 9
    fake_transport.sbatch_stderr = "sbatch exploded"

    with pytest.raises(UserError) as raised:
        runs.submit(fake_transport, row.id)
    refreshed = RunService(ctx).get(row.id)
    assert raised.value.error.code == "run_submit_failed"
    assert refreshed.state == "submit_failed"
    assert len(refreshed.submission_token) == 64
    failed_token = refreshed.submission_token
    assert json.loads(refreshed.submission_error_json)["code"] == "run_submit_failed"
    assert ctx.require_project().paths.run_dir(row.id).exists()  # kept for post-mortem

    fake_transport.sbatch_returncode = 0
    fake_transport.sbatch_stderr = ""
    submitted = runs.submit(fake_transport, row.id)

    assert submitted.state == "submitted"
    assert submitted.submission_token != failed_token
    assert submitted.slurm_job_id == "999001"


def test_failure_before_remote_helper_starts_is_not_marked_unknown(ctx, remote, fake_transport):
    runs = RunService(ctx)
    row = _plan(ctx, remote)
    injected = False

    def fail_once_at_submit_feedback(event):
        nonlocal injected
        if event.phase is OperationPhase.SUBMIT and not injected:
            injected = True
            raise RuntimeError("injected feedback failure")

    with pytest.raises(UserError) as raised:
        runs.submit(fake_transport, row.id, operation_sink=fail_once_at_submit_feedback)

    assert raised.value.error.code == "run_submit_failed"
    assert runs.get(row.id).state == "submit_failed"
    assert not any(call.startswith("python3 - submit-run") for call in fake_transport.calls)


def test_unknown_submission_cannot_resubmit_and_reconciles_from_receipt(
    ctx,
    remote,
    fake_transport,
    monkeypatch,
):
    runs = RunService(ctx)
    row = _plan(ctx, remote)
    real_exec_python = fake_transport.exec_python

    def lose_response(script, args=(), *, timeout=60.0, check=True):
        result = real_exec_python(script, args, timeout=timeout, check=check)
        if args and args[0] == "submit-run":
            raise RemoteTimeout("lost response after remote submission")
        return result

    monkeypatch.setattr(fake_transport, "exec_python", lose_response)
    with pytest.raises(UserError) as raised:
        runs.submit(fake_transport, row.id)

    unknown = runs.get(row.id)
    assert raised.value.error.code == "run_submit_unknown"
    assert unknown.state == "submit_unknown"
    assert unknown.submission_phase == "submit"
    assert len(unknown.submission_token) == 64
    receipt = Path(ctx.layout(remote).run_submission_receipt(unknown.submission_token))
    assert json.loads(receipt.read_text(encoding="utf-8"))["job_id"] == "999001"

    calls_before = list(fake_transport.calls)
    with pytest.raises(UserError) as refused:
        runs.submit(fake_transport, row.id)
    assert refused.value.error.code == "run_submit_unknown"
    assert fake_transport.calls == calls_before

    monkeypatch.setattr(fake_transport, "exec_python", real_exec_python)
    reconciled = runs.reconcile(fake_transport, row.id)

    assert reconciled.state == "submitted"
    assert reconciled.slurm_job_id == "999001"
    assert reconciled.submission_token == unknown.submission_token
    assert fake_transport.next_job_id == 999002


def test_submission_reconcile_preserves_local_corruption_and_never_calls_remote(ctx, remote, fake_transport):
    runs = RunService(ctx)
    target = _plan(ctx, remote)
    token = "a" * 64
    repo = RunRepo(ctx.db())
    assert repo.begin_submission(target.id, token=token, phase="submit")
    assert repo.record_submission_error(
        target.id,
        token=token,
        state="submit_unknown",
        phase="submit",
        error=StructuredError(code="run_submit_unknown", summary="unknown"),
    )
    corrupt = _plan(ctx, remote)
    shutil.rmtree(ctx.require_project().paths.run_dir(corrupt.id))
    calls_before = list(fake_transport.calls)

    with pytest.raises(UserError) as raised:
        runs.reconcile(fake_transport, target.id)

    assert raised.value.error.code == "run_state_corrupt"
    assert fake_transport.calls == calls_before
    assert runs.get(target.id).state == "submit_unknown"


def test_retry_reuses_resources_and_repoints_paths(ctx, remote, fake_transport):
    runs = RunService(ctx)
    fake_transport.fail_task_indices = {1}
    row = runs.submit(fake_transport, _plan(ctx, remote).id)
    # make task 1 fail for real: rerun with a failing command is complex, so
    # instead mark it failed via a broken status file the agent would produce
    remote_run = Path(row.remote_root)
    failed = json.loads((remote_run / "results/001/status.json").read_text())
    failed.update({"state": "FAILED", "exit_code": 1, "reason": "boom"})
    (remote_run / "results/001/status.json").write_text(json.dumps(failed))

    StatusService(ctx).refresh(fake_transport, ctx.layout(remote), [row.id], force=True)
    retry_row = runs.retry(row.id)
    assert retry_row.retry_of == row.id
    assert retry_row.resources.time == "00:30:00"  # original override survived

    retry_dir = ctx.require_project().paths.run_dir(retry_row.id)
    tasks = [json.loads(line) for line in (retry_dir / "tasks.jsonl").read_text().splitlines()]
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "001"  # original id preserved
    config_arg = tasks[0]["argv"][3]
    assert f"/runs/{retry_row.id}/" in config_arg  # {config} re-resolved against the NEW run root
    assert f"/runs/{row.id}/" not in config_arg


def test_retry_without_failures_errors(ctx, remote, fake_transport):
    runs = RunService(ctx)
    row = runs.submit(fake_transport, _plan(ctx, remote).id)
    StatusService(ctx).refresh(fake_transport, ctx.layout(remote), [row.id])
    with pytest.raises(UserError, match="no failed tasks"):
        runs.retry(row.id)


def test_retry_rejects_an_unsafe_stored_config_path_before_reading_or_materializing(ctx, remote, tmp_path):
    runs = RunService(ctx)
    source = _plan(ctx, remote)
    outside = tmp_path / "outside.yaml"
    outside.write_text("value: unsafe\n", encoding="utf-8")
    with ctx.db():
        ctx.db().execute(
            "UPDATE tasks SET config_rel = '../outside.yaml' WHERE run_id = ? AND task_id = '000'",
            (source.id,),
        )
    rows_before = [row.id for row in runs.list_runs()]

    with pytest.raises(UserError, match="Unsafe rendered run path"):
        runs.retry(source.id, task_ids=["000"])

    assert [row.id for row in runs.list_runs()] == rows_before
    assert not ctx.require_project().paths.run_staging_dir.exists()


def test_cancel_and_clean(ctx, remote, fake_transport):
    runs = RunService(ctx)
    row = runs.submit(fake_transport, _plan(ctx, remote).id)
    cancelled = runs.cancel(fake_transport, row.id)
    assert cancelled.state == "cancelled"
    assert any(call.startswith("scancel 999001") for call in fake_transport.calls)

    runs.clean(row.id, transport=fake_transport)
    assert RunService(ctx).list_runs() == []
    assert not Path(row.remote_root).exists()
    assert not ctx.require_project().paths.run_dir(row.id).exists()


def test_sweep_args_without_placeholder_is_rejected(ctx, remote):
    command = CommandTemplateSpec(argv=["python3", "train.py"])  # no {args}, no {config}
    with pytest.raises(UserError, match="never uses"):
        RunService(ctx).plan(command=command, sweep=SWEEP, remote=remote)


def test_submit_requires_unchanged_working_tree(ctx, remote, fake_transport, project_dir):
    row = _plan(ctx, remote)
    (project_dir / "train.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(UserError, match="changed since"):
        RunService(ctx).submit(fake_transport, row.id)
    assert RunService(ctx).get(row.id).state == "submit_failed"


def test_plan_delegates_to_pure_planner_and_atomic_materializer(ctx, remote, monkeypatch):
    planner_calls = []
    materializer_calls = []
    real_plan = RunPlanner.plan
    real_commit = RunMaterializer.commit

    def plan_spy(self, **kwargs):
        result = real_plan(self, **kwargs)
        planner_calls.append(result)
        return result

    def commit_spy(self, plan):
        materializer_calls.append(plan)
        return real_commit(self, plan)

    monkeypatch.setattr(RunPlanner, "plan", plan_spy)
    monkeypatch.setattr(RunMaterializer, "commit", commit_spy)

    row = _plan(ctx, remote)

    assert len(planner_calls) == 1
    assert materializer_calls == planner_calls
    assert ctx.require_project().paths.run_commit_marker(row.id).is_file()


def test_retry_delegates_to_the_same_planner_and_materializer_with_stored_inputs(ctx, remote, monkeypatch):
    runs = RunService(ctx)
    source = _plan(ctx, remote, sweep_file="sweeps/smoke.yaml")
    source_records = {record.spec.task_id: record for record in TaskRepo(ctx.db()).planned_records(source.id)}
    planner_calls = []
    materializer_calls = []
    real_retry = RunPlanner.retry
    real_commit = RunMaterializer.commit

    def retry_spy(self, **kwargs):
        result = real_retry(self, **kwargs)
        planner_calls.append((kwargs, result))
        return result

    def commit_spy(self, plan):
        materializer_calls.append(plan)
        return real_commit(self, plan)

    monkeypatch.setattr(RunPlanner, "retry", retry_spy)
    monkeypatch.setattr(RunMaterializer, "commit", commit_spy)

    retry = runs.retry(source.id, task_ids=["001"])

    assert len(planner_calls) == 1
    assert materializer_calls == [planner_calls[0][1]]
    assert retry.retry_of == source.id
    assert retry.resources == source.resources
    assert retry.command == source.command
    assert retry.sweep_file == source.sweep_file
    retry_record = TaskRepo(ctx.db()).planned_records(retry.id)[0]
    source_record = source_records["001"]
    assert retry_record.spec.task_id == source_record.spec.task_id
    assert retry_record.params == source_record.params
    assert retry_record.args_template == source_record.args_template
    assert retry_record.env_template == source_record.env_template
    assert retry_record.arg_style == source_record.arg_style


@pytest.mark.parametrize("mutation", ["plan", "retry", "submit", "cancel", "clean"])
def test_every_run_mutation_reconciles_before_its_first_side_effect(ctx, remote, fake_transport, mutation):
    runs = RunService(ctx)
    target = _plan(ctx, remote)
    corrupt = _plan(ctx, remote)
    shutil.rmtree(ctx.require_project().paths.run_dir(corrupt.id))
    if mutation == "cancel":
        RunRepo(ctx.db()).record_submission(target.id, slurm_job_id="42", snapshot_hash="hash", env_id="")
    calls_before = list(fake_transport.calls)

    def mutate():
        if mutation == "plan":
            return runs.plan(
                command=CommandTemplateSpec(argv=["python3", "train.py"]),
                overrides=ResourceOverrides(),
                remote=remote,
            )
        if mutation == "retry":
            return runs.retry(target.id, task_ids=["000"])
        if mutation == "submit":
            return runs.submit(fake_transport, target.id)
        if mutation == "cancel":
            return runs.cancel(fake_transport, target.id)
        return runs.clean(target.id)

    with pytest.raises(UserError) as raised:
        mutate()

    assert raised.value.error.code == "run_state_corrupt", raised.value.error
    assert ctx.require_project().paths.run_dir(target.id).is_dir()
    assert fake_transport.calls == calls_before


def test_run_reads_and_doctor_never_reconcile_repairable_state(ctx, remote):
    row = _plan(ctx, remote)
    paths = ctx.require_project().paths
    marker = paths.run_commit_marker(row.id)
    marker.unlink()
    stale = paths.run_staging_wrapper("stale-read-check")
    (stale / "attempt-dead" / "run").mkdir(parents=True)
    lock = paths.run_materialization_lock("stale-read-check")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()

    assert RunService(ctx).get(row.id).id == row.id
    assert [stored.id for stored in RunService(ctx).list_runs()] == [row.id]
    DoctorService(ctx).run()

    assert not marker.exists()
    assert stale.is_dir()
