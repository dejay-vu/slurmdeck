from __future__ import annotations

from pathlib import Path

import pytest

from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.remote import Remote
from slurmdeck.services.context import AppContext
from slurmdeck.storage.db import connect
from slurmdeck.storage.paths import ProjectPaths, UserPaths
from slurmdeck.storage.user_store import UserStore
from slurmdeck.storage.yamlio import dump_yaml_model
from tests.fakes.transport import FakeTransport


@pytest.fixture()
def user_paths(tmp_path: Path) -> UserPaths:
    return UserPaths(config_dir=tmp_path / "config", runtime_dir=tmp_path / "runtime")


@pytest.fixture()
def remote_root(tmp_path: Path) -> Path:
    return tmp_path / "cluster"


@pytest.fixture()
def fake_transport(remote_root: Path) -> FakeTransport:
    return FakeTransport(remote_root)


@pytest.fixture()
def remote(user_paths: UserPaths, remote_root: Path) -> Remote:
    """A registered, 'connected' remote whose base lives in a local tmp dir."""
    remote = Remote(name="cluster", host="user@fake.example.com", base=str(remote_root), resolved_base=str(remote_root))
    store = UserStore(user_paths)
    dump_yaml_model(user_paths.remotes_dir / "cluster.yaml", remote)
    store.set_current_remote("cluster")
    for child in ("runs", "snapshots", "envs"):
        (remote_root / child).mkdir(parents=True, exist_ok=True)
    return remote


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """An initialized project with a small code tree."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "train.py").write_text(
        "import os, pathlib\n"
        "out = pathlib.Path(os.environ['SLURMDECK_OUTPUT_DIR'])\n"
        "(out / 'done.txt').write_text('ok')\n"
        "print('trained')\n",
        encoding="utf-8",
    )
    paths = ProjectPaths(project)
    dump_yaml_model(
        paths.config_path,
        ProjectConfig(project_id="test-project-id", display_name=project.name),
    )
    connect(paths.db_path).close()
    return project


@pytest.fixture()
def ctx(project_dir: Path, user_paths: UserPaths, remote: Remote, fake_transport: FakeTransport) -> AppContext:
    return AppContext.create(
        cwd=project_dir,
        user_paths=user_paths,
        transport_factory=lambda _remote: fake_transport,
    )
