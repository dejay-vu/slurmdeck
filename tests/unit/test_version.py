from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml


def test_pyproject_declares_first_release_version() -> None:
    root = Path(__file__).parents[2]
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)

    assert pyproject["project"]["version"] == "0.1.0"


def test_pyproject_declares_verified_dependency_floors() -> None:
    root = Path(__file__).parents[2]
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)

    assert set(pyproject["project"]["dependencies"]) >= {
        "PyYAML>=6.0.1",
        "rich>=13.8",
        "textual>=8.0",
    }
    assert set(pyproject["project"]["optional-dependencies"]["dev"]) >= {
        "mypy>=2.2.0",
        "pytest-asyncio>=0.23.5",
        "ruff>=0.15.21",
    }
    assert pyproject["tool"]["ruff"]["required-version"] == ">=0.15.21"


def test_supported_python_versions_match_ci_and_package_metadata() -> None:
    root = Path(__file__).parents[2]
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    ci = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

    supported = {"3.11", "3.12", "3.13", "3.14"}
    classifiers = set(pyproject["project"]["classifiers"])
    assert pyproject["project"]["requires-python"] == ">=3.11"
    assert {
        version for version in supported if f"Programming Language :: Python :: {version}" in classifiers
    } == supported
    assert set(ci["jobs"]["checks"]["strategy"]["matrix"]["python-version"]) == supported


def test_release_workflow_uses_least_privilege_oidc_and_pinned_actions() -> None:
    root = Path(__file__).parents[2]
    path = root / ".github" / "workflows" / "release.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    publish = workflow["jobs"]["publish"]

    assert workflow["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert publish["needs"] == "build"
    assert all("run" not in step for step in publish["steps"])
    assert "PYPI_TOKEN" not in text

    action_refs = [
        step["uses"].rsplit("@", 1)[1] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]
    assert action_refs
    assert all(len(ref) == 40 and all(character in "0123456789abcdef" for character in ref) for ref in action_refs)


def test_source_only_version_fallback_is_distinguishable(monkeypatch) -> None:
    root = Path(__file__).parents[2]

    def package_not_installed(_distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", package_not_installed)
    spec = importlib.util.spec_from_file_location("_slurmdeck_source_only", root / "src/slurmdeck/__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.__version__ == "0+unknown"


def test_cli_version_starts_from_source_in_a_fresh_process() -> None:
    root = Path(__file__).parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")

    result = subprocess.run(
        [sys.executable, "-m", "slurmdeck", "--version"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("slurmdeck ")


def test_user_error_type_hints_resolve_in_a_fresh_process() -> None:
    root = Path(__file__).parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    code = (
        "from typing import get_type_hints; "
        "from slurmdeck.errors import UserError; "
        "from slurmdeck.structured_errors import StructuredError; "
        "assert get_type_hints(UserError.__init__)['message'] == str | StructuredError"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("import_order", "code"),
    [
        ("errors-first", "from slurmdeck.errors import UserError, StructuredError"),
        ("structured-errors-first", "from slurmdeck.structured_errors import StructuredError"),
        ("transport-errors-first", "from slurmdeck.transport.errors import TransportError"),
        ("models-first", "from slurmdeck.models import OperationEvent, RunManifest"),
        ("cli-first", "from slurmdeck.cli.main import app"),
    ],
)
def test_public_import_orders_work_in_fresh_processes(import_order: str, code: str) -> None:
    root = Path(__file__).parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{import_order}: {result.stderr}"
