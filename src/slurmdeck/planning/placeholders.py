"""Placeholder expansion: one grammar, one context, resolved at submit time.

Grammar: ``{name}`` where ``name`` is an identifier. Literal braces are
written ``{{`` and ``}}`` (so a shell variable is ``${{SCRATCH}}``). Any other
unescaped ``{`` is an error — typos never pass silently, and the error lists
the placeholders that are valid at that position.
"""

from __future__ import annotations

import re
from typing import Any

from slurmdeck.errors import UserError

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def to_text(value: Any) -> str:
    """Render a scalar for interpolation into strings/args/env."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _unknown(name: str, position: str, context: dict[str, Any]) -> UserError:
    known = ", ".join(sorted(context)) or "none"
    return UserError(
        f"Unknown placeholder {{{name}}} in {position}.",
        hint=f"Valid placeholders here: {known}. For a literal brace write '{{{{' or '}}}}'.",
    )


def expand_text(
    text: str,
    context: dict[str, Any],
    *,
    position: str,
    transform: Any = to_text,
) -> str:
    """Expand all placeholders in ``text``; ``transform`` maps values to strings."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            if text.startswith("{{", i):
                out.append("{")
                i += 2
                continue
            match = _IDENT.match(text, i + 1)
            if match and text.startswith("}", match.end()):
                name = match.group(0)
                if name not in context:
                    raise _unknown(name, position, context)
                out.append(transform(context[name]))
                i = match.end() + 1
                continue
            raise UserError(
                f"Stray '{{' in {position}: {text!r}.",
                hint="Placeholders look like {name}; write '{{' for a literal brace.",
            )
        if ch == "}":
            if text.startswith("}}", i):
                out.append("}")
                i += 2
                continue
            raise UserError(
                f"Stray '}}' in {position}: {text!r}.",
                hint="Write '}}' for a literal closing brace.",
            )
        out.append(ch)
        i += 1
    return "".join(out)


def expand_value(value: Any, context: dict[str, Any], *, position: str) -> Any:
    """Expand a config-tree value: an exact ``{name}`` string keeps the value's type."""
    if isinstance(value, str):
        match = _IDENT.match(value, 1)
        if value.startswith("{") and match and value == "{" + match.group(0) + "}":
            name = match.group(0)
            if name not in context:
                raise _unknown(name, position, context)
            return context[name]
        return expand_text(value, context, position=position)
    if isinstance(value, dict):
        return {str(key): expand_value(item, context, position=f"{position}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [expand_value(item, context, position=f"{position}[{idx}]") for idx, item in enumerate(value)]
    return value


def has_placeholder(text: str, name: str) -> bool:
    """True if ``{name}`` occurs unescaped in ``text``."""
    i = 0
    token = "{" + name + "}"
    while i < len(text):
        if text.startswith("{{", i) or text.startswith("}}", i):
            i += 2
            continue
        if text.startswith(token, i):
            return True
        i += 1
    return False
