from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from slurmdeck.services.doctor import DoctorService
from slurmdeck.transport import TransportError


def _tree_hash(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("returncode", [None, 7])
def test_doctor_remote_failure_paths_never_mutate_local_config_project_or_remote(
    ctx,
    remote_root,
    fake_transport,
    returncode,
) -> None:
    fake_transport._slurm_shims()
    fake_transport.script_call(
        "helper:cluster-observe",
        TransportError("injected Doctor probe failure", returncode=returncode),
    )
    project_root = ctx.require_project().paths.state_dir
    before = (
        _tree_hash(ctx.user_paths.config_dir),
        _tree_hash(project_root),
        _tree_hash(remote_root),
    )

    checks = DoctorService(ctx).run()

    assert any(check.state == "FAILED" for check in checks)
    assert (
        _tree_hash(ctx.user_paths.config_dir),
        _tree_hash(project_root),
        _tree_hash(remote_root),
    ) == before
