from __future__ import annotations

from pathlib import Path

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.services.runs import RunService
from slurmdeck.services.snapshots import SnapshotService
from slurmdeck.transport import TransportError


def test_run_upload_failure_preserves_exact_phase_and_is_safely_retryable(
    ctx,
    remote,
    fake_transport,
) -> None:
    runs = RunService(ctx)
    row = runs.plan(
        command=CommandTemplateSpec(argv=["python3", "train.py"]),
        overrides=ResourceOverrides(),
        remote=remote,
    )
    project = ctx.require_project()
    SnapshotService().ensure(
        fake_transport,
        ctx.layout(remote),
        project.paths.root,
        project.config.sync,
    )
    fake_transport.script_call("upload", TransportError("injected run upload failure"))

    with pytest.raises(UserError) as raised:
        runs.submit(fake_transport, row.id)

    failed = runs.get(row.id)
    assert raised.value.error.code == "run_submit_failed"
    assert failed.state == "submit_failed"
    assert failed.submission_phase == "upload"
    assert project.paths.run_dir(row.id).is_dir()
    assert not Path(row.remote_root).exists()
    assert fake_transport.call_counts["helper:submit-run"] == 0

    submitted = runs.submit(fake_transport, row.id)
    assert submitted.state == "submitted"
    assert submitted.slurm_job_id == "999001"
