"""Compatibility facade for pure domain exit-plan specs."""

from __future__ import annotations

from trader.domain.planning import exit_plan_spec as _domain_exit_plan_spec
from trader.domain.planning.exit_plan_spec import *  # noqa: F401,F403

globals().update(
    {
        name: getattr(_domain_exit_plan_spec, name)
        for name in dir(_domain_exit_plan_spec)
        if not name.startswith("__")
    }
)

__all__ = [name for name in dir(_domain_exit_plan_spec) if not name.startswith("__")]
