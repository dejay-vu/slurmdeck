from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from slurmdeck.cli._output import OutputManager
from slurmdeck.presentation import resolve_theme
from slurmdeck.structured_errors import StructuredError


def _manager(*, width: int, terminal: bool) -> tuple[OutputManager, StringIO, StringIO]:
    stdout = StringIO()
    stderr = StringIO()
    theme = resolve_theme("mono", environ={})
    return (
        OutputManager(
            Console(
                file=stdout,
                width=width,
                height=25,
                force_terminal=terminal,
                color_system=None,
                theme=theme.rich_theme(),
            ),
            Console(
                file=stderr,
                width=width,
                height=25,
                force_terminal=terminal,
                color_system=None,
                theme=theme.rich_theme(),
            ),
        ),
        stdout,
        stderr,
    )


def test_json_success_is_one_stable_envelope_and_never_uses_stderr() -> None:
    output, stdout, stderr = _manager(width=80, terminal=False)

    output.emit_json({"answer": 42}, meta={"stale": False})

    assert stderr.getvalue() == ""
    document = json.loads(stdout.getvalue())
    assert list(document) == ["schema_version", "ok", "data", "meta", "error"]
    assert document == {
        "schema_version": 1,
        "ok": True,
        "data": {"answer": 42},
        "meta": {"stale": False},
        "error": None,
    }


def test_json_error_is_one_stable_envelope_and_never_uses_stderr() -> None:
    output, stdout, stderr = _manager(width=80, terminal=False)
    structured = StructuredError(code="missing_run", summary="Run not found", remediation="Check the run id.")

    output.emit_error(structured)

    assert stderr.getvalue() == ""
    document = json.loads(stdout.getvalue())
    assert document["schema_version"] == 1
    assert document["ok"] is False
    assert document["data"] is None
    assert document["meta"] == {}
    assert document["error"]["code"] == "missing_run"
    assert document["error"]["remediation"] == "Check the run id."


@pytest.mark.parametrize(
    ("width", "terminal", "layout"),
    [(120, True, "table"), (119, True, "double"), (80, True, "double"), (79, True, "key_value")],
)
def test_record_layout_adapts_to_terminal_width(width: int, terminal: bool, layout: str) -> None:
    output, _, _ = _manager(width=width, terminal=terminal)
    assert output.record_layout == layout


def test_non_terminal_records_are_complete_even_at_a_narrow_console_width() -> None:
    output, stdout, _ = _manager(width=20, terminal=False)
    identifier = "run-20260711-very-long-identifier-that-must-never-be-truncated"

    output.records("Runs", ["RUN", "STATE"], [[identifier, "COMPLETED"]])

    rendered = stdout.getvalue()
    assert output.record_layout == "key_value"
    assert identifier in rendered
    assert "…" not in rendered


def test_non_terminal_details_keep_complete_prefixes_and_timestamps() -> None:
    output, stdout, _ = _manager(width=20, terminal=False)
    prefix = "/very/long/shared/filesystem/environment/prefix/that-must-stay-complete"
    timestamp = "2026-07-11T15:45:12Z"

    output.details("Environment", [("Prefix", prefix), ("Updated", timestamp)])

    rendered = stdout.getvalue()
    assert prefix in rendered
    assert timestamp in rendered
    assert "…" not in rendered


def test_medium_terminal_uses_two_lines_per_short_record() -> None:
    output, stdout, _ = _manager(width=100, terminal=True)

    output.records("Runs", ["RUN", "STATE", "TASKS", "JOB"], [["run-1", "READY", "4", "99"]])

    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert lines[0].strip() == "Runs"
    assert len(lines[1:]) == 2
    assert "RUN: run-1" in lines[1]
    assert "TASKS: 4" in lines[2]


@pytest.mark.parametrize(
    ("width", "layout"),
    [(60, "key_value"), (80, "double"), (100, "double"), (120, "table")],
)
def test_supported_cli_widths_lock_actual_rich_layout(width: int, layout: str) -> None:
    output, stdout, _ = _manager(width=width, terminal=True)

    output.records(
        "Runs",
        ["RUN", "STATE", "TASKS", "JOB"],
        [["run-complete-id", Text("READY", style="state.success"), "4", "999001"]],
    )

    rendered = stdout.getvalue()
    assert output.record_layout == layout
    assert "run-complete-id" in rendered
    assert "READY" in rendered
    assert "…" not in rendered
    if layout == "key_value":
        assert "RUN: run-complete-id" in rendered
        assert "JOB: 999001" in rendered
    elif layout == "double":
        lines = [line for line in rendered.splitlines() if line.strip()]
        assert len(lines) == 3
        assert "RUN: run-complete-id" in lines[1]
        assert "TASKS: 4" in lines[2]
    else:
        assert "─" in rendered
        assert not set("┏┓┗┛│").intersection(rendered)


def test_wide_table_uses_semantic_colors_and_only_a_header_rule() -> None:
    theme = resolve_theme("dark", environ={})
    stdout = StringIO()
    output = OutputManager(
        Console(
            file=stdout,
            width=120,
            height=25,
            force_terminal=True,
            color_system="truecolor",
            no_color=theme.no_color,
            theme=theme.rich_theme(),
        ),
        Console(file=StringIO(), force_terminal=True, no_color=theme.no_color, theme=theme.rich_theme()),
    )

    output.records("Runs", ["RUN", "STATE"], [["run-1", Text("READY", style="state.success")]])

    rendered = stdout.getvalue()
    plain = _ANSI.sub("", rendered)
    rules = [line for line in plain.splitlines() if "─" in line]
    assert len(rules) == 1
    assert not set("┏┓┗┛│").intersection(plain)
    assert "38;2;34;211;238" in rendered  # table title / accent role
    assert "38;2;56;189;248" in rendered  # header / information role


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _themed_records(theme_name: str) -> str:
    theme = resolve_theme(theme_name, environ={})
    stdout = StringIO()
    output = OutputManager(
        Console(
            file=stdout,
            width=100,
            height=25,
            force_terminal=True,
            color_system="truecolor",
            no_color=theme.no_color,
            theme=theme.rich_theme(),
        ),
        Console(file=StringIO(), force_terminal=True, no_color=theme.no_color, theme=theme.rich_theme()),
    )
    output.records("Runs", ["RUN", "STATE"], [["run-1", Text("READY", style="state.success")]])
    return stdout.getvalue()


def test_cli_themes_preserve_content_but_change_visual_styles() -> None:
    dark = _themed_records("dark")
    light = _themed_records("light")
    mono = _themed_records("mono")

    assert _ANSI.sub("", dark) == _ANSI.sub("", light) == _ANSI.sub("", mono)
    assert dark != light
    assert "38;2;" in dark
    assert "38;2;" in light
    assert "38;2;" not in mono


def test_cli_no_color_resolves_to_the_mono_visual_contract() -> None:
    no_color = resolve_theme("dark", environ={"NO_COLOR": "1"})
    assert no_color.name == "mono"
    assert no_color.rich_styles == resolve_theme("mono", environ={}).rich_styles
    assert no_color.no_color is True


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_RUNTIME_DIR"] = str(tmp_path / "runtime")
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    return subprocess.run(
        [sys.executable, "-m", "slurmdeck.cli.main", *args],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_json_subprocess_success_has_one_document_and_empty_stderr(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "remote", "list", "--json")

    assert result.returncode == 0
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["ok"] is True
    assert document["data"] == []


def test_json_subprocess_application_error_has_one_document_and_empty_stderr(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "run", "show", "missing", "--json")

    assert result.returncode == 1
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert document["data"] is None
    assert document["error"]["code"] == "user_error"


def test_typer_usage_errors_remain_native_even_when_json_is_present(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "run", "list", "--not-an-option", "--json")

    assert result.returncode == 2
    assert "Usage:" in result.stderr
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)


def test_json_and_watch_are_rejected_with_an_application_envelope(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "run", "status", "--json", "--watch")

    assert result.returncode == 1
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert "--json cannot be combined with --watch" in document["error"]["summary"]


@pytest.mark.parametrize(
    "args",
    [
        ("run", "logs", "--json", "--follow"),
        ("env", "logs", "env-123456789abc", "--json", "--follow"),
    ],
)
def test_json_and_follow_are_rejected_before_context_resolution(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _run_cli(tmp_path, *args)

    assert result.returncode == 1
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert "--json cannot be combined with --follow" in document["error"]["summary"]
