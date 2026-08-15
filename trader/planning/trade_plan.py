"""Compatibility facade for pure domain trade-plan policy."""

from __future__ import annotations

from trader.domain.planning import trade_plan as _domain_trade_plan
from trader.domain.planning.trade_plan import (
    InvalidExitPlanError,
    MoveStopTo,
    PositionSide,
    ProfitProtection,
    STRUCTURAL_HARD_STOP_ANCHORS,
    STRUCTURAL_HARD_STOP_LEVELS,
    StructuralHardStopAnchor,
    TakeProfit,
    TradePlan,
    TrailingStop,
    TrailingStopTrailType,
    apply_exit_update,
    create_trade_plan,
    create_trade_plan_from_order,
    normalize_exit_plan,
    normalize_indicator_watch,
    resolve_exit_plan,
    swing_high,
    swing_low,
    vwap,
)
from trader.domain.trade_plan import TRAILING_STOP_TRAIL_TYPES as TRAILING_STOP_TRAIL_TYPES

__all__ = [
    "InvalidExitPlanError",
    "MoveStopTo",
    "PositionSide",
    "ProfitProtection",
    "STRUCTURAL_HARD_STOP_ANCHORS",
    "STRUCTURAL_HARD_STOP_LEVELS",
    "StructuralHardStopAnchor",
    "TRAILING_STOP_TRAIL_TYPES",
    "TakeProfit",
    "TradePlan",
    "TrailingStop",
    "TrailingStopTrailType",
    "apply_exit_update",
    "create_trade_plan",
    "create_trade_plan_from_order",
    "normalize_exit_plan",
    "normalize_indicator_watch",
    "resolve_exit_plan",
    "swing_high",
    "swing_low",
    "validate_exit_plan",
    "vwap",
]


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
