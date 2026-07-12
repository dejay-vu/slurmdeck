"""Resolving the user command per task (argv and shell modes, one quoting impl)."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from typing import Any

from slurmdeck.errors import UserError
from slurmdeck.models.run import CommandTemplateSpec
from slurmdeck.planning.placeholders import expand_text, has_placeholder, to_text

ARGS_TOKEN = "{args}"

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def shell_join(args: Sequence[object]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def command_mentions_args(command: CommandTemplateSpec) -> bool:
    if command.argv is not None:
        return any(token == ARGS_TOKEN for token in command.argv)
    assert command.shell is not None
    return has_placeholder(command.shell, "args")


def _quote_for_state(value: str, state: str) -> str:
    """Quote a substituted value according to the shell-quote context at that point."""
    if state == "'":
        return value.replace("'", "'\\''")
    if state == '"':
        return re.sub(r'([$`"\\])', r"\\\1", value)
    return shlex.quote(value)


def _expand_shell(template: str, context: dict[str, Any], args: Sequence[str], *, position: str) -> str:
    """Expand placeholders in a shell command string, tracking quote state.

    A placeholder outside quotes becomes a fully quoted word; inside single or
    double quotes it is escaped for that quoting style — so both
    ``--config {config}`` and ``--config "{config}"`` produce a single correct
    argument even when the value contains spaces.
    """
    out: list[str] = []
    state = ""  # '', "'", or '"'
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if state == "'":
            if ch == "'":
                state = ""
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and state in ("", '"') and i + 1 < n:
            out.append(template[i : i + 2])
            i += 2
            continue
        if ch == "'" and state == "":
            state = "'"
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            state = '"' if state == "" else ""
            out.append(ch)
            i += 1
            continue
        if ch == "{":
            if template.startswith("{{", i):
                out.append("{")
                i += 2
                continue
            match = _IDENT.match(template, i + 1)
            if match and template.startswith("}", match.end()):
                name = match.group(0)
                if name == "args":
                    if state == "":
                        out.append(shell_join(args))
                    else:
                        out.append(_quote_for_state(" ".join(str(arg) for arg in args), state))
                elif name in context:
                    out.append(_quote_for_state(to_text(context[name]), state))
                else:
                    known = ", ".join(sorted([*context, "args"])) or "none"
                    raise UserError(
                        f"Unknown placeholder {{{name}}} in {position}.",
                        hint=f"Valid placeholders here: {known}. For a literal brace write '{{{{' or '}}}}'.",
                    )
                i = match.end() + 1
                continue
            raise UserError(
                f"Stray '{{' in {position}: {template!r}.",
                hint="Placeholders look like {name}; write '{{' for a literal brace.",
            )
        if ch == "}":
            if template.startswith("}}", i):
                out.append("}")
                i += 2
                continue
            raise UserError(
                f"Stray '}}' in {position}: {template!r}.",
                hint="Write '}}' for a literal closing brace.",
            )
        out.append(ch)
        i += 1
    if state:
        raise UserError(f"Unbalanced quote ({state}) in {position}: {template!r}.")
    return "".join(out)


def resolve_command(
    command: CommandTemplateSpec,
    context: dict[str, Any],
    *,
    args: Sequence[str] = (),
) -> tuple[list[str] | None, str | None]:
    """Resolve one task's command. Returns ``(argv, None)`` or ``(None, shell)``.

    In argv mode ``{args}`` must be a standalone token and splices the task
    args as separate argv entries; other placeholders may be embedded inside
    tokens. In shell mode substitution is quote-state aware (see
    ``_expand_shell``).
    """
    if command.argv is not None:
        argv: list[str] = []
        for position_index, token in enumerate(command.argv):
            if token == ARGS_TOKEN:
                argv.extend(str(arg) for arg in args)
                continue
            if has_placeholder(token, "args"):
                raise UserError(
                    f"{{args}} must be a standalone token in argv mode (argv[{position_index}]={token!r}).",
                    hint="Use `--shell '...'` if you need {args} inside a larger string.",
                )
            argv.append(expand_text(token, context, position=f"command argv[{position_index}]"))
        return argv, None

    assert command.shell is not None
    return None, _expand_shell(command.shell, context, args, position="command")
