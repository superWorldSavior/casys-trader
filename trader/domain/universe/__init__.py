"""Pure contracts for candidate-scope and universe intelligence."""

from trader.domain.universe.candidate_scope import (
    RADAR_CANDIDATES_TOP,
    candidate_run_ids,
    compose_candidate_pool,
    merge_news_challengers,
    retained_news_challengers,
)
from trader.domain.universe.intelligence import (
    CompanyContextProjectionLimits,
    DEFAULT_MAX_SITUATION_POINTS,
    DEFAULT_MAX_SITUATION_TEXT_CHARS,
    UNCLASSIFIED_FAMILY,
    UniverseCompanyContext,
    UniverseSituationContext,
    build_family_snapshot,
    candidate_scope_id,
    enrich_candidates_with_family,
    project_brief_to_universe_context,
    project_company_briefs_to_universe_context,
)
from trader.domain.universe.global_family_board import build_global_family_board
from trader.domain.universe.mandate import SymbolMandate, UniverseMandate
from trader.domain.universe.news_challengers import (
    NewsChallenger,
    NewsChallengerEvidence,
    NewsChallengerSelection,
    select_news_challengers,
)
from trader.domain.universe.selection import (
    apply_hysteresis,
    apply_override,
    compose_active_universe,
    compose_final,
    emergency_exits,
    sticky_symbols,
)
from trader.domain.universe.user_overrides import (
    UserOverrides,
    apply_user_overrides,
)

__all__ = [
    "DEFAULT_MAX_SITUATION_POINTS",
    "DEFAULT_MAX_SITUATION_TEXT_CHARS",
    "CompanyContextProjectionLimits",
    "NewsChallenger",
    "NewsChallengerEvidence",
    "NewsChallengerSelection",
    "RADAR_CANDIDATES_TOP",
    "SymbolMandate",
    "UNCLASSIFIED_FAMILY",
    "UniverseCompanyContext",
    "UniverseSituationContext",
    "UniverseMandate",
    "UserOverrides",
    "apply_hysteresis",
    "apply_override",
    "apply_user_overrides",
    "build_family_snapshot",
    "build_global_family_board",
    "candidate_scope_id",
    "candidate_run_ids",
    "compose_active_universe",
    "compose_candidate_pool",
    "compose_final",
    "emergency_exits",
    "enrich_candidates_with_family",
    "project_brief_to_universe_context",
    "project_company_briefs_to_universe_context",
    "merge_news_challengers",
    "retained_news_challengers",
    "select_news_challengers",
    "sticky_symbols",
]
