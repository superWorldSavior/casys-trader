"""Inbound learnings: consolidate the global roster; project the trader prompt.

Prompt slice: ``global`` plus optional ``guardrails``.
Never injected: raw notes, ``by_symbol``.
"""

from trader.application.learnings.consolidate import (
    DEFAULT_CONSOLIDATION_THRESHOLD,
    DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    consolidate_payload,
    finalize_consolidated_payload,
    maybe_consolidate,
)
from trader.application.learnings.context import build_context_learnings, project_global_rules
from trader.application.learnings.protocols import (
    ConsolidatedLearningsPort,
    ConsolidationStatusPort,
    CurationPort,
    LearningComposer,
    RawLearningsPort,
)

__all__ = [
    "DEFAULT_CONSOLIDATION_THRESHOLD",
    "DEFAULT_CONSOLIDATOR_TIMEOUT_S",
    "ConsolidatedLearningsPort",
    "ConsolidationStatusPort",
    "CurationPort",
    "LearningComposer",
    "RawLearningsPort",
    "build_context_learnings",
    "consolidate_payload",
    "finalize_consolidated_payload",
    "maybe_consolidate",
    "project_global_rules",
]
