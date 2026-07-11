"""Compatibility facade for pure domain trade-plan policy."""

from __future__ import annotations

from trader.domain.planning import trade_plan as _domain_trade_plan
from trader.domain.trade_plan import TRAILING_STOP_TRAIL_TYPES as TRAILING_STOP_TRAIL_TYPES

_exported_names = [
    name for name in dir(_domain_trade_plan)
    if not (name.startswith("__") and name.endswith("__"))
]
globals().update({name: getattr(_domain_trade_plan, name) for name in _exported_names})
__all__ = [name for name in _exported_names if not name.startswith("_")]


def validate_exit_plan(
    raw_exit_plan: dict | None,
    *,
    reference_volatility: float | None = None,
    allow_unresolved: bool = False,
) -> None:
    """Preserve the legacy monkeypatch hook for trailing-stop vocabulary tests."""
    _domain_trade_plan.TRAILING_STOP_TRAIL_TYPES = TRAILING_STOP_TRAIL_TYPES
    return _domain_trade_plan.validate_exit_plan(
        raw_exit_plan,
        reference_volatility=reference_volatility,
        allow_unresolved=allow_unresolved,
    )


del _exported_names
