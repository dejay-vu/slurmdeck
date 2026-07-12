from __future__ import annotations

from pathlib import Path

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.project import SyncConfig
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.operations import OperationEvent, OperationPhase, OperationStatus
from slurmdeck.services.results import ResultsService
from slurmdeck.services.runs import RunService
from slurmdeck.services.snapshots import SnapshotService
from slurmdeck.services.status import StatusService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import SyncStats

COMMAND = CommandTemplateSpec(argv=["python3", "train.py"])


def _assert_typed(events: list[OperationEvent]) -> None:
    assert events
    assert all(isinstance(event, OperationEvent) for event in events)


def test_run_submit_emits_typed_events_for_owned_and_nested_operations(ctx, remote, fake_transport) -> None:
    runs = RunService(ctx)
    row = runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote)
    events: list[OperationEvent] = []

    submitted = runs.submit(fake_transport, row.id, operation_sink=events.append)

    _assert_typed(events)
    assert events[0].operation == "run.submit"
    assert events[0].status is OperationStatus.STARTED
    assert any(event.operation == "snapshot.ensure" for event in events)
    assert {event.phase for event in events if event.operation == "run.submit"} >= {
        OperationPhase.SNAPSHOT,
        OperationPhase.UPLOAD,
        OperationPhase.SUBMIT,
    }
    assert events[-1].operation == "run.submit"
    assert events[-1].status is OperationStatus.COMPLETED
    assert events[-1].result_counts == {"submitted": 1}
    assert submitted.state == "submitted"


def test_run_plan_reports_reconciliation_as_a_typed_phase(ctx, remote) -> None:
    events: list[OperationEvent] = []

    row = RunService(ctx).plan(
        command=COMMAND,
        overrides=ResourceOverrides(),
        remote=remote,
        operation_sink=events.append,
    )

    _assert_typed(events)
    assert any(event.phase is OperationPhase.RECONCILE for event in events)
    assert events[-1].status is OperationStatus.COMPLETED
    assert events[-1].result_counts == {"tasks": row.summary.total}


def test_run_submit_unknown_run_fails_during_validation(ctx, fake_transport) -> None:
    events: list[OperationEvent] = []

    with pytest.raises(UserError, match="Unknown run"):
        RunService(ctx).submit(fake_transport, "missing", operation_sink=events.append)

    assert events[-1].status is OperationStatus.FAILED
    assert events[-1].phase is OperationPhase.VALIDATE


def test_run_submit_non_planned_run_fails_during_validation(ctx, remote, fake_transport) -> None:
    runs = RunService(ctx)
    row = runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote)
    runs.submit(fake_transport, row.id)
    events: list[OperationEvent] = []

    with pytest.raises(UserError, match="only planned or failed submissions"):
        runs.submit(fake_transport, row.id, operation_sink=events.append)

    assert events[-1].status is OperationStatus.FAILED
    assert events[-1].phase is OperationPhase.VALIDATE


def test_snapshot_ensure_emits_probe_upload_and_completion_events(tmp_path, fake_transport, remote_root) -> None:
    project = tmp_path / "snapshot-project"
    project.mkdir()
    (project / "train.py").write_text("print(1)\n", encoding="utf-8")
    events: list[OperationEvent] = []

    snapshot = SnapshotService().ensure(
        fake_transport,
        layout=RemoteLayout(str(remote_root)),
        project_dir=project,
        sync=SyncConfig(),
        operation_sink=events.append,
    )

    _assert_typed(events)
    assert [(events[0].operation, events[0].status)] == [("snapshot.ensure", OperationStatus.STARTED)]
    assert OperationPhase.PROBE in {event.phase for event in events}
    assert OperationPhase.UPLOAD in {event.phase for event in events}
    assert events[-1].status is OperationStatus.COMPLETED
    assert snapshot.reused is False


def test_status_refresh_emits_typed_events_with_result_counts(ctx, remote, fake_transport) -> None:
    runs = RunService(ctx)
    row = runs.submit(
        fake_transport,
        runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote).id,
    )
    events: list[OperationEvent] = []

    report = StatusService(ctx).refresh(
        fake_transport,
        ctx.layout(remote),
        [row.id],
        operation_sink=events.append,
    )

    _assert_typed(events)
    assert events[0].status is OperationStatus.STARTED
    assert events[-1].status is OperationStatus.COMPLETED
    assert events[-1].result_counts == {"refreshed": 1, "changed": report.changed}
    assert all(event.operation == "status.refresh" for event in events)


def test_results_pull_emits_typed_download_events(ctx, remote, fake_transport, tmp_path: Path) -> None:
    runs = RunService(ctx)
    row = runs.submit(
        fake_transport,
        runs.plan(command=COMMAND, overrides=ResourceOverrides(), remote=remote).id,
    )
    events: list[OperationEvent] = []

    fake_transport.download = lambda *_args, **_kwargs: SyncStats(  # type: ignore[method-assign]
        transferred=True,
        matched_files=5,
        transferred_files=2,
        skipped_files=2,
        failed_files=1,
        bytes_transferred=2048,
        failed_paths=(f"{row.remote_root}/results/003/output.txt",),
        returncode=23,
    )
    report = ResultsService(ctx).pull(fake_transport, row, into=tmp_path / "pulled", operation_sink=events.append)

    _assert_typed(events)
    assert [(event.operation, event.phase, event.status) for event in events] == [
        ("results.pull", OperationPhase.DOWNLOAD, OperationStatus.STARTED),
        ("results.pull", OperationPhase.DOWNLOAD, OperationStatus.PROGRESS),
        ("results.pull", OperationPhase.DOWNLOAD, OperationStatus.COMPLETED),
    ]
    assert report.destination == tmp_path / "pulled"
    assert (report.matched, report.transferred, report.skipped, report.failed, report.bytes) == (5, 2, 2, 1, 2048)
    assert report.failed_paths == ("results/003/output.txt",)
    assert events[-1].result_counts == {"matched": 5, "transferred": 2, "skipped": 2, "failed": 1}
