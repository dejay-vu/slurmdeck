from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from slurmdeck.errors import UserError
from slurmdeck.models.cluster import (
    BuildExecutor,
    ClusterObservation,
    ClusterProfile,
    CondaProfile,
    LoginBuildPolicy,
    ModuleInitializationProfile,
    NetworkProfile,
    PlatformProfile,
    SharedFilesystemProfile,
    SlurmClusterProfile,
    ToolObservation,
)
from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.doctor import DoctorService
from slurmdeck.services.remotes import RemoteService


def _complete_profile() -> ClusterProfile:
    return ClusterProfile(
        allowed_build_executors=[BuildExecutor.SLURM],
        default_build_executor=BuildExecutor.SLURM,
        login_build_policy=LoginBuildPolicy.FORBIDDEN,
        shared_filesystem=SharedFilesystemProfile(login_to_compute=True),
        module_initialization=ModuleInitializationProfile(strategy="none"),
        conda=CondaProfile(executable="conda"),
        network=NetworkProfile(compute_access="full", channel_access="direct"),
        slurm=SlurmClusterProfile(
            partition="short",
            afterok_dependency=True,
            kill_invalid_dependency="per_job",
        ),
        platform=PlatformProfile(system="Linux", machine="x86_64", conda_subdir="linux-64"),
    )


def _observation() -> ClusterObservation:
    return ClusterObservation(
        observed_at=123.0,
        python_version="3.11.9",
        tools={
            name: ToolObservation(available=True, path=f"/usr/bin/{name}", version="23.11")
            for name in ("sbatch", "squeue", "sacct", "scancel", "sinfo")
        },
        base_writable=True,
        default_partition="short",
        partitions=[{"name": "short", "is_default": True, "max_time": "01:00:00"}],
        system="Linux",
        machine="x86_64",
        conda_subdir="linux-64",
        conda_path="/usr/bin/conda",
        afterok_dependency_supported=True,
        kill_invalid_dependency_supported=True,
    )


class TestClusterModels:
    def test_incomplete_profile_is_valid_and_contract_reports_missing_policy(self):
        profile = ClusterProfile()

        contract = ClusterCapabilityService().resolve(profile, _observation())

        assert contract.complete is False
        assert "allowed_build_executors" in contract.missing
        assert "login_build_policy" in contract.missing
        assert "shared_filesystem.login_to_compute" in contract.missing

    def test_complete_slurm_profile_resolves_without_missing_or_conflicts(self):
        contract = ClusterCapabilityService().resolve(_complete_profile(), _observation())

        assert contract.complete is True
        assert contract.executor is BuildExecutor.SLURM
        assert contract.missing == []
        assert contract.conflicts == []
        assert contract.afterok_eligible is True

    def test_contradictory_executor_policy_is_rejected(self):
        with pytest.raises(ValidationError, match="default_build_executor"):
            ClusterProfile(
                allowed_build_executors=[BuildExecutor.SLURM],
                default_build_executor=BuildExecutor.LOGIN,
            )
        with pytest.raises(ValidationError, match="login_build_policy"):
            ClusterProfile(
                allowed_build_executors=[BuildExecutor.LOGIN],
                default_build_executor=BuildExecutor.LOGIN,
                login_build_policy=LoginBuildPolicy.FORBIDDEN,
            )
        with pytest.raises(ValidationError, match="module initialization"):
            ClusterProfile(
                module_initialization=ModuleInitializationProfile(strategy="none"),
                conda=CondaProfile(modules=["Anaconda3"]),
            )

    def test_observed_dependency_option_mismatch_is_a_contract_conflict(self):
        payload = _observation().model_dump(mode="json")
        payload["kill_invalid_dependency_supported"] = False
        observation = ClusterObservation.model_validate(payload)

        contract = ClusterCapabilityService().resolve(_complete_profile(), observation)

        assert contract.complete is False
        assert any("kill-on-invalid-dep" in conflict for conflict in contract.conflicts)


class TestProfileStorage:
    def test_profile_set_validates_before_atomic_remote_replacement(self, ctx, tmp_path):
        service = RemoteService(ctx)
        remote_path = ctx.user_paths.remotes_dir / "cluster.yaml"
        before = remote_path.read_bytes()
        invalid = tmp_path / "invalid-profile.yaml"
        invalid.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "allowed_build_executors": ["slurm"],
                    "default_build_executor": "login",
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(UserError, match="default_build_executor"):
            service.set_profile("cluster", invalid)

        assert remote_path.read_bytes() == before

        profile_file = tmp_path / "profile.yaml"
        profile_file.write_text(yaml.safe_dump(_complete_profile().model_dump(mode="json")), encoding="utf-8")
        updated = service.set_profile("cluster", profile_file)

        assert updated.cluster == _complete_profile()
        assert service.show_profile("cluster") == _complete_profile()

    def test_profile_show_is_read_only(self, ctx):
        service = RemoteService(ctx)
        remote_path = ctx.user_paths.remotes_dir / "cluster.yaml"
        before = remote_path.read_bytes()

        assert service.show_profile("cluster") is None

        assert remote_path.read_bytes() == before


def _tree_content(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


class TestReadOnlyObservation:
    def test_observe_uses_one_read_only_remote_call(self, remote, remote_root, fake_transport):
        fake_transport._slurm_shims()
        before = _tree_content(remote_root)
        fake_transport.calls.clear()

        observation = ClusterCapabilityService().observe(fake_transport, remote)

        assert len(fake_transport.calls) == 1
        assert fake_transport.calls[0].startswith("python3 - cluster-observe")
        assert observation.python_version
        assert observation.tools["sbatch"].available is True
        assert observation.afterok_dependency_supported is True
        assert observation.kill_invalid_dependency_supported is True
        assert _tree_content(remote_root) == before

    def test_doctor_does_not_create_db_wal_profile_cache_or_remote_state(
        self,
        ctx,
        remote_root,
        fake_transport,
    ):
        fake_transport._slurm_shims()
        paths = ctx.require_project().paths
        paths.db_path.unlink()
        Path(f"{paths.db_path}-wal").unlink(missing_ok=True)
        Path(f"{paths.db_path}-shm").unlink(missing_ok=True)
        local_before = _tree_content(paths.state_dir)
        config_before = _tree_content(ctx.user_paths.config_dir)
        remote_before = _tree_content(remote_root)

        checks = DoctorService(ctx).run()

        assert any(check.name == "database" for check in checks)
        assert not paths.db_path.exists()
        assert not Path(f"{paths.db_path}-wal").exists()
        assert not Path(f"{paths.db_path}-shm").exists()
        assert _tree_content(paths.state_dir) == local_before
        assert _tree_content(ctx.user_paths.config_dir) == config_before
        assert _tree_content(remote_root) == remote_before


def test_observation_json_contract_is_strict():
    payload = json.loads(_observation().model_dump_json())
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ClusterObservation.model_validate(payload)
