"""Compatibility facade for pure domain watch evaluation."""

from __future__ import annotations

from trader.domain.planning.watch_evaluator import (
    build_indicator_snapshot,
    compute_indicator_values,
    evaluate_indicator_watches,
)

__all__ = [
    "build_indicator_snapshot",
    "compute_indicator_values",
    "evaluate_indicator_watches",
]
