from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path
from types import SimpleNamespace

from slurmdeck.agent import protocol
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.services.runs import RunService
from slurmdeck.transport import ExecResult, parse_json_lines

AGENT = Path("src/slurmdeck/agent/agent.py").resolve()


def test_partial_clean_reports_exact_residue_and_is_idempotently_retryable(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    base = tmp_path / "remote"
    run_id = "run-1"
    token = "a" * 64
    run_dir = base / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "payload").write_text("data", encoding="utf-8")
    receipt = base / "receipts" / "run" / f"{token}.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps({"schema_version": 1, "token": token, "run_id": run_id, "status": "submitted"}),
        encoding="utf-8",
    )
    namespace = runpy.run_path(str(AGENT))
    command = namespace["cmd_clean_run"]
    real_unlink = command.__globals__["os"].unlink

    def fail_receipt_unlink(path, *args, **kwargs):
        if Path(path) == receipt:
            raise OSError("injected receipt cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(command.__globals__["os"], "unlink", fail_receipt_unlink)
    args = SimpleNamespace(base=str(base), run_id=run_id, token=token)
    assert command(args) == 0
    partial = parse_json_lines(capsys.readouterr().out)[-1]

    assert partial["kind"] == protocol.RUN_CLEAN_KIND
    assert partial["ok"] is False
    assert partial["removed_run"] is True
    assert partial["removed_receipt"] is False
    assert not run_dir.exists()
    assert receipt.is_file()

    monkeypatch.setattr(command.__globals__["os"], "unlink", real_unlink)
    assert command(args) == 0
    retried = parse_json_lines(capsys.readouterr().out)[-1]
    assert retried["ok"] is True
    assert retried["removed_run"] is False
    assert retried["removed_receipt"] is True
    assert not receipt.exists()


def test_run_service_preserves_local_retry_handle_and_reports_partial_remote_clean(
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
    row = runs.submit(fake_transport, row.id)
    shutil.rmtree(row.remote_root)
    partial_payload = {
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
        ExecResult(0, protocol.JSON_PREFIX + json.dumps(partial_payload) + "\n", ""),
    )

    partial = runs.clean(row.id, transport=fake_transport)

    assert partial.partial is True
    assert partial.local_removed is False
    assert partial.remote_removed is True
    assert partial.receipt_removed is False
    assert partial.snapshot_reference_released is False
    assert "receipt cleanup" in partial.error
    assert runs.get(row.id).id == row.id

    completed = runs.clean(row.id, transport=fake_transport)
    assert completed.partial is False
    assert completed.local_removed is True
    assert completed.remote_removed is True
    assert completed.receipt_removed is True
    assert completed.snapshot_reference_released is True
