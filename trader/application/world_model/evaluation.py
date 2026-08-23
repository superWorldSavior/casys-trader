"""Compatibility shim. Canonical owner: :mod:`trader.reporting.read_models.world_evaluation`."""

from trader.reporting.read_models.world_evaluation import (
    BASELINE_MODEL_ID,
    CLASSES,
    CONTEXT_MODEL_VERSION,
    DEFAULT_MINIMUM_PAIRED_SUPPORT,
    DIRECTIONAL_SHADOW_DRAWDOWN_CAVEAT,
    GRU_MODEL_ID,
    MARKET_MODEL_VERSION,
    UNIFORM_BRIER,
    evaluate_context_ablation,
    evaluate_shadow,
)

__all__ = [
    "BASELINE_MODEL_ID",
    "CLASSES",
    "CONTEXT_MODEL_VERSION",
    "DEFAULT_MINIMUM_PAIRED_SUPPORT",
    "DIRECTIONAL_SHADOW_DRAWDOWN_CAVEAT",
    "GRU_MODEL_ID",
    "MARKET_MODEL_VERSION",
    "UNIFORM_BRIER",
    "evaluate_context_ablation",
    "evaluate_shadow",
]
