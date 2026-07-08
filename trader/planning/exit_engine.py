"""Compatibility facade for pure domain exit-plan evaluation."""

from __future__ import annotations

from trader.domain.planning import exit_engine as _domain_exit_engine
from trader.domain.planning.exit_engine import *  # noqa: F401,F403

globals().update(
    {
        name: getattr(_domain_exit_engine, name)
        for name in dir(_domain_exit_engine)
        if not name.startswith("__")
    }
)

__all__ = [name for name in dir(_domain_exit_engine) if not name.startswith("__")]
