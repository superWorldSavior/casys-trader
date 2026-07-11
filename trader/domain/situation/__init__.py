"""Pure situation contracts used by macro/news analysis."""

from trader.domain.situation.brief import NewsMacroBrief, SituationPoint, SituationSection
from trader.domain.situation.global_digest import (
    DEFAULT_MAX_DIGEST_POINTS,
    GlobalSituationDigest,
    build_global_situation_digest,
)

__all__ = [
    "NewsMacroBrief",
    "SituationPoint",
    "SituationSection",
    "DEFAULT_MAX_DIGEST_POINTS",
    "GlobalSituationDigest",
    "build_global_situation_digest",
]
