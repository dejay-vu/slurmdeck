from __future__ import annotations

import json

from slurmdeck.errors import StructuredError, UserError
from slurmdeck.operations import OperationPhase
from slurmdeck.transport.errors import TransportError


def test_structured_error_has_exact_public_fields_and_debug_only_cause() -> None:
    cause = ValueError("socket closed")
    error = StructuredError(
        code="remote_unavailable",
        summary="Remote unavailable",
        detail="The connection closed during a probe.",
        operation="remote.connect",
        phase=OperationPhase.PROBE,
        retryable=True,
        remediation="Retry after checking the login node.",
        context={"remote": "hpc"},
        underlying_cause=cause,
    )

    assert set(StructuredError.model_fields) == {
        "code",
        "summary",
        "detail",
        "operation",
        "phase",
        "retryable",
        "remediation",
        "context",
        "underlying_cause",
    }
    assert "underlying_cause" not in error.model_dump()
    assert "underlying_cause" not in json.loads(error.model_dump_json())
    assert error.model_dump_debug()["underlying_cause"] == "ValueError('socket closed')"
    assert json.loads(error.model_dump_json_debug())["underlying_cause"] == "ValueError('socket closed')"


def test_user_error_keeps_legacy_string_hint_and_exposes_structured_error() -> None:
    error = UserError("No remote selected.", hint="Select one with `slurmdeck remote use`.")

    assert error.message == "No remote selected."
    assert error.hint == "Select one with `slurmdeck remote use`."
    assert str(error) == "No remote selected.\n  hint: Select one with `slurmdeck remote use`."
    assert error.exit_code == 1
    assert error.error == StructuredError(
        code="user_error",
        summary="No remote selected.",
        remediation="Select one with `slurmdeck remote use`.",
    )


def test_user_error_can_wrap_a_structured_error() -> None:
    structured = StructuredError(
        code="run_not_found",
        summary="Run not found.",
        detail="No run has id demo-1.",
        operation="run.show",
        phase=OperationPhase.VALIDATE,
        remediation="List runs and choose an existing id.",
        context={"run_id": "demo-1"},
    )

    error = UserError(structured)

    assert error.error is structured
    assert error.message == "Run not found."
    assert error.hint == "List runs and choose an existing id."
    assert str(error) == "Run not found.\n  hint: List runs and choose an existing id."


def test_transport_error_keeps_exit_code_and_has_a_structured_payload() -> None:
    error = TransportError("remote command failed", command="false", returncode=1, stderr="first\nlast")

    assert error.exit_code == 3
    assert str(error) == "remote command failed\n  first\n  last"
    assert error.error.code == "transport_error"
    assert error.error.context == {"command": "false", "returncode": 1}
    assert "underlying_cause" not in error.error.model_dump()
