"""Compatibility facade for pure domain armed-order policy."""

from __future__ import annotations

from trader.domain.planning.armed_order import (
    ARMED_ORDER_MAX_TTL_MINUTES,
    InvalidExitPlanError,
    armed_order_price_coherent,
    normalize_armed_order,
    normalize_exit_plan,
    validate_exit_plan,
)

__all__ = [
    "ARMED_ORDER_MAX_TTL_MINUTES",
    "InvalidExitPlanError",
    "armed_order_price_coherent",
    "normalize_armed_order",
    "normalize_exit_plan",
    "validate_exit_plan",
]
