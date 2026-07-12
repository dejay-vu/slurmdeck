"""Context wiring for CLI commands (overridable in tests)."""

from __future__ import annotations

from collections.abc import Callable

from slurmdeck.services.context import AppContext

_factory: Callable[[], AppContext] | None = None
_cached: AppContext | None = None


def set_context_factory(factory: Callable[[], AppContext] | None) -> None:
    global _factory, _cached
    _factory = factory
    _cached = None


def get_context() -> AppContext:
    global _cached
    if _cached is None:
        _cached = _factory() if _factory is not None else AppContext.create()
    return _cached


def reset() -> None:
    global _cached
    _cached = None
