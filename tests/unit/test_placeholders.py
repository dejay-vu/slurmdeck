from __future__ import annotations

import shlex

import pytest

from slurmdeck.errors import UserError
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.planning.commandline import command_mentions_args, resolve_command, shell_join
from slurmdeck.planning.placeholders import expand_text, expand_value, has_placeholder

CTX = {"config": "/remote/configs/000.yaml", "output": "/remote/results/000", "seed": 7, "flag": True}


class TestExpandText:
    def test_basic_and_embedded(self):
        assert expand_text("--config={config}", CTX, position="t") == "--config=/remote/configs/000.yaml"
        assert expand_text("run-{seed}", CTX, position="t") == "run-7"

    def test_bool_and_none_rendering(self):
        assert expand_text("{flag}", CTX, position="t") == "true"
        assert expand_text("{x}", {"x": None}, position="t") == ""

    def test_escapes(self):
        assert expand_text("${{SCRATCH}}/out", CTX, position="t") == "${SCRATCH}/out"
        assert expand_text("{{literal}}", CTX, position="t") == "{literal}"

    def test_unknown_placeholder_lists_valid_names(self):
        with pytest.raises(UserError) as excinfo:
            expand_text("{sead}", CTX, position="args[0]")
        assert "sead" in str(excinfo.value)
        assert "seed" in str(excinfo.value)  # valid names listed

    def test_stray_brace_is_an_error_with_escape_hint(self):
        with pytest.raises(UserError, match="literal brace"):
            expand_text("${SCRATCH}/out", {}, position="t")
        with pytest.raises(UserError, match="Stray"):
            expand_text("a } b", {}, position="t")


class TestExpandValue:
    def test_exact_placeholder_preserves_type(self):
        assert expand_value("{seed}", CTX, position="c") == 7
        assert expand_value({"nested": {"s": "{seed}", "label": "s{seed}"}}, CTX, position="c") == {
            "nested": {"s": 7, "label": "s7"}
        }

    def test_non_strings_pass_through(self):
        assert expand_value([1, "{seed}", None], CTX, position="c") == [1, 7, None]


class TestArgvMode:
    def test_args_token_splices(self):
        argv, shell = resolve_command(
            CommandTemplateSpec(argv=["python", "t.py", "{args}", "--config", "{config}"]),
            CTX,
            args=["--lr", "0.1"],
        )
        assert shell is None
        assert argv == ["python", "t.py", "--lr", "0.1", "--config", "/remote/configs/000.yaml"]

    def test_embedded_args_rejected_with_shell_hint(self):
        with pytest.raises(UserError, match="standalone token"):
            resolve_command(CommandTemplateSpec(argv=["x", "--extra={args}"]), CTX, args=["a"])


class TestShellMode:
    def test_unquoted_placeholder_is_quoted(self):
        _, shell = resolve_command(
            CommandTemplateSpec(shell="python t.py --config {config}"), {"config": "/pa th/0.yaml"}
        )
        assert shlex.split(shell) == ["python", "t.py", "--config", "/pa th/0.yaml"]

    def test_placeholder_inside_double_quotes(self):
        # regression: naive shlex.quote substitution produced nested quotes
        _, shell = resolve_command(
            CommandTemplateSpec(shell='python t.py --config "{config}"'), {"config": "/pa th/0.yaml"}
        )
        assert shlex.split(shell) == ["python", "t.py", "--config", "/pa th/0.yaml"]

    def test_args_are_individually_quoted(self):
        _, shell = resolve_command(CommandTemplateSpec(shell="run.sh {args}"), {}, args=["a b", "c"])
        assert shlex.split(shell) == ["run.sh", "a b", "c"]

    def test_operators_survive(self):
        _, shell = resolve_command(CommandTemplateSpec(shell="prep.py && train.py --out {output}"), {"output": "/o"})
        assert "&&" in shell

    def test_unbalanced_quote_rejected(self):
        with pytest.raises(UserError, match="Unbalanced"):
            resolve_command(CommandTemplateSpec(shell="echo 'oops"), {})

    def test_unknown_placeholder_rejected(self):
        with pytest.raises(UserError, match="Unknown placeholder"):
            resolve_command(CommandTemplateSpec(shell="echo {nope}"), {})


def test_command_mentions_args():
    assert command_mentions_args(CommandTemplateSpec(argv=["x", "{args}"]))
    assert not command_mentions_args(CommandTemplateSpec(argv=["x", "args"]))
    assert command_mentions_args(CommandTemplateSpec(shell="x {args}"))
    assert not command_mentions_args(CommandTemplateSpec(shell="x {{args}}"))


def test_shell_join_quotes():
    assert shell_join(["a b", "$HOME"]) == "'a b' '$HOME'"


def test_has_placeholder_respects_escapes():
    assert has_placeholder("x {args} y", "args")
    assert not has_placeholder("x {{args}} y", "args")
