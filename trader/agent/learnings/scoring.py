"""Compatibility façade for domain FLAIR/MemRL scoring."""

from trader.domain.learnings.scoring import (
    CITATION_UTILITY_HELPS,
    CITATION_UTILITY_HURTS,
    CITATION_UTILITY_NEUTRAL,
    CITATION_UTILITY_UNKNOWN,
    MEMRL_MIN_UPDATES,
    SIGNIFICANT_RETURN_BAND,
    apply_shrinkage,
    citation_utility,
    classify_decision_quality,
    compute_lift,
    compute_outcome_scores,
    is_known_harmful_utility,
)

__all__ = [
    "CITATION_UTILITY_HELPS",
    "CITATION_UTILITY_HURTS",
    "CITATION_UTILITY_NEUTRAL",
    "CITATION_UTILITY_UNKNOWN",
    "MEMRL_MIN_UPDATES",
    "SIGNIFICANT_RETURN_BAND",
    "apply_shrinkage",
    "citation_utility",
    "classify_decision_quality",
    "compute_lift",
    "compute_outcome_scores",
    "is_known_harmful_utility",
]
