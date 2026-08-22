"""Compatibility shim. Canonical owner: :mod:`trader.reporting.read_models.world_impact`."""

from trader.reporting.read_models.world_impact import (
    CLASSES,
    DEFAULT_MINIMUM_MARKET_SAMPLES,
    DEFAULT_MINIMUM_TRADER_CYCLES,
    UNIFORM_BRIER,
    evaluate_world_shadow_impact,
)

__all__ = [
    "CLASSES",
    "DEFAULT_MINIMUM_MARKET_SAMPLES",
    "DEFAULT_MINIMUM_TRADER_CYCLES",
    "UNIFORM_BRIER",
    "evaluate_world_shadow_impact",
]
