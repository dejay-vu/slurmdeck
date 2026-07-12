from __future__ import annotations

import asyncio
import statistics
import time
from io import StringIO

from rich.console import Console

from slurmdeck.cli._output import OutputManager
from slurmdeck.models.env import EnvWaitPolicy
from slurmdeck.models.resources import Resources
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.presentation import resolve_theme
from slurmdeck.services.cluster import ClusterCapabilityService
from slurmdeck.services.env_binding import EnvironmentRunBindingService
from slurmdeck.services.env_cache import EnvironmentCache
from slurmdeck.services.env_execution import EnvironmentExecutorClient, EnvironmentPreparationService
from slurmdeck.services.env_lifecycle import EnvironmentLifecycleService
from slurmdeck.services.runs import RunService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.storage.repos import RunRepo
from slurmdeck.tui.app import SlurmDeckApp
from slurmdeck.tui.screens import EnvsScreen
from slurmdeck.tui.widgets import EmptyState
from tests.unit.test_env_executors import _fake_conda, _prepare, _profile, _project


async def _wait_for(condition, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_cached_no_wait_create_and_ready_check_enforce_round_trip_budgets(
    ctx,
    remote,
    remote_root,
    project_dir,
    fake_transport,
) -> None:
    project = _project(project_dir)
    conda = _fake_conda(remote_root / "fake-conda")
    configured_remote = remote.model_copy(update={"cluster": _profile(str(conda))})
    cache = EnvironmentCache(ctx.user_paths)
    observation = ClusterCapabilityService().observe(fake_transport, configured_remote)
    cache.remember_observation(configured_remote, observation)
    service = EnvironmentPreparationService(cache=cache)
    layout = RemoteLayout(str(remote_root))
    fake_transport.reset_metrics()

    created = service.prepare(
        transport=fake_transport,
        remote=configured_remote,
        layout=layout,
        project=project,
        project_dir=project_dir,
        wait=False,
    )

    assert created.record.status.value == "QUEUED"
    assert fake_transport.call_counts["helper"] == 1
    assert fake_transport.call_counts["env:prepare-build"] == 1
    assert fake_transport.call_counts["upload"] == 1

    attempt = created.record.attempts[-1]
    ready = (
        EnvironmentExecutorClient()
        .build(
            fake_transport,
            layout,
            created.record.env_id,
            attempt.attempt_id,
        )
        .record
    )
    cache.remember_registry(configured_remote, [ready])
    fake_transport.reset_metrics()

    reused = service.prepare(
        transport=fake_transport,
        remote=configured_remote,
        layout=layout,
        project=project,
        project_dir=project_dir,
        wait=False,
    )

    assert reused.record.status.value == "READY"
    assert fake_transport.call_counts["helper"] == 1
    assert fake_transport.call_counts["env:candidate-check"] == 1
    assert fake_transport.call_counts["upload"] == 0


def test_env_status_is_one_batched_scan(remote, remote_root, project_dir, fake_transport) -> None:
    project = _project(project_dir)
    prepared = EnvironmentPreparationService().prepare(
        transport=fake_transport,
        remote=remote.model_copy(update={"cluster": _profile("conda")}),
        layout=RemoteLayout(str(remote_root)),
        project=project,
        project_dir=project_dir,
        wait=False,
    )
    fake_transport.reset_metrics()

    EnvironmentLifecycleService().status(
        fake_transport,
        RemoteLayout(str(remote_root)),
        prepared.record.env_id,
    )

    assert fake_transport.call_counts["helper"] == 1
    assert fake_transport.call_counts["env:scan"] == 1


def test_cached_ready_run_submit_is_one_preflight_one_upload_and_one_submit(
    ctx,
    remote,
    remote_root,
    project_dir,
    fake_transport,
) -> None:
    project = _project(project_dir)
    conda = _fake_conda(remote_root / "fake-conda")
    configured_remote = remote.model_copy(update={"cluster": _profile(str(conda))})
    prepared = _prepare(
        fake_transport=fake_transport,
        remote=remote,
        remote_root=remote_root,
        project_dir=project_dir,
        profile=configured_remote.cluster,
        project=project,
    )
    attempt = prepared.record.attempts[-1]
    ready = (
        EnvironmentExecutorClient()
        .build(
            fake_transport,
            RemoteLayout(str(remote_root)),
            prepared.record.env_id,
            attempt.attempt_id,
        )
        .record
    )
    cache = EnvironmentCache(ctx.user_paths)
    cache.remember_observation(
        configured_remote,
        ClusterCapabilityService().observe(fake_transport, configured_remote),
    )
    cache.remember_registry(configured_remote, [ready])
    assert ctx.project is not None
    ctx.project.config = project
    binding = EnvironmentRunBindingService(cache=cache).resolve(
        transport=fake_transport,
        remote=configured_remote,
        layout=RemoteLayout(str(remote_root)),
        project=project,
        project_dir=project_dir,
        wait_policy=EnvWaitPolicy.READY,
    )
    assert binding is not None
    runs = RunService(ctx)
    first = runs.plan(
        command=CommandTemplateSpec(argv=["/usr/bin/true"]), remote=configured_remote, env_binding=binding
    )
    runs.submit(fake_transport, first.id)
    second = runs.plan(
        command=CommandTemplateSpec(argv=["/usr/bin/true"]),
        remote=configured_remote,
        env_binding=binding,
    )
    fake_transport.reset_metrics()

    runs.submit(fake_transport, second.id)

    assert fake_transport.call_counts["helper"] == 2
    assert fake_transport.call_counts["env:binding-check"] == 1
    assert fake_transport.call_counts["helper:submit-run"] == 1
    assert fake_transport.call_counts["upload"] == 1
    assert fake_transport.call_counts["exec"] == 0


def test_query_and_render_one_thousand_runs_has_sub_500ms_median(ctx) -> None:
    database = ctx.db()
    repository = RunRepo(database)
    resources = Resources()
    command = CommandTemplateSpec(argv=["python3", "train.py"])
    with database:
        for index in range(1000):
            run_id = f"run-20260711-{index:04d}"
            repository.insert(
                run_id=run_id,
                project_id="project-id",
                project_display_name="Project",
                name=run_id,
                remote="cluster",
                created_at=f"2026-07-11T12:{index // 60:02d}:{index % 60:02d}Z",
                state="planned",
                remote_root=f"/remote/runs/{run_id}",
                snapshot_hash="a" * 64,
                env_id="",
                resources=resources,
                command=command,
                sweep_file=None,
                retry_of=None,
                transaction=database,
            )

    theme = resolve_theme("mono", environ={})

    def render_once() -> float:
        stdout = StringIO()
        output = OutputManager(
            Console(
                file=stdout,
                width=120,
                height=30,
                force_terminal=True,
                color_system=None,
                theme=theme.rich_theme(),
            ),
            Console(file=StringIO()),
        )
        started = time.perf_counter()
        rows = RunService(ctx).list_views()
        output.records(
            "Runs",
            ["RUN", "STATE", "TASKS", "JOB", "CREATED"],
            [[row.id, row.state, row.summary.format_counts(), row.slurm_job_id or "-", row.created_at] for row in rows],
        )
        return time.perf_counter() - started

    render_once()
    durations = [render_once() for _ in range(7)]
    assert statistics.median(durations) < 0.5, durations


async def test_tui_paints_loading_within_200ms_while_remote_latency_is_visible(ctx, fake_transport) -> None:
    fake_transport.set_delay("env:scan", 0.35)
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        started = time.monotonic()
        await pilot.press("2")
        await _wait_for(lambda: isinstance(app.screen, EnvsScreen))
        screen = app.screen
        assert isinstance(screen, EnvsScreen)
        empty = screen.query_one("#envs-empty", EmptyState)
        assert app.operation_started_at is not None
        assert 0 <= app.operation_started_at - started < 0.2
        assert "Loading" in empty.render().plain
        assert app.operation_text == "Loading environments"

        await asyncio.sleep(0.22)
        assert app.operation_elapsed_now() >= 0.2
        await _wait_for(lambda: app.operation_text == "")
