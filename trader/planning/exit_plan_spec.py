"""Compatibility facade for pure domain exit-plan specs."""

from __future__ import annotations

from trader.domain.planning.exit_plan_spec import (
    InvalidExitPlanError,
    STRUCTURAL_HARD_STOP_ANCHORS,
    StructuralHardStopAnchor,
    TRAILING_STOP_TRAIL_TYPES,
    _bounded_float,
    _bounded_fraction,
    _non_negative_float,
    _positive_float,
    _positive_int,
    _validate_optional_pct_bounds,
    _validate_structural_hard_stop,
    normalize_exit_plan,
    validate_exit_plan,
)

__all__ = [
    "InvalidExitPlanError",
    "STRUCTURAL_HARD_STOP_ANCHORS",
    "StructuralHardStopAnchor",
    "TRAILING_STOP_TRAIL_TYPES",
    "_bounded_float",
    "_bounded_fraction",
    "_non_negative_float",
    "_positive_float",
    "_positive_int",
    "_validate_optional_pct_bounds",
    "_validate_structural_hard_stop",
    "normalize_exit_plan",
    "validate_exit_plan",
]
