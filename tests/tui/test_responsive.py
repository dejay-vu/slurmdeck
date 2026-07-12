"""Responsive master-detail contracts at supported terminal widths."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import RichLog

from slurmdeck.models.env import (
    EnvBackend,
    EnvGeneration,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvOwnership,
)
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.services.env_registry import EnvRegistryClient
from slurmdeck.services.runs import RunService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.tui.app import SlurmDeckApp
from slurmdeck.tui.screens import (
    DoctorScreen,
    EnvLogsScreen,
    EnvsScreen,
    LogsScreen,
    RecordDetailScreen,
    RemotesScreen,
    RunDetailScreen,
    RunsScreen,
)
from slurmdeck.tui.widgets import KeyedTable, MasterPane
from slurmdeck.tui.widgets.responsive import DetailPane, ResponsiveMasterDetail, split_layout


async def _wait_for(condition, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture()
def submitted_run(ctx, remote, fake_transport):
    return RunService(ctx).submit(
        fake_transport,
        RunService(ctx)
        .plan(
            command=CommandTemplateSpec(argv=["python3", "-c", "print(1)"]),
            sweep=Sweep.model_validate({"version": 1, "parameters": {"seed": [0, 1]}}),
            overrides=ResourceOverrides(),
            remote=remote,
        )
        .id,
    )


@pytest.fixture()
def ready_env(ctx, remote, remote_root, fake_transport) -> EnvironmentRecord:
    digest = "d" * 64
    env_id = f"wide-{digest[:12]}"
    layout = RemoteLayout(str(remote_root))
    prefix = Path(layout.env_generation_dir(env_id, "gen-1"))
    prefix.mkdir(parents=True)
    provenance = EnvironmentProvenance(canonical_spec_hash=digest)
    generation = EnvGeneration(
        generation_id="gen-1",
        attempt_id="attempt-1",
        prefix=str(prefix),
        status=EnvironmentStatus.READY,
        created_at="2026-07-11T00:00:00Z",
        verified_at="2026-07-11T00:00:00Z",
        provenance=provenance,
    )
    record = EnvironmentRecord(
        env_id=env_id,
        full_hash=digest,
        backend=EnvBackend.CONDA,
        ownership=EnvOwnership.MANAGED,
        status=EnvironmentStatus.READY,
        active_generation=generation.generation_id,
        active_prefix=generation.prefix,
        created_at="2026-07-11T00:00:00Z",
        updated_at="2026-07-11T00:00:00Z",
        verified_at="2026-07-11T00:00:00Z",
        generations=[generation],
        provenance=provenance,
    )
    EnvRegistryClient().prepare(fake_transport, layout, record)
    return record


@pytest.mark.parametrize(
    ("width", "expected"),
    [(60, False), (80, False), (99, False), (100, True), (120, True)],
)
def test_split_layout_threshold(width: int, expected: bool) -> None:
    assert split_layout(width) is expected


@pytest.mark.parametrize("width", [100, 120])
async def test_runs_use_inline_detail_at_wide_widths(ctx, submitted_run, width: int) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
        assert app.screen.query_one(ResponsiveMasterDetail).has_class("responsive-master-detail")
        detail = app.screen.query_one("#run-preview", DetailPane)
        assert detail.display is True
        assert submitted_run.id in detail.plain

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)


async def test_runs_open_detail_page_at_80_columns(ctx, submitted_run) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        detail = app.screen.query_one("#run-preview", DetailPane)
        assert detail.display is False

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunDetailScreen)
        assert app.screen.run_id == submitted_run.id


async def test_resize_switches_existing_screen_without_recreation(ctx, submitted_run) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RunsScreen)
        detail = screen.query_one("#run-preview", DetailPane)
        assert detail.display is False

        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        assert app.screen is screen
        assert detail.display is True
        assert submitted_run.id in detail.plain


@pytest.mark.parametrize("width", [100, 120])
async def test_environments_use_inline_detail_at_wide_widths(ctx, ready_env, width: int) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await _wait_for(lambda: app.screen.query_one("#envs-table", KeyedTable).row_count == 1)
        assert isinstance(app.screen, EnvsScreen)
        detail = app.screen.query_one("#env-preview", DetailPane)
        assert detail.display is True
        assert ready_env.env_id in detail.plain

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EnvsScreen)


async def test_environment_enter_opens_record_page_at_80_columns(ctx, ready_env) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await _wait_for(lambda: app.screen.query_one("#envs-table", KeyedTable).row_count == 1)
        assert app.screen.query_one("#env-preview", DetailPane).display is False
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RecordDetailScreen)
        assert ready_env.env_id in app.screen.plain


@pytest.mark.parametrize("width", [100, 120])
async def test_remotes_and_doctor_share_wide_detail(ctx, remote, width: int) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        assert isinstance(app.screen, RemotesScreen)
        detail = app.screen.query_one("#remote-preview", DetailPane)
        assert detail.display is True
        assert remote.destination in detail.plain

        await pilot.press("d")
        await _wait_for(lambda: app.screen.query_one("#doctor-table", KeyedTable).display)
        assert isinstance(app.screen, RemotesScreen)
        assert app.screen.query_one("#doctor-table", KeyedTable).has_class("responsive-detail-content")


async def test_remote_and_doctor_open_pages_at_80_columns(ctx, remote) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RecordDetailScreen)
        assert remote.destination in app.screen.plain
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, DoctorScreen)
        await _wait_for(lambda: app.screen.query_one("#doctor-page-table", KeyedTable).row_count > 0)
        assert not app.screen.query("#profile-save")


@pytest.mark.parametrize("width", [100, 120])
async def test_task_logs_render_inline_at_wide_widths(ctx, submitted_run, width: int) -> None:
    log_path = Path(submitted_run.remote_root) / "logs" / f"task_{submitted_run.slurm_job_id}_0.out"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("responsive log line\n", encoding="utf-8")
    app = SlurmDeckApp(ctx)

    async with app.run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        app.push_screen(RunDetailScreen(submitted_run.id))
        await pilot.pause()
        assert isinstance(app.screen, RunDetailScreen)
        inline = app.screen.query_one("#inline-log", RichLog)
        assert inline.display is True

        await pilot.press("enter")
        await _wait_for(lambda: "responsive log line" in app.screen.inline_log_text)
        assert isinstance(app.screen, RunDetailScreen)


async def test_task_logs_keep_separate_page_at_80_columns(ctx, submitted_run) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        app.push_screen(RunDetailScreen(submitted_run.id))
        await pilot.pause()
        assert app.screen.query_one("#task-log-detail", MasterPane).display is False
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LogsScreen)


@pytest.mark.parametrize(
    ("owner", "description"),
    [
        (RunsScreen, "New run"),
        (EnvsScreen, "Prepare"),
        (RemotesScreen, "Add"),
        (RunDetailScreen, "Logs"),
        (LogsScreen, "Follow"),
        (EnvLogsScreen, "Follow"),
        (DoctorScreen, "Back"),
        (RecordDetailScreen, "Back"),
    ],
)
def test_each_screen_exposes_only_its_primary_footer_action(owner, description: str) -> None:
    assert [binding.description for binding in owner.BINDINGS if binding.show] == [description]


def test_global_navigation_stays_in_help_and_palette_not_footer() -> None:
    assert not [binding for binding in SlurmDeckApp.BINDINGS if binding.show]


def test_global_exit_keys_avoid_single_key_and_ctrl_q_conflicts() -> None:
    bindings = {binding.key: binding for binding in SlurmDeckApp.BINDINGS}

    assert "q" not in bindings
    assert bindings["ctrl+q"].action == "ignore_legacy_quit"
    assert bindings["ctrl+q"].priority is False
    assert bindings["ctrl+c"].action == "confirm_quit"
    assert bindings["ctrl+c"].priority is False


def test_command_palette_uses_non_priority_colon_instead_of_ctrl_p() -> None:
    bindings = {binding.key: binding for binding in SlurmDeckApp.BINDINGS}

    assert SlurmDeckApp.COMMAND_PALETTE_BINDING == "colon"
    assert "ctrl+p" not in bindings
    assert bindings["colon"].action == "command_palette"
    assert bindings["colon"].priority is False


def test_ctrl_c_requires_two_presses_inside_confirmation_window(ctx, monkeypatch) -> None:
    app = SlurmDeckApp(ctx)
    now = iter((100.0, 101.0))
    notifications: list[str] = []
    exits: list[bool] = []
    monkeypatch.setattr("slurmdeck.tui.app.time.monotonic", lambda: next(now))
    monkeypatch.setattr(app, "notify", lambda message, **_kwargs: notifications.append(message))
    monkeypatch.setattr(app, "exit", lambda: exits.append(True))

    app.action_confirm_quit()

    assert exits == []
    assert notifications == ["Press Ctrl+C again within 2 seconds to quit."]

    app.action_confirm_quit()

    assert exits == [True]


def test_expired_ctrl_c_confirmation_is_rearmed(ctx, monkeypatch) -> None:
    app = SlurmDeckApp(ctx)
    now = iter((100.0, 103.0, 104.0))
    exits: list[bool] = []
    monkeypatch.setattr("slurmdeck.tui.app.time.monotonic", lambda: next(now))
    monkeypatch.setattr(app, "notify", lambda _message, **_kwargs: None)
    monkeypatch.setattr(app, "exit", lambda: exits.append(True))

    app.action_confirm_quit()
    app.action_confirm_quit()
    assert exits == []

    app.action_confirm_quit()
    assert exits == [True]


async def test_runtime_key_bindings_ignore_ctrl_q_and_require_double_ctrl_c(ctx) -> None:
    app = SlurmDeckApp(ctx)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.active_bindings["ctrl+q"].binding.action == "ignore_legacy_quit"

        await pilot.press("ctrl+q")
        assert app._exit is False

        await pilot.press("ctrl+c")
        assert app._exit is False

        await pilot.press("ctrl+c")
        assert app._exit is True


async def test_runtime_footer_resolves_to_one_primary_action(ctx, submitted_run) -> None:
    app = SlurmDeckApp(ctx)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        visible = [
            active.binding.description
            for active in app.active_bindings.values()
            if active.binding.show and active.enabled
        ]
        assert visible == ["New run"]
