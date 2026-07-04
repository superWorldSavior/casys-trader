"""Rotation package: universe hot-set orchestration and venue scheduling."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_CORE_EXPORTS = {
    "CoverageError",
    "UniverseWriteError",
    "apply_hysteresis",
    "apply_override",
    "compose_final",
    "emergency_exits",
    "main",
    "run",
    "sticky_symbols",
    "write_universe_atomic",
}

__all__ = sorted(_CORE_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _CORE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("trader.market.rotation.core"), name)
    globals()[name] = value
    return value
