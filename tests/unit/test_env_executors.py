from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest
import yaml

from slurmdeck.agent import protocol
from slurmdeck.errors import UserError
from slurmdeck.models.cluster import (
    BuildExecutor,
    ClusterProfile,
    CondaProfile,
    LoginBuildPolicy,
    ModuleInitializationProfile,
    NetworkProfile,
    PlatformProfile,
    SharedFilesystemProfile,
    SlurmClusterProfile,
)
from slurmdeck.models.env import CondaEnvSpec, EnvironmentPlanAction, EnvironmentStatus, ExistingEnvSpec
from slurmdeck.models.project import ProjectConfig
from slurmdeck.models.resources import Resources
from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_execution import EnvironmentExecutorClient, EnvironmentPreparationService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.transport import TransportError, parse_json_lines


def _profile(
    conda: str,
    *,
    default: BuildExecutor = BuildExecutor.SLURM,
    allowed: list[BuildExecutor] | None = None,
    login_policy: LoginBuildPolicy = LoginBuildPolicy.FORBIDDEN,
) -> ClusterProfile:
    return ClusterProfile(
        allowed_build_executors=allowed or [default],
        default_build_executor=default,
        login_build_policy=login_policy,
        shared_filesystem=SharedFilesystemProfile(login_to_compute=True),
        module_initialization=ModuleInitializationProfile(strategy="none"),
        conda=CondaProfile(executable=conda),
        network=NetworkProfile(compute_access="full", channel_access="direct"),
        slurm=SlurmClusterProfile(
            partition="short",
            afterok_dependency=True,
            kill_invalid_dependency="per_job",
        ),
        platform=PlatformProfile(system="Linux", machine="x86_64", conda_subdir="linux-64"),
    )


def _project(project_dir: Path, *, post_install: list[str] | None = None, smoke_test: str | None = None):
    (project_dir / "environment.yml").write_text(
        "channels: [conda-forge]\ndependencies: [python=3.12]\n",
        encoding="utf-8",
    )
    return ProjectConfig(
        project_id="project-1",
        display_name="research",
        resources=Resources(time="01:00:00", cpus=2, mem="8G", partition="short"),
        env=CondaEnvSpec(
            name="ml",
            post_install=post_install or [],
            smoke_test=smoke_test,
        ),
    )


def _fake_conda(
    path: Path,
    *,
    explicit_url: str = "https://conda.anaconda.org/conda-forge/linux-64/python-3.12-0.conda",
    create_error: str = "",
    sleep_seconds: float = 0,
) -> Path:
    args_log = path.with_suffix(".args")
    env_log = path.with_suffix(".env")
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f'echo "$*" >> {args_log!s}\n'
        f'echo "${{CONDARC:-}}|${{HOME:-}}|${{XDG_CONFIG_HOME:-}}|${{CONDA_SUBDIR:-}}|'
        f'${{CONDA_DEFAULT_CHANNELS:-}}|${{CONDA_PLUGINS_AUTO_ACCEPT_TOS:-}}|${{CI-__UNSET__}}" >> {env_log!s}\n'
        'if [ "${1:-}" = "--version" ]; then echo "conda 25.5.0"; exit 0; fi\n'
        'if [ "${1:-}" = "env" ] && [ "${2:-}" = "create" ]; then\n'
        f"  sleep {sleep_seconds}\n"
        + (f"  echo {json.dumps(create_error)} >&2\n  exit 1\n" if create_error else "")
        + "  prefix=\n"
        '  while [ "$#" -gt 0 ]; do\n'
        '    if [ "$1" = "--prefix" ]; then prefix="$2"; shift 2; else shift; fi\n'
        "  done\n"
        '  mkdir -p "$prefix/conda-meta" "$prefix/bin" "$prefix/etc/conda/activate.d"\n'
        '  echo "export SLURMDECK_TEST_ACTIVATED=yes" > "$prefix/etc/conda/activate.d/slurmdeck-test.sh"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "list" ]; then\n'
        "  echo '# platform: linux-64'\n"
        "  echo '@EXPLICIT'\n"
        f"  echo {json.dumps(explicit_url)}\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _prepare(
    *,
    fake_transport,
    remote,
    remote_root: Path,
    project_dir: Path,
    profile: ClusterProfile,
    project: ProjectConfig,
    executor: BuildExecutor | None = None,
    rebuild: bool = False,
):
    return EnvironmentPreparationService().prepare(
        transport=fake_transport,
        remote=remote.model_copy(update={"cluster": profile}),
        layout=RemoteLayout(str(remote_root)),
        project=project,
        project_dir=project_dir,
        requested_executor=executor,
        rebuild=rebuild,
        wait=False,
    )


def _wait_for_process_exit(pid: int, *, timeout: float = 5.0) -> None:
    if pid <= 0:
        pytest.fail(f"invalid process ID: {pid}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError as exc:
            pytest.fail(f"unable to inspect background process {pid}: {exc}")
        time.sleep(0.05)
    pytest.fail(f"background process {pid} did not exit within {timeout:.1f}s")


class TestSlurmEnvironmentExecutor:
    def test_failed_build_helper_exits_nonzero_so_afterok_cannot_release(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        conda = _fake_conda(remote_root / "fake-conda", create_error="solver exploded")
        prepared = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile(str(conda)),
            project=_project(project_dir),
        )
        attempt = prepared.record.attempts[-1]

        result = fake_transport.exec_python(
            protocol.env_agent_source(),
            [
                "build",
                "--base",
                str(remote_root),
                "--env-id",
                prepared.record.env_id,
                "--attempt-id",
                attempt.attempt_id,
            ],
            check=False,
        )
        payload = parse_json_lines(result.stdout)[-1]

        assert result.returncode != 0
        assert payload["ok"] is True
        assert payload["action"] == "failed"
        assert payload["record"]["status"] == "FAILED"

    def test_concurrent_prepare_uploads_may_race_but_remote_lock_submits_exactly_once(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        project = _project(project_dir)
        profile = _profile("conda")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def prepare() -> None:
            barrier.wait(timeout=5)
            try:
                results.append(
                    _prepare(
                        fake_transport=fake_transport,
                        remote=remote,
                        remote_root=remote_root,
                        project_dir=project_dir,
                        profile=profile,
                        project=project,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=prepare) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(result.action.value for result in results) == ["attach", "create"]
        assert (remote_root / ".shims" / "sbatch.count").read_text(encoding="utf-8").strip() == "1"
        assert len(results[0].record.attempts) == 1

    def test_prepare_preallocates_immutable_generation_and_duplicate_attaches(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        project = _project(project_dir)
        profile = _profile("conda")

        created = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=profile,
            project=project,
        )
        attempt = created.record.attempts[-1]

        assert created.action is EnvironmentPlanAction.CREATE
        assert created.record.status is EnvironmentStatus.QUEUED
        assert created.record.active_generation is None
        assert attempt.job_id.isdigit()
        assert attempt.generation_id is not None
        assert attempt.prefix == RemoteLayout(str(remote_root)).env_generation_dir(
            created.record.env_id,
            attempt.generation_id,
        )
        assert len(fake_transport.uploads) == 1
        assert (Path(attempt.build_dir) / ".condarc").is_file()
        assert (Path(attempt.build_dir) / "isolated-environment.yml").is_file()
        assert " > /dev/null" in (Path(attempt.build_dir) / "build.sbatch").read_text(encoding="utf-8")
        assert (remote_root / ".shims" / "sbatch.count").read_text(encoding="utf-8").strip() == "1"

        attached = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=profile,
            project=project,
        )

        assert attached.action is EnvironmentPlanAction.ATTACH
        assert attached.record.current_attempt == attempt.attempt_id
        assert len(fake_transport.uploads) == 1
        assert (remote_root / ".shims" / "sbatch.count").read_text(encoding="utf-8").strip() == "1"
        assert Path(RemoteLayout(str(remote_root)).env_lock(created.record.full_hash)).is_file()

    def test_successful_build_validates_urls_before_atomic_promotion(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
        monkeypatch,
    ):
        monkeypatch.setenv("CI", "true")
        conda = _fake_conda(remote_root / "fake-conda")
        prepared = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile(str(conda)),
            project=_project(project_dir, smoke_test='[ "${SLURMDECK_TEST_ACTIVATED:-}" = yes ]'),
        )
        attempt = prepared.record.attempts[-1]

        built = (
            EnvironmentExecutorClient()
            .build(
                fake_transport,
                RemoteLayout(str(remote_root)),
                prepared.record.env_id,
                attempt.attempt_id,
            )
            .record
        )

        assert built.status is EnvironmentStatus.READY
        assert built.attempts[-1].scheduler_state == "COMPLETED"
        assert built.active_generation == attempt.generation_id
        assert built.active_prefix == attempt.prefix
        assert len(built.generations) == 1
        assert built.generations[0].prefix == attempt.prefix
        assert built.generations[0].provenance.package_urls == [
            "https://conda.anaconda.org/conda-forge/linux-64/python-3.12-0.conda"
        ]
        assert built.attempts[-1].resolved_channels == ["https://conda.anaconda.org/conda-forge"]
        assert Path(attempt.prefix).is_dir()
        args = conda.with_suffix(".args").read_text(encoding="utf-8")
        assert f"env create --prefix {attempt.prefix}" in args
        assert "--solver libmamba" in args
        assert "--subdir" not in args
        assert "--sha256" not in args
        assert "--override-channels" not in args
        assert "tos accept" not in args.lower()
        isolated = (Path(attempt.build_dir) / "isolated-environment.yml").read_text(encoding="utf-8")
        assert "nodefaults" in isolated
        condarc = yaml.safe_load((Path(attempt.build_dir) / ".condarc").read_text(encoding="utf-8"))
        assert condarc["channels"] == ["conda-forge"]
        assert condarc["default_channels"] == ["conda-forge"]
        assert condarc["allowlist_channels"] == ["conda-forge"]
        assert condarc["channel_priority"] == "strict"
        env_lines = conda.with_suffix(".env").read_text(encoding="utf-8").splitlines()
        assert env_lines
        for line in env_lines:
            env_values = line.split("|")
            assert env_values[0] == str(Path(attempt.build_dir) / ".condarc")
            assert env_values[1] == str(Path(attempt.build_dir) / "home")
            assert env_values[3] == "linux-64"
            assert env_values[4] == "conda-forge"
            assert env_values[5] == "false"
            assert env_values[6] == "__UNSET__"

        rebuilding = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile(str(conda)),
            project=_project(project_dir, smoke_test='[ "${SLURMDECK_TEST_ACTIVATED:-}" = yes ]'),
            rebuild=True,
        )
        next_attempt = rebuilding.record.attempts[-1]
        assert rebuilding.action is EnvironmentPlanAction.REBUILD
        assert rebuilding.record.active_generation == attempt.generation_id
        assert next_attempt.generation_id != attempt.generation_id
        assert next_attempt.prefix != attempt.prefix

        rebuilt = (
            EnvironmentExecutorClient()
            .build(
                fake_transport,
                RemoteLayout(str(remote_root)),
                rebuilding.record.env_id,
                next_attempt.attempt_id,
            )
            .record
        )
        assert rebuilt.active_generation == next_attempt.generation_id
        assert [generation.generation_id for generation in rebuilt.generations] == [
            attempt.generation_id,
            next_attempt.generation_id,
        ]
        assert Path(attempt.prefix).is_dir()

    @pytest.mark.parametrize(
        ("create_error", "explicit_url", "error_code"),
        [
            ("Channel Terms of Service rejected; run conda tos accept", "", "CHANNEL_TERMS_REQUIRED"),
            ("", "https://repo.anaconda.com/pkgs/main/linux-64/python-3.12-0.conda", "CHANNEL_ISOLATION_FAILED"),
        ],
    )
    def test_failed_terms_or_undeclared_package_url_never_publishes_generation(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
        create_error,
        explicit_url,
        error_code,
    ):
        conda = _fake_conda(
            remote_root / "fake-conda",
            create_error=create_error,
            explicit_url=explicit_url or "https://conda.anaconda.org/conda-forge/linux-64/python.conda",
        )
        prepared = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile(str(conda)),
            project=_project(project_dir),
        )
        attempt = prepared.record.attempts[-1]

        failed = (
            EnvironmentExecutorClient()
            .build(
                fake_transport,
                RemoteLayout(str(remote_root)),
                prepared.record.env_id,
                attempt.attempt_id,
            )
            .record
        )

        assert failed.status is EnvironmentStatus.FAILED
        assert failed.active_generation is None
        assert failed.generations == []
        assert failed.last_error is not None
        assert failed.last_error.code == error_code
        assert failed.attempts[-1].scheduler_state == "FAILED"
        args = conda.with_suffix(".args").read_text(encoding="utf-8")
        assert "tos accept" not in args.lower()

    def test_explicit_defaults_channel_is_not_silently_remapped_or_autoaccepted(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        project = _project(project_dir)
        (project_dir / "environment.yml").write_text(
            "channels: [defaults]\ndependencies: [python=3.12]\n",
            encoding="utf-8",
        )

        prepared = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=_profile("conda"),
            project=project,
        )

        attempt = prepared.record.attempts[-1]
        condarc = yaml.safe_load((Path(attempt.build_dir) / ".condarc").read_text(encoding="utf-8"))
        assert condarc["channels"] == ["defaults"]
        assert condarc["allowlist_channels"] == ["defaults"]
        assert "default_channels" not in condarc


class TestLoginEnvironmentExecutor:
    def test_login_executor_is_rejected_before_upload_without_explicit_permission(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        project = _project(project_dir)

        with pytest.raises(UserError, match="login"):
            _prepare(
                fake_transport=fake_transport,
                remote=remote,
                remote_root=remote_root,
                project_dir=project_dir,
                profile=_profile("conda"),
                project=project,
                executor=BuildExecutor.LOGIN,
            )

        assert fake_transport.uploads == []
        assert not (remote_root / "envs" / "registry").exists()

    def test_background_login_build_has_pid_heartbeat_receipt_and_no_resubmit_on_unknown(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        conda = _fake_conda(remote_root / "fake-conda", sleep_seconds=1)
        profile = _profile(
            str(conda),
            default=BuildExecutor.LOGIN,
            allowed=[BuildExecutor.LOGIN],
            login_policy=LoginBuildPolicy.ALLOWED,
        )
        project = _project(project_dir)
        prepared = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=profile,
            project=project,
        )
        attempt = prepared.record.attempts[-1]

        assert prepared.record.status is EnvironmentStatus.BUILDING
        assert attempt.login_pid is not None and attempt.login_pid > 0
        assert attempt.login_host
        assert attempt.heartbeat_at
        assert Path(RemoteLayout(str(remote_root)).env_receipt(attempt.attempt_id)).is_file()
        assert not (remote_root / ".shims" / "sbatch.count").exists()

        unknown = (
            EnvironmentExecutorClient()
            .reconcile(
                fake_transport,
                RemoteLayout(str(remote_root)),
                prepared.record.env_id,
                heartbeat_timeout=0,
            )
            .record
        )
        assert unknown.status is EnvironmentStatus.BUILD_UNKNOWN

        attached = _prepare(
            fake_transport=fake_transport,
            remote=remote,
            remote_root=remote_root,
            project_dir=project_dir,
            profile=profile,
            project=project,
        )
        assert attached.action is EnvironmentPlanAction.ATTACH
        assert attached.record.attempts[-1].login_pid == attempt.login_pid

        deadline = time.monotonic() + 10
        reconciled = attached.record
        while time.monotonic() < deadline and reconciled.status is not EnvironmentStatus.READY:
            time.sleep(0.1)
            reconciled = (
                EnvironmentExecutorClient()
                .reconcile(
                    fake_transport,
                    RemoteLayout(str(remote_root)),
                    prepared.record.env_id,
                )
                .record
            )
        assert reconciled.status is EnvironmentStatus.READY
        assert reconciled.active_generation == attempt.generation_id
        assert protocol.JSON_PREFIX not in Path(attempt.stdout_path).read_text(encoding="utf-8")
        _wait_for_process_exit(attempt.login_pid)


class TestExistingEnvironmentVerification:
    def test_existing_prefix_is_activation_probed_and_registered_without_a_cluster_profile(
        self,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        prefix = remote_root / "shared-env"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "activate").write_text(
            "export SLURMDECK_EXISTING_ACTIVE=yes\n",
            encoding="utf-8",
        )
        project = ProjectConfig(
            project_id="project-1",
            display_name="research",
            env=ExistingEnvSpec(
                name="shared",
                prefix=str(prefix),
                smoke_test='[ "$SLURMDECK_EXISTING_ACTIVE" = yes ]',
            ),
        )

        verified = EnvironmentPreparationService().prepare(
            transport=fake_transport,
            remote=remote,
            layout=RemoteLayout(str(remote_root)),
            project=project,
            project_dir=project_dir,
            wait=False,
        )

        assert verified.action is EnvironmentPlanAction.VERIFY
        assert verified.record.status is EnvironmentStatus.READY
        assert verified.record.active_prefix == str(prefix)
        assert verified.record.generations == []
        assert prefix.is_dir()

    def test_cached_external_ready_record_rechecks_a_missing_prefix(
        self,
        ctx,
        fake_transport,
        remote,
        remote_root,
        project_dir,
    ):
        prefix = remote_root / "shared-env"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "activate").write_text("true\n", encoding="utf-8")
        project = ProjectConfig(
            project_id="project-1",
            display_name="research",
            env=ExistingEnvSpec(name="shared", prefix=str(prefix)),
        )
        verified = EnvironmentPreparationService().prepare(
            transport=fake_transport,
            remote=remote,
            layout=RemoteLayout(str(remote_root)),
            project=project,
            project_dir=project_dir,
            wait=False,
        )
        cache = EnvironmentCache(ctx.user_paths)
        cache.remember_observation(remote, ClusterCapabilityService().observe(fake_transport, remote))
        cache.remember_registry(remote, [verified.record])
        shutil.rmtree(prefix)

        with pytest.raises(TransportError, match="prefix does not exist"):
            EnvironmentPreparationService(cache=cache).prepare(
                transport=fake_transport,
                remote=remote,
                layout=RemoteLayout(str(remote_root)),
                project=project,
                project_dir=project_dir,
                wait=False,
            )
