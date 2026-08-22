"""Shadow-only, exogenous World-model application services.

Nothing exported here has decision authority.  The baseline is deliberately
kept independent from the Trader's prompt, scheduler, risk gates and broker.
"""

from trader.application.world_model.baseline import (
    ALLOWED_CATEGORICAL_FEATURES,
    ALLOWED_NUMERIC_FEATURES,
    OUTCOME_CLASSES,
    BaselinePrediction,
    FeatureBoundaryError,
    FutureLabelLeakageError,
    HierarchicalDirichletWorldBaseline,
    ModelUpdate,
    OutcomeEventConflictError,
    WorldBaseline,
)

__all__ = [
    "ALLOWED_CATEGORICAL_FEATURES",
    "ALLOWED_NUMERIC_FEATURES",
    "OUTCOME_CLASSES",
    "BaselinePrediction",
    "FeatureBoundaryError",
    "FutureLabelLeakageError",
    "HierarchicalDirichletWorldBaseline",
    "ModelUpdate",
    "OutcomeEventConflictError",
    "WorldBaseline",
]
