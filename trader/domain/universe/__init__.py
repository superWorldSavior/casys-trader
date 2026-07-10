"""Pure contracts for candidate-scope and universe intelligence."""

from trader.domain.universe.intelligence import (
    DEFAULT_MAX_SITUATION_POINTS,
    DEFAULT_MAX_SITUATION_TEXT_CHARS,
    UNCLASSIFIED_FAMILY,
    UniverseSituationContext,
    build_family_snapshot,
    candidate_scope_id,
    enrich_candidates_with_family,
    project_brief_to_universe_context,
)
from trader.domain.universe.global_family_board import build_global_family_board
from trader.domain.universe.news_challengers import (
    NewsChallenger,
    NewsChallengerEvidence,
    NewsChallengerSelection,
    select_news_challengers,
)

__all__ = [
    "DEFAULT_MAX_SITUATION_POINTS",
    "DEFAULT_MAX_SITUATION_TEXT_CHARS",
    "NewsChallenger",
    "NewsChallengerEvidence",
    "NewsChallengerSelection",
    "UNCLASSIFIED_FAMILY",
    "UniverseSituationContext",
    "build_family_snapshot",
    "build_global_family_board",
    "candidate_scope_id",
    "enrich_candidates_with_family",
    "project_brief_to_universe_context",
    "select_news_challengers",
]
