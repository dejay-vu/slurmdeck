from __future__ import annotations

from collections.abc import Callable
from typing import Any

from slurmdeck.models.remote import Remote
from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.models.status import RunSummary
from slurmdeck.operations import OperationEvent, OperationPhase, OperationStatus
from slurmdeck.services.runs import RunService
from slurmdeck.services.status import RefreshReport, StatusService
from slurmdeck.storage.repos import RunRepo
from slurmdeck.tui.app import SlurmDeckApp
from slurmdeck.tui.controller import DeckController
from slurmdeck.tui.messages import OperationFinished, OperationProgressed, OperationStarted, RefreshFinished
from slurmdeck.tui.screens import DeckScreen


class ImmediateApp:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.notifications: list[str] = []

    def post_message(self, message: object) -> None:
        self.messages.append(message)

    def run_worker(self, work: Callable[[], None], **_kwargs: object) -> None:
        work()

    def notify(self, message: str, **_kwargs: object) -> None:
        self.notifications.append(message)

    def call_from_thread(self, callback: Callable[..., Any], *args: object) -> None:
        callback(*args)


class QueuedApp(ImmediateApp):
    def __init__(self) -> None:
        super().__init__()
        self.workers: list[tuple[Callable[[], None], dict[str, object]]] = []

    def run_worker(self, work: Callable[[], None], **kwargs: object) -> None:
        self.workers.append((work, kwargs))


def test_controller_bridges_typed_event_to_tui_message(ctx) -> None:
    app = ImmediateApp()
    controller = DeckController(app, ctx)  # type: ignore[arg-type]
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.UPLOAD,
        status=OperationStatus.PROGRESS,
        elapsed=0.5,
        message="Uploading run directory",
    )

    assert controller.run_operation("Submitting", lambda sink: sink(event))

    progressed = next(message for message in app.messages if isinstance(message, OperationProgressed))
    assert progressed.event is event
    assert not hasattr(progressed, "detail")


def test_controller_forwards_synthetic_operation_event_without_string_projection(ctx) -> None:
    app = ImmediateApp()
    controller = DeckController(app, ctx)  # type: ignore[arg-type]

    assert controller.run_operation(
        "Cleaning demo",
        lambda _sink: None,
        operation="run.clean",
        phase=OperationPhase.CLEANUP,
    )

    progressed = [message for message in app.messages if isinstance(message, OperationProgressed)]
    assert len(progressed) == 2
    assert progressed[0].event.status is OperationStatus.STARTED
    assert progressed[-1].event.status is OperationStatus.COMPLETED


def test_read_only_workers_do_not_share_the_mutation_lock(ctx) -> None:
    app = QueuedApp()
    controller = DeckController(app, ctx)  # type: ignore[arg-type]

    assert controller.run_operation("Mutating", lambda _sink: None)
    assert controller.operation == "Mutating"
    assert controller.run_operation("Reading", lambda _sink: None, mutation=False)
    assert not controller.run_operation("Second mutation", lambda _sink: None)

    assert app.workers[0][1]["group"] == "deck-mutations"
    assert app.workers[1][1]["group"] == "deck-read"
    assert app.workers[0][1]["exclusive"] is True
    assert app.workers[1][1]["exclusive"] is False


def test_tui_feedback_keeps_mutation_visible_when_a_read_finishes(ctx) -> None:
    app = SlurmDeckApp(ctx)
    app.on_operation_started(OperationStarted("mutation:1", "Submitting", mutation=True, started_at=10.0))
    app.on_operation_started(OperationStarted("read:2", "Loading", mutation=False, started_at=11.0))

    assert app.operation_text == "Submitting"

    app.on_operation_finished(OperationFinished("read:2", "Loading", ok=True))

    assert app.operation_text == "Submitting"
    assert app.operation_started_at == 10.0


def test_reload_screens_skips_a_stopped_screen(ctx) -> None:
    class ReloadTrackingScreen(DeckScreen):
        reload_count = 0

        def reload(self) -> None:
            self.reload_count += 1

    app = SlurmDeckApp(ctx)
    screen = ReloadTrackingScreen()
    app._screen_stacks[app.DEFAULT_MODE].append(screen)
    screen._running = True

    app._reload_screens()
    assert screen.reload_count == 1

    screen._running = False

    app._reload_screens()
    assert screen.reload_count == 1


def test_controller_reports_cached_refresh_failure_without_overwriting_last_success(
    ctx,
    remote,
    fake_transport,
) -> None:
    fake_transport.simulate_execution = False
    runs = RunService(ctx)
    row = runs.submit(
        fake_transport,
        runs.plan(
            command=CommandTemplateSpec(argv=["python3", "-c", "print(1)"]),
            overrides=ResourceOverrides(),
            remote=remote,
        ).id,
    )
    app = ImmediateApp()
    controller = DeckController(app, ctx)  # type: ignore[arg-type]
    controller.refresh_now([row.id])
    assert next(message for message in reversed(app.messages) if isinstance(message, RefreshFinished)).ok is True
    controller.last_refresh_at = 123.0
    controller.connection = "ok"

    fake_transport.squeue_returncode = 7
    fake_transport.squeue_stderr = "slurmctld unavailable"
    controller.refresh_now([row.id])

    finished = next(message for message in reversed(app.messages) if isinstance(message, RefreshFinished))
    assert finished.ok is True
    assert finished.stale is True
    assert "slurmctld unavailable" in finished.error
    assert controller.last_refresh_at == 123.0
    assert controller.connection == "ok"


def test_global_refresh_skips_settled_cancelled_run_from_removed_remote_but_keeps_settling_runs(
    ctx,
    remote,
    remote_root,
    monkeypatch,
) -> None:
    runs = RunService(ctx)
    active = runs.plan(
        command=CommandTemplateSpec(argv=["true"]),
        overrides=ResourceOverrides(),
        remote=remote,
    )
    repo = RunRepo(ctx.db())
    repo.set_state(active.id, "submitted")

    retired = Remote(
        name="retired",
        host="user@retired.example.com",
        base=str(remote_root / "retired"),
        resolved_base=str(remote_root / "retired"),
    )
    ctx.user_store.add_remote(retired)
    cancelled = runs.plan(
        command=CommandTemplateSpec(argv=["true"]),
        overrides=ResourceOverrides(),
        remote=retired,
    )
    repo.set_state(cancelled.id, "cancelled")
    repo.set_summary(cancelled.id, RunSummary(total=1, counts={"CANCELLED": 1}))
    ctx.user_store.remove_remote(retired.name)

    settling = runs.plan(
        command=CommandTemplateSpec(argv=["true"]),
        overrides=ResourceOverrides(),
        remote=remote,
    )
    repo.set_state(settling.id, "cancelled")
    repo.set_summary(settling.id, RunSummary(total=1, counts={"RUNNING": 1}))

    refreshed: list[list[str]] = []

    def record_refresh(
        _service: StatusService,
        _transport: object,
        _layout: object,
        run_ids: list[str] | None = None,
        **_kwargs: object,
    ) -> RefreshReport:
        assert run_ids is not None
        refreshed.append(run_ids)
        return RefreshReport(refreshed=run_ids)

    monkeypatch.setattr(StatusService, "refresh", record_refresh)
    app = ImmediateApp()
    controller = DeckController(app, ctx)  # type: ignore[arg-type]

    controller.refresh_now()

    assert len(refreshed) == 1
    assert set(refreshed[0]) == {active.id, settling.id}
    finished = next(message for message in reversed(app.messages) if isinstance(message, RefreshFinished))
    assert finished.ok is True
