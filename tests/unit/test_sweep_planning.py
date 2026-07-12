from __future__ import annotations

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.sweep import Sweep
from slurmdeck.planning.sweep import expand_sweep, render_args_from_config


def _matrix(**extra):
    base = {"version": 1, "parameters": {"lr": [0.1, 0.01], "model": ["s", "l"]}}
    base.update(extra)
    return Sweep.model_validate(base)


class TestMatrixExpansion:
    def test_cartesian_product(self):
        drafts = expand_sweep(_matrix())
        assert len(drafts) == 4
        assert drafts[0].params == {"lr": 0.1, "model": "s"}
        assert drafts[0].name == "lr-0.1__model-s"

    def test_exclude_drops_matching_combos(self):
        drafts = expand_sweep(_matrix(exclude=[{"model": "l"}]))
        assert len(drafts) == 2
        assert all(draft.params["model"] == "s" for draft in drafts)

    def test_include_adds_combos(self):
        drafts = expand_sweep(_matrix(include=[{"lr": 1.0, "model": "xl"}]))
        assert len(drafts) == 5
        assert drafts[-1].params == {"lr": 1.0, "model": "xl"}

    def test_all_excluded_is_an_error(self):
        with pytest.raises(UserError, match="no tasks"):
            expand_sweep(_matrix(exclude=[{"model": "s"}, {"model": "l"}]))


class TestExplicitTasks:
    def test_tasks_form(self):
        sweep = Sweep.model_validate(
            {
                "version": 1,
                "tasks": [
                    {"name": "baseline", "config": {"lr": 0.1}, "args": ["--lr", "{lr}"], "env": {"S": "1"}},
                    {"config": {"lr": 0.2}},
                ],
            }
        )
        drafts = expand_sweep(sweep)
        assert drafts[0].name == "baseline"
        assert drafts[0].args_template == ["--lr", "{lr}"]
        assert drafts[1].name == "task-1"


class TestArgRendering:
    def test_posix(self):
        args = render_args_from_config(
            {"training": {"lr": 0.001, "amp": True, "debug": False, "skip": None, "layers": [4, 4]}}, "posix"
        )
        assert args == [
            "--training-lr",
            "0.001",
            "--training-amp",
            "--no-training-debug",
            "--training-layers",
            "4",
            "4",
        ]

    def test_hydra_renders_null_explicitly(self):
        # regression: null values used to be silently dropped
        args = render_args_from_config({"a": {"b": None, "flag": True, "xs": [1, None]}}, "hydra")
        assert args == ["a.b=null", "a.flag=true", "a.xs=[1,null]"]

    def test_none_style(self):
        assert render_args_from_config({"a": 1}, "none") == []

    def test_empty_mapping_rejected(self):
        with pytest.raises(UserError, match="empty mapping"):
            render_args_from_config({"a": {}}, "posix")
