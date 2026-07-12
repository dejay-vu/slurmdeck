"""Jinja rendering for sbatch scripts (cached environment, strict undefined)."""

from __future__ import annotations

import shlex
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined


@lru_cache(maxsize=1)
def environment() -> Environment:
    env = Environment(
        loader=PackageLoader("slurmdeck.rendering", "templates"),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["shell_quote"] = shlex.quote
    return env


def render_template(name: str, /, **context: object) -> str:
    return environment().get_template(name).render(**context)


def render_to_file(path: Path, name: str, /, **context: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_template(name, **context), encoding="utf-8")
    path.chmod(0o755)
