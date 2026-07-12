from __future__ import annotations

import time

import pytest

from slurmdeck.transport import ExecResult
from slurmdeck.transport.errors import TransportError


def test_fake_transport_supports_delays_scripts_and_call_type_counters(fake_transport) -> None:
    fake_transport.set_delay("exec:true", 0.02)
    fake_transport.script_call(
        "exec:true",
        ExecResult(0, "first\n", ""),
        ExecResult(0, "second\n", ""),
    )

    started = time.perf_counter()
    assert fake_transport.exec("true").stdout == "first\n"
    assert fake_transport.exec("true").stdout == "second\n"

    assert time.perf_counter() - started >= 0.04
    assert fake_transport.call_counts["exec"] == 2
    assert fake_transport.call_counts["exec:true"] == 2
    fake_transport.reset_metrics()
    assert fake_transport.call_counts == {}
    assert fake_transport.calls == []


def test_fake_transport_named_fault_points_cover_env_login_and_afterok(fake_transport) -> None:
    fake_transport.script_call("env:inspect", TransportError("env fault"))
    with pytest.raises(TransportError, match="env fault"):
        fake_transport.exec_python("ignored", ["inspect"], check=False)

    login_request = '{"executor":"login"}'
    fake_transport.script_call("login", TransportError("login fault"))
    with pytest.raises(TransportError, match="login fault"):
        fake_transport.exec_python("ignored", ["prepare-build", "--request-json", login_request], check=False)

    fake_transport.script_call("afterok", TransportError("afterok fault"))
    with pytest.raises(TransportError, match="afterok fault"):
        fake_transport.exec_python(
            "ignored",
            ["submit-run", "--dependency-job-id", "42"],
            check=False,
        )

    assert fake_transport.call_counts["env:inspect"] == 1
    assert fake_transport.call_counts["login"] == 1
    assert fake_transport.call_counts["afterok"] == 1
