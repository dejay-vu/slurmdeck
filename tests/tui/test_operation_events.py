from __future__ import annotations

from collections.abc import Callable
from typing import Any

from slurmdeck.models.resources import ResourceOverrides
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.operations import OperationEvent, OperationPhase, OperationStatus
from slurmdeck.services.runs import RunService
from slurmdeck.tui.app import SlurmDeckApp
from slurmdeck.tui.controller import DeckController
from slurmdeck.tui.messages import OperationFinished, OperationProgressed, OperationStarted, RefreshFinished


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
