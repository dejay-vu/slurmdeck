from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import get_type_hints

import pytest
from pydantic import PydanticDeprecatedSince20, ValidationError

from slurmdeck.operations import (
    OperationEvent,
    OperationPhase,
    OperationReporter,
    OperationStatus,
    noop_operation_sink,
)


def _clock(values: list[float]) -> Iterator[float]:
    yield from values


def test_operation_event_validates_fields_and_is_frozen() -> None:
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.SUBMIT,
        status=OperationStatus.PROGRESS,
        current=1,
        total=3,
        elapsed=0.25,
        message="Submitting Slurm array",
        result_counts={"submitted": 1},
    )

    assert event.model_dump(mode="json") == {
        "operation": "run.submit",
        "phase": "submit",
        "status": "progress",
        "current": 1,
        "total": 3,
        "elapsed": 0.25,
        "message": "Submitting Slurm array",
        "result_counts": {"submitted": 1},
    }
    with pytest.raises(ValidationError, match="Instance is frozen"):
        event.message = "changed"
    with pytest.raises(ValidationError):
        OperationEvent(operation="run.submit", phase="not-a-phase", status="progress")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OperationEvent(operation="run.submit", phase="submit", status="progress", surprise=True)


def test_operation_event_result_counts_keep_exact_dict_contract() -> None:
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.SUBMIT,
        status=OperationStatus.COMPLETED,
        result_counts={"submitted": 1},
    )

    assert get_type_hints(OperationEvent)["result_counts"] == dict[str, int]
    assert isinstance(event.result_counts, dict)
    assert event.result_counts == {"submitted": 1}


def test_operation_event_result_counts_block_all_public_mutations() -> None:
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.SUBMIT,
        status=OperationStatus.COMPLETED,
        result_counts={"submitted": 1},
    )
    counts = event.result_counts

    with pytest.raises(TypeError, match="immutable"):
        counts["submitted"] = 2
    with pytest.raises(TypeError, match="immutable"):
        del counts["submitted"]
    with pytest.raises(TypeError, match="immutable"):
        counts.clear()
    with pytest.raises(TypeError, match="immutable"):
        counts.pop("submitted")
    with pytest.raises(TypeError, match="immutable"):
        counts.popitem()
    with pytest.raises(TypeError, match="immutable"):
        counts.setdefault("failed", 1)
    with pytest.raises(TypeError, match="immutable"):
        counts.update({"failed": 1})
    with pytest.raises(TypeError, match="immutable"):
        counts |= {"failed": 1}

    assert counts == {"submitted": 1}


def test_operation_event_result_count_copies_stay_dicts_and_immutable() -> None:
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.SUBMIT,
        status=OperationStatus.COMPLETED,
        result_counts={"submitted": 1},
    )

    for copied in (event.result_counts.copy(), copy.copy(event.result_counts), copy.deepcopy(event.result_counts)):
        assert isinstance(copied, dict)
        assert copied == {"submitted": 1}
        with pytest.raises(TypeError, match="immutable"):
            copied["submitted"] = 2


def test_operation_event_model_copy_revalidates_and_refreezes_counts() -> None:
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.SUBMIT,
        status=OperationStatus.COMPLETED,
        result_counts={"submitted": 1},
    )

    for copied in (
        event.model_copy(),
        event.model_copy(deep=True),
        event.model_copy(update={"result_counts": {"failed": 2}}),
    ):
        assert isinstance(copied.result_counts, dict)
        with pytest.raises(TypeError, match="immutable"):
            copied.result_counts["new"] = 1

    updated = event.model_copy(update={"result_counts": {"failed": 2}})
    assert updated.result_counts == {"failed": 2}
    with pytest.raises(ValidationError):
        event.model_copy(update={"result_counts": {"failed": "not-an-int"}})  # type: ignore[dict-item]


def test_operation_event_deprecated_copy_keeps_counts_immutable_and_serializable() -> None:
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.SUBMIT,
        status=OperationStatus.COMPLETED,
        result_counts={"submitted": 1},
    )

    for options in ({}, {"deep": True}, {"update": {"result_counts": {"failed": 2}}}):
        with pytest.warns(PydanticDeprecatedSince20):
            copied = event.copy(**options)  # type: ignore[arg-type]
        assert isinstance(copied.result_counts, dict)
        assert copied.model_dump()["result_counts"] == dict(copied.result_counts)
        assert json.loads(copied.model_dump_json())["result_counts"] == dict(copied.result_counts)
        with pytest.raises(TypeError, match="immutable"):
            copied.result_counts["new"] = 1

    with pytest.warns(PydanticDeprecatedSince20), pytest.raises(ValidationError):
        event.copy(update={"result_counts": {"failed": "not-an-int"}})  # type: ignore[dict-item]


def test_operation_event_result_counts_serialize_as_a_plain_dict() -> None:
    event = OperationEvent(
        operation="run.submit",
        phase=OperationPhase.SUBMIT,
        status=OperationStatus.COMPLETED,
        result_counts={"submitted": 1},
    )

    with pytest.raises(TypeError):
        event.result_counts["submitted"] = 2

    assert event.model_dump()["result_counts"] == {"submitted": 1}
    assert json.loads(event.model_dump_json())["result_counts"] == {"submitted": 1}


def test_operation_reporter_uses_one_monotonic_start_time() -> None:
    events: list[OperationEvent] = []
    ticks = _clock([100.0, 100.25, 100.75, 101.5])
    reporter = OperationReporter("run.submit", events.append, clock=lambda: next(ticks))

    started = reporter.started(OperationPhase.SNAPSHOT, message="Preparing snapshot")
    progressed = reporter.progress(OperationPhase.UPLOAD, current=1, total=2, message="Uploading run")
    completed = reporter.completed(OperationPhase.SUBMIT, result_counts={"submitted": 1})

    assert events == [started, progressed, completed]
    assert [event.elapsed for event in events] == [0.25, 0.75, 1.5]
    assert [event.status for event in events] == [
        OperationStatus.STARTED,
        OperationStatus.PROGRESS,
        OperationStatus.COMPLETED,
    ]
    assert all(event.operation == "run.submit" for event in events)


def test_noop_operation_sink_accepts_an_event() -> None:
    event = OperationEvent(operation="status.refresh", phase="refresh", status="started")

    assert noop_operation_sink(event) is None
