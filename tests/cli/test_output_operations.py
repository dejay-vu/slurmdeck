from __future__ import annotations

from io import StringIO
from typing import Any

from rich.console import Console

from slurmdeck.cli import _output
from slurmdeck.operations import OperationEvent, OperationPhase, OperationStatus


def test_non_tty_activity_renders_only_operation_and_phase_boundaries(monkeypatch) -> None:
    stream = StringIO()
    monkeypatch.setattr(_output, "err_console", Console(file=stream, force_terminal=False, color_system=None))

    with _output.activity("Preparing") as sink:
        sink(
            OperationEvent(
                operation="snapshot.ensure",
                phase=OperationPhase.UPLOAD,
                status=OperationStatus.STARTED,
                message="Uploading snapshot",
            )
        )
        sink(
            OperationEvent(
                operation="snapshot.ensure",
                phase=OperationPhase.UPLOAD,
                status=OperationStatus.PROGRESS,
                current=1,
                total=2,
                message="Still uploading",
            )
        )
        sink(
            OperationEvent(
                operation="run.submit",
                phase=OperationPhase.SUBMIT,
                status=OperationStatus.STARTED,
                message="Submitting job",
            )
        )

    assert stream.getvalue().splitlines() == [
        "operation: Preparing",
        "upload: Uploading snapshot",
        "submit: Submitting job",
    ]


def test_non_tty_activity_uses_phase_name_when_event_has_no_message(monkeypatch) -> None:
    stream = StringIO()
    monkeypatch.setattr(_output, "err_console", Console(file=stream, force_terminal=False, color_system=None))
    event = OperationEvent(
        operation="status.refresh",
        phase=OperationPhase.REFRESH,
        status=OperationStatus.COMPLETED,
    )

    with _output.activity("Refreshing") as sink:
        sink(event)

    assert stream.getvalue().splitlines() == ["operation: Refreshing", "refresh: refresh"]


class FakeProgress:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.updates: list[tuple[int, dict[str, Any]]] = []

    def add_task(self, _description: str, *, total: int | None) -> int:
        assert total is None
        return 7

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def update(self, task_id: int, **kwargs: Any) -> None:
        self.updates.append((task_id, kwargs))


class FakeTimer:
    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.cancelled = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback()


def test_tty_progress_is_delayed_and_updates_typed_counts() -> None:
    progress = FakeProgress()
    timers: list[FakeTimer] = []

    def timer_factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    renderer = _output._DelayedProgress(
        Console(file=StringIO(), force_terminal=True),
        "Preparing",
        progress=progress,
        timer_factory=timer_factory,
    )

    assert timers[0].delay == 0.2
    assert progress.started == 0
    timers[0].fire()
    assert progress.started == 1

    renderer(
        OperationEvent(
            operation="snapshot.ensure",
            phase=OperationPhase.UPLOAD,
            status=OperationStatus.PROGRESS,
            current=2,
            total=5,
            elapsed=0.4,
            message="Uploading",
        )
    )
    renderer.close()

    assert progress.updates[-1] == (
        7,
        {"description": "Uploading", "completed": 2, "total": 5, "refresh": True},
    )
    assert progress.stopped == 1


def test_tty_short_activity_never_starts_progress() -> None:
    progress = FakeProgress()
    timers: list[FakeTimer] = []

    def timer_factory(delay, callback):
        timer = FakeTimer(delay, callback)
        timers.append(timer)
        return timer

    renderer = _output._DelayedProgress(
        Console(file=StringIO(), force_terminal=True),
        "Fast operation",
        progress=progress,
        timer_factory=timer_factory,
    )
    renderer.close()
    timers[0].fire()

    assert progress.started == 0
    assert progress.stopped == 0
