"""Textual pilot tests over seeded state and the FakeTransport."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.command import CommandPalette
from textual.widgets import Checkbox, Input, Select, TextArea

from slurmdeck.models.cluster import BuildExecutor
from slurmdeck.models.env import (
    EnvBackend,
    EnvBuildAttempt,
    EnvGeneration,
    EnvironmentProvenance,
    EnvironmentRecord,
    EnvironmentStatus,
    EnvironmentView,
    EnvOwnership,
    ExistingEnvSpec,
)
from slurmdeck.models.resources import ResourceOverrides, Resources
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.sweep import Sweep
from slurmdeck.services.context import AppContext
from slurmdeck.services.env_registry import EnvRegistryClient
from slurmdeck.services.runs import RunService
from slurmdeck.storage.paths import RemoteLayout
from slurmdeck.storage.yamlio import dump_yaml_model
from slurmdeck.transport import ExecResult
from slurmdeck.tui.app import SlurmDeckApp
from slurmdeck.tui.messages import RefreshFinished
from slurmdeck.tui.screens import (
    DoctorScreen,
    EnvLogsScreen,
    EnvsScreen,
    HelpScreen,
    LogsScreen,
    RemotesScreen,
    RunDetailScreen,
    RunsScreen,
)
from slurmdeck.tui.widgets import ConfirmModal, EmptyState, ErrorPanel, KeyedTable
from slurmdeck.tui.widgets.forms import NewRunModal, ProfileModal, RemoteFormModal

SWEEP = Sweep.model_validate({"version": 1, "parameters": {"seed": [0, 1]}})
COMMAND = CommandTemplateSpec(argv=["python3", "-c", "print(1)"])


@pytest.fixture()
def submitted_run(ctx, remote, fake_transport):
    runs = RunService(ctx)
    row = runs.plan(command=COMMAND, sweep=SWEEP, overrides=ResourceOverrides(), remote=remote)
    return runs.submit(fake_transport, row.id)


@pytest.fixture()
def pending_run(ctx, remote, fake_transport):
    """A submitted run whose tasks are still on the scheduler (nothing executed)."""
    fake_transport.simulate_execution = False
    runs = RunService(ctx)
    row = runs.plan(command=COMMAND, sweep=SWEEP, overrides=ResourceOverrides(), remote=remote)
    row = runs.submit(fake_transport, row.id)
    fake_transport.squeue_output = f"{row.slurm_job_id}_0|RUNNING|node1\n{row.slurm_job_id}_1|PENDING|\n"
    return row


async def _wait_for(condition, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


class TestRunsScreen:
    async def test_explicit_theme_override_is_registered(self, ctx, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("SLURMDECK_THEME", "dark")
        ctx.user_store.set_ui_theme("mono")
        app = SlurmDeckApp(ctx, theme_name="light")

        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.theme_spec.name == "light"
            assert app.theme == "slurmdeck-light"
            assert ctx.user_store.ui_theme() == "mono"

    async def test_selected_theme_persists_across_ui_restarts(self, ctx, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("SLURMDECK_THEME", raising=False)
        first = SlurmDeckApp(ctx)

        async with first.run_test() as pilot:
            await pilot.pause()
            first.select_theme("monokai")
            await pilot.pause()
            assert first.theme_spec.name == "dark"
            assert first.theme == "monokai"
            assert ctx.user_store.ui_theme() == "monokai"

        reopened = SlurmDeckApp(ctx)
        async with reopened.run_test() as pilot:
            await pilot.pause()
            assert reopened.theme_spec.name == "dark"
            assert reopened.theme == "monokai"

    async def test_theme_picker_offers_full_textual_and_slurmdeck_theme_sets(self, ctx, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        app = SlurmDeckApp(ctx)
        captured = []

        async with app.run_test() as pilot:
            await pilot.pause()
            monkeypatch.setattr(
                app,
                "search_commands",
                lambda commands, placeholder: captured.extend(commands),
            )
            app.action_change_theme()
            monokai = next(command for command in captured if command[0] == "monokai")
            monokai[1]()
            await pilot.pause()
            assert app.theme == "monokai"
            assert ctx.user_store.ui_theme() == "monokai"

        names = [command[0] for command in captured]
        assert {"monokai", "nord", "dracula", "SlurmDeck Dark", "SlurmDeck Light", "SlurmDeck Mono"} <= set(names)
        assert len(names) > 20

    async def test_theme_picker_selection_uses_real_command_palette(self, ctx, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("SLURMDECK_THEME", raising=False)
        app = SlurmDeckApp(ctx)

        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_change_theme()
            await pilot.pause()
            await pilot.press("m", "o", "n", "o", "k", "a", "i")
            await pilot.pause()
            await pilot.press("enter")
            await _wait_for(lambda: app.theme == "monokai")

            assert app.theme_spec.name == "dark"
            assert ctx.user_store.ui_theme() == "monokai"

    async def test_copying_selected_input_text_does_not_arm_global_exit(self, ctx):
        app = SlurmDeckApp(ctx)

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            command = app.screen.query_one("#run-command", Input)
            command.value = "python3 train.py"
            command.focus()
            command.select_all()

            await pilot.press("ctrl+c")

            assert app.clipboard == "python3 train.py"
            assert app._quit_armed_until is None

    async def test_colon_opens_command_palette_without_ctrl_modifier(self, ctx):
        app = SlurmDeckApp(ctx)

        async with app.run_test() as pilot:
            await pilot.pause()
            assert "ctrl+p" not in app.active_bindings
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, RunsScreen)

            await pilot.press(":")
            await pilot.pause()

            assert isinstance(app.screen, CommandPalette)

    async def test_colon_remains_typable_inside_form_inputs(self, ctx):
        app = SlurmDeckApp(ctx)

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewRunModal)
            command = app.screen.query_one("#run-command", Input)
            command.value = "https"
            command.cursor_position = len(command.value)
            command.focus()

            await pilot.press(":")
            await pilot.pause()

            assert isinstance(app.screen, NewRunModal)
            assert command.value == "https:"

    async def test_new_run_form_plans_simple_command(self, ctx):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewRunModal)
            app.screen.query_one("#run-command", Input).value = "python3 train.py --epochs 2"
            app.screen.query_one("#run-submit", Checkbox).value = False
            await pilot.click("#run-save")
            await _wait_for(lambda: len(RunService(ctx).list_runs()) == 1)

            row = RunService(ctx).list_runs()[0]
            assert row.command.argv == ["python3", "train.py", "--epochs", "2"]
            assert row.state == "planned"

    async def test_new_run_uses_project_resource_defaults_and_relative_sweep(self, ctx):
        assert ctx.project is not None
        ctx.project.config = ctx.project.config.model_copy(
            update={"resources": Resources(time="00:30:00", cpus=4, mem="16G")}
        )
        dump_yaml_model(ctx.project.paths.root / "sweep.yaml", SWEEP)
        app = SlurmDeckApp(ctx)

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewRunModal)
            assert app.screen.query_one("#run-time", Input).value == "00:30:00"
            assert app.screen.query_one("#run-cpus", Input).value == "4"
            assert app.screen.query_one("#run-mem", Input).value == "16G"
            app.screen.query_one("#run-command", Input).value = "python3 train.py"
            app.screen.query_one("#run-sweep", Input).value = "sweep.yaml"
            app.screen.query_one("#run-submit", Checkbox).value = False
            await pilot.click("#run-save")
            await _wait_for(lambda: len(RunService(ctx).list_runs()) == 1)

            row = RunService(ctx).list_runs()[0]
            assert row.summary.total == 2
            assert row.resources.cpus == 4

    async def test_lists_runs_and_counts(self, ctx, submitted_run):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)
            table = app.screen.query_one("#runs-table", KeyedTable)
            assert table.row_count == 1
            assert table.selected_key == submitted_run.id
            assert app.counts_text.startswith("1 run(s)")

    async def test_incremental_update_keeps_cursor_and_rows(self, ctx, remote, submitted_run, fake_transport):
        second = RunService(ctx).plan(command=COMMAND, sweep=SWEEP, overrides=ResourceOverrides(), remote=remote)
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunsScreen)
            table = screen.query_one("#runs-table", KeyedTable)
            assert table.row_count == 2
            await pilot.press("down")
            selected_before = table.selected_key
            # a cell changes (state planned → cancelled) without rows changing
            RunService(ctx).get(second.id)  # sanity: row exists
            from slurmdeck.storage.repos import RunRepo

            RunRepo(ctx.db()).set_state(second.id, "submit_failed")
            screen.reload()
            await pilot.pause()
            assert table.row_count == 2
            assert table.selected_key == selected_before

    async def test_filter_narrows_and_clears(self, ctx, remote, submitted_run):
        RunService(ctx).plan(command=COMMAND, sweep=SWEEP, overrides=ResourceOverrides(), remote=remote, name="special")
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#runs-table", KeyedTable)
            assert table.row_count == 2
            await pilot.press("slash")
            for char in "special":
                await pilot.press(char)
            await _wait_for(lambda: table.row_count == 1)
            await pilot.press("escape")
            await _wait_for(lambda: table.row_count == 2)

    async def test_cancel_flow_requires_confirmation(self, ctx, pending_run, fake_transport):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.click("#no")
            await pilot.pause()
            assert not any(call.startswith("scancel") for call in fake_transport.calls)

            await _wait_for(lambda: isinstance(app.screen, RunsScreen))
            await pilot.press("c")
            await pilot.pause()
            await pilot.click("#yes")
            await _wait_for(lambda: any(call.startswith("scancel") for call in fake_transport.calls))
            await _wait_for(lambda: RunService(ctx).get(pending_run.id).state == "cancelled")

    async def test_cancel_refused_for_terminal_run(self, ctx, submitted_run):
        # the fake cluster already ran every task; refresh marks the run terminal
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _wait_for(lambda: RunService(ctx).get(submitted_run.id).state == "terminal")
            app.screen.reload()
            await pilot.press("c")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmModal)  # guard warned instead

    async def test_operation_feedback_in_status_bar(self, ctx, pending_run, fake_transport):
        import time as _time

        def slow_scancel(command: str) -> ExecResult:
            _time.sleep(0.3)
            return ExecResult(0, "", "")

        fake_transport.handlers["scancel"] = slow_scancel
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            await pilot.click("#yes")
            await _wait_for(lambda: app.operation_text.startswith("Cancelling"))
            await _wait_for(lambda: app.operation_text == "")

    async def test_retry_without_failures_reports_error_and_creates_nothing(self, ctx, submitted_run, fake_transport):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _wait_for(lambda: not app.controller.refreshing)  # let the mount refresh settle
            fake_transport.calls.clear()
            await pilot.press("t")
            await pilot.pause()
            await pilot.click("#yes")
            # the operation ran (its internal refresh hit the cluster) and then failed
            await _wait_for(lambda: any(call.startswith("python3 - scan") for call in fake_transport.calls))
            await _wait_for(lambda: app.controller.operation is None)
            await pilot.pause()
            assert len(RunService(ctx).list_runs()) == 1  # no retry run appeared
            assert app.error_text
            panel = app.screen.query_one(ErrorPanel)
            assert panel.display is True
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert app.error_text == ""
            assert panel.display is False

    async def test_stale_refresh_is_visible_without_erasing_last_success(self, ctx, submitted_run):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await _wait_for(lambda: not app.controller.refreshing)
            app.controller.last_refresh_at = 123.0
            app.on_refresh_finished(RefreshFinished(ok=True, stale=True, error="slurmctld unavailable"))
            await pilot.pause()

            assert app.status_stale is True
            assert app.last_refresh_at == 123.0
            assert app.error_text == "slurmctld unavailable"
            assert app.screen.query_one(ErrorPanel).display is True


class TestNavigation:
    async def test_enter_opens_detail_and_logs_chain(self, ctx, submitted_run):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)
            table = app.screen.query_one("#tasks-table", KeyedTable)
            assert table.row_count == 2

            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LogsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)

    async def test_task_filter_cycle(self, ctx, submitted_run):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RunDetailScreen)
            table = screen.query_one("#tasks-table", KeyedTable)
            assert table.row_count == 2  # both COMPLETED (fake cluster ran them)
            await pilot.press("f")  # active → none match
            await pilot.pause()
            assert table.display is False
            empty = screen.query_one("#tasks-empty", EmptyState)
            assert empty.display is True
            await pilot.press("f")  # failed → none match
            await pilot.press("f")  # back to all
            await pilot.pause()
            assert table.display is True
            assert table.row_count == 2

    async def test_help_overlay_opens_and_closes(self, ctx, submitted_run):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)

    async def test_mode_switching(self, ctx, submitted_run):
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            assert isinstance(app.screen, RemotesScreen)
            await pilot.press("2")
            await pilot.pause()
            assert isinstance(app.screen, EnvsScreen)
            await pilot.press("1")
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)


class TestLogs:
    async def test_log_tail_renders_and_follow_cleans_up(self, ctx, submitted_run):
        run_dir = Path(submitted_run.remote_root)
        log_path = run_dir / "logs" / f"task_{submitted_run.slurm_job_id}_0.out"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("hello from task 0\nline two\n", encoding="utf-8")

        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, LogsScreen)
            await _wait_for(lambda: "hello from task 0" in screen._content)

            await pilot.press("f")  # start following
            await pilot.pause()
            assert screen._handle is not None
            await pilot.press("escape")  # leaving must close the stream handle
            await pilot.pause()
            assert screen._handle is None


class TestEnvs:
    async def test_attach_and_rebuild_route_to_prepare_semantics(self, ctx, monkeypatch):
        assert ctx.project is not None
        ctx.project.config = ctx.project.config.model_copy(update={"env": ExistingEnvSpec(prefix="/shared/existing")})
        digest = "c" * 64
        attempt = EnvBuildAttempt(
            attempt_id="attempt-1",
            status=EnvironmentStatus.QUEUED,
            executor=BuildExecutor.SLURM,
            generation_id="gen-1",
            prefix="/base/gen-1",
            job_id="42",
            created_at="2026-07-11T00:00:00Z",
        )
        record = EnvironmentRecord(
            env_id=f"training-{digest[:12]}",
            full_hash=digest,
            backend=EnvBackend.CONDA,
            ownership=EnvOwnership.MANAGED,
            status=EnvironmentStatus.QUEUED,
            created_at="2026-07-11T00:00:00Z",
            updated_at="2026-07-11T00:00:00Z",
            current_attempt=attempt.attempt_id,
            attempts=[attempt],
            provenance=EnvironmentProvenance(canonical_spec_hash=digest),
        )
        calls: list[bool] = []

        def prepare_env(*, rebuild: bool = False, after=None) -> None:
            calls.append(rebuild)

        app = SlurmDeckApp(ctx)
        monkeypatch.setattr(app.controller, "prepare_env", prepare_env)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("2")
            await _wait_for(lambda: isinstance(app.screen, EnvsScreen))
            screen = app.screen
            assert isinstance(screen, EnvsScreen)
            screen._records = [EnvironmentView(record=record, desired_by_project=True)]
            screen.reload()

            await pilot.press("a")
            await pilot.pause()
            assert calls == [False]

            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.click("#yes")
            await pilot.pause()
            assert calls == [False, True]

    async def test_active_build_logs_and_cancel_workflow(self, ctx, remote, remote_root, fake_transport):
        digest = "b" * 64
        env_id = f"training-{digest[:12]}"
        layout = RemoteLayout(str(remote_root))
        attempt_dir = Path(layout.env_attempt_dir(env_id, "attempt-1"))
        attempt_dir.mkdir(parents=True)
        stdout_path = attempt_dir / "build.out"
        stderr_path = attempt_dir / "build.err"
        stdout_path.write_text("building environment\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        attempt = EnvBuildAttempt(
            attempt_id="attempt-1",
            status=EnvironmentStatus.QUEUED,
            executor=BuildExecutor.SLURM,
            generation_id="gen-1",
            prefix=layout.env_generation_dir(env_id, "gen-1"),
            job_id="42",
            build_dir=str(attempt_dir),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            created_at="2026-07-11T00:00:00Z",
        )
        record = EnvironmentRecord(
            env_id=env_id,
            full_hash=digest,
            backend=EnvBackend.CONDA,
            ownership=EnvOwnership.MANAGED,
            status=EnvironmentStatus.QUEUED,
            created_at="2026-07-11T00:00:00Z",
            updated_at="2026-07-11T00:00:00Z",
            current_attempt=attempt.attempt_id,
            attempts=[attempt],
            provenance=EnvironmentProvenance(canonical_spec_hash=digest),
        )
        EnvRegistryClient().prepare(fake_transport, layout, record)
        fake_transport.squeue_output = "42|PENDING|Resources\n"

        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("2")
            await _wait_for(lambda: app.screen.query_one("#envs-table", KeyedTable).row_count == 1)
            await pilot.press("l")
            await _wait_for(lambda: isinstance(app.screen, EnvLogsScreen))
            log_screen = app.screen
            assert isinstance(log_screen, EnvLogsScreen)
            await _wait_for(lambda: "building environment" in log_screen._content)
            await pilot.press("escape")
            await _wait_for(lambda: isinstance(app.screen, EnvsScreen))

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.click("#yes")
            await _wait_for(
                lambda: EnvRegistryClient().inspect(fake_transport, layout)[0].status is EnvironmentStatus.CANCELLED
            )

    async def test_gc_previews_before_deleting(self, ctx, remote_root):
        candidate = remote_root / "envs" / "trash" / "old-aaaaaaaaaaaa" / "gen-old"
        candidate.mkdir(parents=True)
        (candidate / "payload").write_text("old", encoding="utf-8")
        app = SlurmDeckApp(ctx)

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("2")
            await _wait_for(lambda: isinstance(app.screen, EnvsScreen))
            await pilot.press("g")
            await _wait_for(lambda: isinstance(app.screen, ConfirmModal))
            assert candidate.exists()
            await pilot.click("#yes")
            await _wait_for(lambda: not candidate.exists())

    async def test_envs_listing_and_remove_flow(self, ctx, remote, remote_root, submitted_run):
        full_hash = "a" * 64
        env_id = f"ml-{full_hash[:12]}"
        layout = RemoteLayout(str(remote_root))
        prefix = Path(layout.env_generation_dir(env_id, "gen-1"))
        prefix.mkdir(parents=True)
        provenance = EnvironmentProvenance(canonical_spec_hash=full_hash)
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
            full_hash=full_hash,
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
        EnvRegistryClient().prepare(ctx.transport(remote), layout, record)
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("2")
            await _wait_for(lambda: isinstance(app.screen, EnvsScreen))
            screen = app.screen
            table = screen.query_one("#envs-table", KeyedTable)
            await _wait_for(lambda: table.row_count == 1)
            assert table.selected_key == env_id

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.click("#yes")
            await _wait_for(lambda: not prefix.exists())
            await _wait_for(lambda: table.row_count == 0)


class TestOutsideProject:
    async def test_starts_on_remotes_and_runs_mode_shows_guidance(self, user_paths, remote, fake_transport, tmp_path):
        ctx = AppContext.create(
            cwd=tmp_path / "not-a-project",
            user_paths=user_paths,
            transport_factory=lambda _remote: fake_transport,
        )
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RemotesScreen)
            table = app.screen.query_one("#remotes-table", KeyedTable)
            assert table.row_count == 1

            await pilot.press("1")  # runs section must degrade, not crash
            await pilot.pause()
            assert isinstance(app.screen, RunsScreen)
            empty = app.screen.query_one("#runs-empty", EmptyState)
            assert empty.display is True

    async def test_no_remotes_guidance(self, tmp_path):
        from slurmdeck.storage.paths import UserPaths

        ctx = AppContext.create(cwd=tmp_path, user_paths=UserPaths(config_dir=tmp_path / "cfg"))
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RemotesScreen)
            empty = app.screen.query_one("#remotes-empty", EmptyState)
            assert empty.display is True

    async def test_add_remote_form_supports_ssh_alias(self, tmp_path):
        from slurmdeck.services.context import AppContext
        from slurmdeck.storage.paths import UserPaths

        ctx = AppContext.create(cwd=tmp_path, user_paths=UserPaths(config_dir=tmp_path / "cfg"))
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, RemoteFormModal)
            app.screen.query_one("#remote-name", Input).value = "cluster"
            app.screen.query_one("#remote-method", Select).value = "ssh_alias"
            app.screen.query_one("#remote-destination", Input).value = "example-cluster"
            app.screen.query_one("#remote-base", Input).value = "$DATA/slurmdeck"
            app.screen.query_one("#remote-host-key-policy", Select).value = "strict"
            await pilot.click("#remote-save")
            await _wait_for(lambda: "cluster" in ctx.user_store.list_remote_names())

            remote = ctx.user_store.read_remote("cluster")
            assert remote.ssh_alias == "example-cluster"
            assert remote.host is None
            assert remote.host_key_policy == "strict"


class TestProfileWorkflow:
    async def test_import_preview_and_explicit_save(self, ctx, tmp_path):
        profile_path = tmp_path / "profile.yaml"
        profile_path.write_text(
            """schema_version: 1
allowed_build_executors: [slurm]
default_build_executor: slurm
login_build_policy: forbidden
shared_filesystem: {login_to_compute: true}
module_initialization: {strategy: none}
conda: {executable: conda}
network: {compute_access: full, channel_access: direct}
slurm:
  partition: devel
  afterok_dependency: false
  kill_invalid_dependency: unsupported
platform: {system: Linux, machine: x86_64, conda_subdir: linux-64}
""",
            encoding="utf-8",
        )
        app = SlurmDeckApp(ctx)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, ProfileModal)
            app.screen.query_one("#profile-import-path", Input).value = str(profile_path)
            await pilot.click("#profile-import")
            await pilot.pause()
            assert "partition" in app.screen.query_one("#profile-diff", TextArea).text
            await pilot.click("#profile-save")
            await _wait_for(lambda: ctx.user_store.read_remote("cluster").cluster is not None)

            profile = ctx.user_store.read_remote("cluster").cluster
            assert profile is not None
            assert profile.slurm.partition == "devel"

    async def test_doctor_remains_read_only_without_save_controls(self, ctx):
        app = SlurmDeckApp(ctx)
        before = ctx.user_store.read_remote("cluster")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.press("d")
            await _wait_for(lambda: isinstance(app.screen, DoctorScreen))
            await _wait_for(lambda: app.screen.query_one("#doctor-page-table", KeyedTable).row_count > 0)

            assert not app.screen.query("#profile-save")
            assert ctx.user_store.read_remote("cluster") == before
