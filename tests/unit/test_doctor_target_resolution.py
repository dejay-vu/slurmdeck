from __future__ import annotations

from slurmdeck.models.env import ExistingEnvSpec
from slurmdeck.models.project import ProjectConfig, ProjectTarget
from slurmdeck.services.doctor import DoctorService
from slurmdeck.storage.db import DB_SCHEMA_VERSION


def test_legacy_doctor_remote_override_still_checks_top_level_environment(ctx):
    project = ctx.require_project()
    project.config = ProjectConfig(
        project_id=project.config.project_id,
        display_name=project.config.display_name,
        remote="cluster",
        env=ExistingEnvSpec(name="legacy", prefix="/shared/legacy"),
    )

    checks = {check.name: check for check in DoctorService(ctx).run(remote_name="cluster")}

    assert checks["environment"].state == "WARN"
    assert checks["environment"].detail == "existing env intent configured; readiness not verified"
    assert "env plan --remote cluster" in checks["environment"].fix


def test_named_target_doctor_remote_override_is_explicitly_remote_only(ctx):
    project = ctx.require_project()
    project.config = ProjectConfig(
        project_id=project.config.project_id,
        display_name=project.config.display_name,
        default_target="cluster-target",
        targets={
            "cluster-target": ProjectTarget(
                remote="cluster",
                env=ExistingEnvSpec(name="target-env", prefix="/shared/target"),
            )
        },
    )

    checks = {check.name: check for check in DoctorService(ctx).run(remote_name="cluster")}

    assert "target" not in checks
    assert checks["environment"].state == "SKIPPED"
    assert checks["environment"].detail == "no target selected for this remote-only check"


def test_named_target_doctor_labels_environment_intent_as_unverified(ctx):
    project = ctx.require_project()
    project.config = ProjectConfig(
        project_id=project.config.project_id,
        display_name=project.config.display_name,
        default_target="cluster-target",
        targets={
            "cluster-target": ProjectTarget(
                remote="cluster",
                env=ExistingEnvSpec(name="target-env", prefix="/shared/target"),
            )
        },
    )

    checks = {check.name: check for check in DoctorService(ctx).run(target_name="cluster-target")}

    assert checks["target"].detail == "cluster-target -> cluster"
    assert checks["environment"].state == "WARN"
    assert "readiness not verified" in checks["environment"].detail
    assert "env plan --target cluster-target" in checks["environment"].fix


def test_doctor_old_database_recommends_safe_automatic_migration(ctx):
    connection = ctx.db()
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    check = {item.name: item for item in DoctorService(ctx).run()}["database"]

    assert check.state == "FAILED"
    assert f"expected {DB_SCHEMA_VERSION}" in check.detail
    assert "slurmdeck run list" in check.fix
    assert "fresh" not in check.fix.lower()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
