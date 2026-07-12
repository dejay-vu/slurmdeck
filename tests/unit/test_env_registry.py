from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from slurmdeck.agent import protocol
from slurmdeck.models.env import (
    EnvBackend,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvironmentView,
    EnvOwnership,
)
from slurmdeck.services.env_registry import EnvRegistryClient
from slurmdeck.storage.paths import RemoteLayout

FULL_HASH = "a" * 64
ENV_ID = f"ml-{FULL_HASH[:12]}"


def _record() -> EnvironmentRecord:
    return EnvironmentRecord(
        env_id=ENV_ID,
        full_hash=FULL_HASH,
        backend=EnvBackend.CONDA,
        ownership=EnvOwnership.MANAGED,
        status=EnvironmentStatus.PLANNED,
        created_at="2026-07-11T00:00:00Z",
        updated_at="2026-07-11T00:00:00Z",
        provenance=EnvironmentProvenance(
            canonical_spec_hash=FULL_HASH,
            environment_file_hash="b" * 64,
            channels=["conda-forge"],
            channel_priority="strict",
            solver="libmamba",
            platform="linux-64",
        ),
    )


class TestEnvironmentModels:
    def test_record_carries_full_identity_and_view_owns_dynamic_references(self):
        record = _record()
        view = EnvironmentView(
            record=record,
            references=["run:project-1/run-1", "run:project-2/run-2"],
            desired_by_project=True,
        )

        assert record.full_hash == FULL_HASH
        assert view.reference_count == 2
        assert view.references == ["run:project-1/run-1", "run:project-2/run-2"]
        assert "reference_count" not in record.model_dump()

    def test_record_rejects_short_hash_wrong_suffix_and_persisted_reference_fields(self):
        payload = _record().model_dump(mode="json")
        payload["full_hash"] = "short"
        with pytest.raises(ValidationError, match="full_hash"):
            EnvironmentRecord.model_validate(payload)

        payload = _record().model_dump(mode="json")
        payload["env_id"] = "ml-wrong000000"
        with pytest.raises(ValidationError, match="env_id"):
            EnvironmentRecord.model_validate(payload)

        payload = _record().model_dump(mode="json")
        payload["reference_count"] = 1
        with pytest.raises(ValidationError, match="reference_count"):
            EnvironmentRecord.model_validate(payload)


class TestRemoteEnvironmentLayout:
    def test_first_format_paths_are_centralized(self):
        layout = RemoteLayout("/base")

        assert layout.env_registry_record(ENV_ID) == f"/base/envs/registry/{ENV_ID}.json"
        assert layout.env_generation_dir(ENV_ID, "gen-1") == f"/base/envs/generations/{ENV_ID}/gen-1"
        assert layout.env_attempt_dir(ENV_ID, "attempt-1") == f"/base/envs/attempts/{ENV_ID}/attempt-1"
        assert layout.env_inbox_dir("attempt-1") == "/base/envs/inbox/attempt-1"
        assert layout.env_trash_dir(ENV_ID) == f"/base/envs/trash/{ENV_ID}"
        assert layout.env_lock(FULL_HASH) == f"/base/locks/env/{FULL_HASH}/.lock"
        assert layout.env_receipt("attempt-1") == "/base/receipts/env/attempt-1.json"


class TestEnvironmentRegistryHelper:
    def test_prepare_is_hash_locked_idempotent_and_inspect_ignores_legacy_projection(
        self,
        fake_transport,
        remote_root,
    ):
        client = EnvRegistryClient()
        layout = RemoteLayout(str(remote_root))
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def prepare() -> None:
            barrier.wait(timeout=5)
            try:
                results.append(client.prepare(fake_transport, layout, _record()))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=prepare) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(result.action for result in results) == ["create", "reuse"]
        assert all(result.record == _record() for result in results)
        registry = Path(layout.env_registry_record(ENV_ID))
        assert EnvironmentRecord.model_validate_json(registry.read_text(encoding="utf-8")) == _record()
        assert Path(layout.env_lock(FULL_HASH)).is_file()
        assert not (remote_root / "envs" / ENV_ID / "env.json").exists()

        legacy = remote_root / "envs" / "legacy-env" / "env.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"env_id": "legacy-env", "status": "READY"}), encoding="utf-8")
        fake_transport.calls.clear()

        records = client.inspect(fake_transport, layout)

        assert records == [_record()]
        assert len(fake_transport.calls) == 1
        assert fake_transport.calls[0].startswith("python3 - inspect")

    def test_conflicting_or_corrupt_registry_is_never_overwritten(self, fake_transport, remote_root):
        layout = RemoteLayout(str(remote_root))
        registry = Path(layout.env_registry_record(ENV_ID))
        registry.parent.mkdir(parents=True)
        registry.write_text("{broken", encoding="utf-8")
        before = registry.read_bytes()

        with pytest.raises(Exception, match="registry"):
            EnvRegistryClient().prepare(fake_transport, layout, _record())

        assert registry.read_bytes() == before

    def test_helper_source_is_stdlib_only(self):
        source = protocol.env_agent_source()
        assert "slurmdeck.env-registry.v1" in source
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                module = line.split()[1].split(".")[0]
                assert module in {
                    "argparse",
                    "contextlib",
                    "fcntl",
                    "hashlib",
                    "json",
                    "os",
                    "re",
                    "shlex",
                    "shutil",
                    "socket",
                    "subprocess",
                    "sys",
                    "time",
                }
