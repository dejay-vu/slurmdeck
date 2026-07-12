from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.project import SyncConfig
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.services import snapshots as snapshots_module
from slurmdeck.services.runs import RunService
from slurmdeck.services.snapshots import SnapshotService, select_files
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.storage.repos import RunRepo
from slurmdeck.structured_errors import StructuredError


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "train.py").write_text("print(1)\n")
    (project / "pkg" / "util.py").write_text("x = 1\n")
    (project / ".slurmdeck").mkdir()
    (project / ".slurmdeck" / "junk.db").write_text("db")
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "x.pyc").write_text("junk")
    return project


class TestSelection:
    def test_default_ignores(self, tmp_path):
        project = _project(tmp_path)
        files = select_files(project, SyncConfig())
        assert files == ["pkg/util.py", "train.py"]

    def test_pull_destination_never_enters_the_snapshot(self, tmp_path):
        # regression: pulling into <project>/pulled used to bloat the next
        # snapshot (and change its hash) with the run's own results
        project = _project(tmp_path)
        baseline = SnapshotService().compute(project, SyncConfig())[0]
        results = project / "pulled" / "run-1" / "results" / "000"
        results.mkdir(parents=True)
        (results / "result.json").write_text("{}")
        files = select_files(project, SyncConfig())
        assert files == ["pkg/util.py", "train.py"]
        assert SnapshotService().compute(project, SyncConfig())[0] == baseline

    def test_ignore_file_and_extra_patterns(self, tmp_path):
        project = _project(tmp_path)
        (project / "data.bin").write_text("blob")
        (project / ".slurmdeckignore").write_text("*.bin\n# comment\n")
        files = select_files(project, SyncConfig(extra_ignores=["pkg"]))
        assert files == [".slurmdeckignore", "train.py"]

    def test_git_selection_tracks_git_files(self, tmp_path):
        project = _project(tmp_path)
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(["git", "add", "train.py"], cwd=project, check=True)
        (project / "untracked.py").write_text("u = 1\n")
        assert select_files(project, SyncConfig()) == ["train.py"]
        with_untracked = select_files(project, SyncConfig(include_untracked=True))
        assert "untracked.py" in with_untracked

    def test_directory_rules_prune_before_traversal(self, tmp_path, monkeypatch):
        project = _project(tmp_path)
        excluded = project / "large-data"
        (excluded / "nested").mkdir(parents=True)
        (excluded / "nested" / "payload.bin").write_bytes(b"x" * 1024)
        (project / ".slurmdeckignore").write_text("large-data/\n", encoding="utf-8")
        real_scandir = os.scandir

        def refuse_excluded(path):
            if Path(path) == excluded:
                raise AssertionError("ignored directory was traversed")
            return real_scandir(path)

        monkeypatch.setattr(snapshots_module.os, "scandir", refuse_excluded)

        assert select_files(project, SyncConfig()) == [".slurmdeckignore", "pkg/util.py", "train.py"]

    def test_sensitive_names_and_private_key_content_are_rejected(self, tmp_path):
        project = _project(tmp_path)
        (project / ".env").write_text("EXAMPLE_VALUE=placeholder\n", encoding="utf-8")
        (project / "client.pem").write_text("certificate-or-key\n", encoding="utf-8")
        private_key_header = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        (project / "notes.txt").write_text(f"{private_key_header}\nnot-a-real-key\n", encoding="utf-8")

        with pytest.raises(UserError) as error:
            select_files(project, SyncConfig())

        assert ".env" in str(error.value)
        assert "client.pem" in str(error.value)
        assert "notes.txt" in str(error.value)
        assert "sync.allow_sensitive_files" in str(error.value)

    @pytest.mark.parametrize("tracked", [False, True])
    def test_git_selection_rejects_selected_sensitive_files(self, tmp_path, tracked):
        project = _project(tmp_path)
        (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(["git", "add", "train.py"], cwd=project, check=True)
        if tracked:
            subprocess.run(["git", "add", "-f", ".env"], cwd=project, check=True)

        with pytest.raises(UserError, match=r"\.env"):
            select_files(project, SyncConfig(include_untracked=not tracked))

    def test_safe_templates_and_public_keys_remain_selectable(self, tmp_path):
        project = _project(tmp_path)
        (project / ".env.example").write_text("TOKEN=replace-me\n", encoding="utf-8")
        (project / "id_ed25519.pub").write_text("ssh-ed25519 public\n", encoding="utf-8")

        files = select_files(project, SyncConfig())

        assert ".env.example" in files
        assert "id_ed25519.pub" in files

    def test_exact_allowlist_permits_a_reviewed_sensitive_file(self, tmp_path):
        project = _project(tmp_path)
        (project / ".env").write_text("TOKEN=reviewed\n", encoding="utf-8")

        files = select_files(project, SyncConfig(allow_sensitive_files=[".env"]))

        assert ".env" in files

    def test_invalid_sensitive_allowlist_path_is_rejected(self, tmp_path):
        project = _project(tmp_path)

        with pytest.raises(UserError, match="project-relative POSIX path"):
            select_files(project, SyncConfig(allow_sensitive_files=["../secret.pem"]))

    def test_ignored_sensitive_file_does_not_block_the_snapshot(self, tmp_path):
        project = _project(tmp_path)
        (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (project / ".slurmdeckignore").write_text(".env\n", encoding="utf-8")

        files = select_files(project, SyncConfig())

        assert ".env" not in files


class TestHashing:
    def test_hash_is_content_sensitive(self, tmp_path):
        project = _project(tmp_path)
        service = SnapshotService()
        digest1, files = service.compute(project, SyncConfig())
        assert files == ["pkg/util.py", "train.py"]
        digest2, _ = service.compute(project, SyncConfig())
        assert digest1 == digest2
        (project / "train.py").write_text("print(2)\n")
        digest3, _ = service.compute(project, SyncConfig())
        assert digest3 != digest1

    def test_preview_reports_the_exact_files_size_and_hash(self, tmp_path):
        project = _project(tmp_path)
        service = SnapshotService()

        preview = service.preview(project, SyncConfig())
        digest, files = service.compute(project, SyncConfig())

        assert preview.hash == digest
        assert preview.file_count == len(files)
        assert preview.files == tuple(files)
        assert preview.size_bytes == sum((project / relative_path).stat().st_size for relative_path in files)


class TestEnsure:
    def test_uploads_once_then_reuses(self, tmp_path, fake_transport, remote_root):
        project = _project(tmp_path)
        layout = RemoteLayout(str(remote_root))
        service = SnapshotService()

        first = service.ensure(fake_transport, layout, project, SyncConfig())
        assert not first.reused
        assert (Path(first.remote_path) / "train.py").exists()
        assert (Path(first.remote_path) / "pkg" / "util.py").exists()
        assert not (Path(first.remote_path) / "__pycache__").exists()
        marker = json.loads(Path(layout.snapshot_marker(first.hash)).read_text(encoding="utf-8"))
        assert marker["size_bytes"] == len((project / "train.py").read_bytes()) + len(
            (project / "pkg" / "util.py").read_bytes()
        )
        listed = service.list_snapshots(fake_transport, layout)
        assert listed[0].size_bytes == marker["size_bytes"]

        upload_count = len(fake_transport.uploads)
        second = service.ensure(fake_transport, layout, project, SyncConfig())
        assert second.reused
        assert len(fake_transport.uploads) == upload_count  # no re-transfer


def _remote_snapshot(remote_root: Path, digest: str, *, created_at: str, content: bytes = b"payload") -> Path:
    snapshot = remote_root / "snapshots" / digest
    (snapshot / "code").mkdir(parents=True)
    (snapshot / "code" / "data.bin").write_bytes(content)
    (snapshot / ".complete.json").write_text(
        json.dumps({"schema_version": 1, "hash": digest, "created_at": created_at}) + "\n",
        encoding="utf-8",
    )
    return snapshot


class TestLifecycle:
    def test_list_derives_run_and_active_receipt_references_in_one_call(
        self,
        remote_root,
        fake_transport,
    ):
        run_hash = "a" * 64
        receipt_hash = "b" * 64
        unreferenced_hash = "c" * 64
        _remote_snapshot(remote_root, run_hash, created_at="2000-01-01T00:00:00Z")
        _remote_snapshot(remote_root, receipt_hash, created_at="2000-01-01T00:00:00Z")
        _remote_snapshot(remote_root, unreferenced_hash, created_at="2000-01-01T00:00:00Z")
        run_dir = remote_root / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "snapshot_hash": run_hash,
                }
            ),
            encoding="utf-8",
        )
        receipt_dir = remote_root / "receipts" / "run"
        receipt_dir.mkdir(parents=True)
        token = "d" * 64
        (receipt_dir / f"{token}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "token": token,
                    "run_id": "run-2",
                    "status": "unknown",
                    "snapshot_hash": receipt_hash,
                }
            ),
            encoding="utf-8",
        )
        submitted_token = "e" * 64
        (receipt_dir / f"{submitted_token}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "token": submitted_token,
                    "run_id": "completed-receipt",
                    "status": "submitted",
                    "snapshot_hash": unreferenced_hash,
                }
            ),
            encoding="utf-8",
        )
        fake_transport.calls.clear()

        snapshots = SnapshotService().list_snapshots(fake_transport, RemoteLayout(str(remote_root)))

        assert len(fake_transport.calls) == 1
        assert fake_transport.calls[0].startswith("python3 - snapshot-list")
        by_hash = {snapshot.hash: snapshot for snapshot in snapshots}
        assert by_hash[run_hash].references == ("run:run-1",)
        assert by_hash[receipt_hash].references == (f"submission:{token}",)
        assert by_hash[unreferenced_hash].references == ()
        assert by_hash[run_hash].reference_count == 1
        assert by_hash[run_hash].size_bytes > len(b"payload")
        assert by_hash[run_hash].created_at == "2000-01-01T00:00:00Z"
        assert not (remote_root / "locks" / "snapshot-gc.lock").exists()

    def test_gc_is_dry_run_by_default_and_deletes_only_old_unreferenced_v1_snapshots(
        self,
        remote_root,
        fake_transport,
    ):
        old = "1" * 64
        referenced = "2" * 64
        young = "3" * 64
        unknown = "legacy-snapshot"
        old_dir = _remote_snapshot(remote_root, old, created_at="2000-01-01T00:00:00Z")
        referenced_dir = _remote_snapshot(remote_root, referenced, created_at="2000-01-01T00:00:00Z")
        young_dir = _remote_snapshot(remote_root, young, created_at="2999-01-01T00:00:00Z")
        unknown_dir = remote_root / "snapshots" / unknown
        unknown_dir.mkdir(parents=True)
        (unknown_dir / "legacy.marker").write_text("unknown", encoding="utf-8")
        run_dir = remote_root / "runs" / "run-ref"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"schema_version": 1, "run_id": "run-ref", "snapshot_hash": referenced}),
            encoding="utf-8",
        )
        service = SnapshotService()
        layout = RemoteLayout(str(remote_root))

        preview = service.gc(fake_transport, layout)

        assert preview.dry_run is True
        assert preview.candidates == (old,)
        assert preview.deleted == ()
        assert old_dir.is_dir()

        applied = service.gc(fake_transport, layout, confirmed=True)

        assert applied.dry_run is False
        assert applied.candidates == (old,)
        assert applied.deleted == (old,)
        assert not old_dir.exists()
        assert referenced_dir.is_dir()
        assert young_dir.is_dir()
        assert unknown_dir.is_dir()
        assert (remote_root / "locks" / "snapshot-gc.lock").is_file()

    def test_run_clean_removes_reference_without_running_snapshot_gc(self, ctx, remote, fake_transport, remote_root):
        runs = RunService(ctx)
        row = runs.plan(
            command=CommandTemplateSpec(argv=["python3", "train.py"]),
            overrides=ResourceOverrides(),
            remote=remote,
        )
        row = runs.submit(fake_transport, row.id)
        layout = ctx.layout(remote)
        service = SnapshotService()
        before = {item.hash: item for item in service.list_snapshots(fake_transport, layout)}
        assert before[row.snapshot_hash].reference_count == 1

        runs.clean(row.id, transport=fake_transport)

        after = {item.hash: item for item in service.list_snapshots(fake_transport, layout)}
        assert after[row.snapshot_hash].reference_count == 0
        assert Path(layout.snapshot_dir(row.snapshot_hash)).is_dir()

    def test_run_clean_releases_an_active_submission_receipt(self, ctx, remote, fake_transport, remote_root):
        runs = RunService(ctx)
        row = runs.plan(
            command=CommandTemplateSpec(argv=["python3", "train.py"]),
            overrides=ResourceOverrides(),
            remote=remote,
        )
        token = "9" * 64
        repo = RunRepo(ctx.db())
        assert repo.begin_submission(row.id, token=token, phase="submit")
        assert repo.record_submission_error(
            row.id,
            token=token,
            state="submit_unknown",
            phase="submit",
            error=StructuredError(code="run_submit_unknown", summary="unknown"),
        )
        _remote_snapshot(remote_root, row.snapshot_hash, created_at="2000-01-01T00:00:00Z")
        receipt = Path(ctx.layout(remote).run_submission_receipt(token))
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "token": token,
                    "run_id": row.id,
                    "status": "unknown",
                    "snapshot_hash": row.snapshot_hash,
                }
            ),
            encoding="utf-8",
        )
        service = SnapshotService()
        assert service.list_snapshots(fake_transport, ctx.layout(remote))[0].reference_count == 1

        runs.clean(row.id, transport=fake_transport)

        remaining = service.list_snapshots(fake_transport, ctx.layout(remote))[0]
        assert remaining.reference_count == 0
        assert not receipt.exists()
