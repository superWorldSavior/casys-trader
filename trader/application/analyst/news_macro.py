"""Application use case for the macro/news analyst."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from trader.domain.situation import NewsMacroBrief


@dataclass(frozen=True)
class NewsMacroAnalysisRequest:
    as_of: str
    valid_until: str
    venue: str = "GLOBAL"
    news_items: tuple[dict, ...] = ()
    global_news_items: tuple[dict, ...] = ()
    macro_next: tuple[dict, ...] = ()
    macro_series: tuple[dict, ...] = ()
    # GDELT geopolitical events — alimentés seulement pour la passe GLOBAL.
    geopolitical_events: tuple[dict, ...] = ()
    # Upstream shortlist (radar top 40 + qualified challengers), never the hotlist.
    candidate_symbols: tuple[str, ...] = ()
    family_context: dict[str, dict] | None = None
    # Compact durable company anchors for symbols touched by current news.
    company_anchors: dict[str, dict] | None = None
    input_refs: dict | None = None
    # Market-as-judge digest of past notes. Omitted from the prompt when empty.
    situation_feedback: dict | None = None


@dataclass(frozen=True)
class NewsMacroAnalysisResult:
    triggered: bool
    written: bool
    brief_ref: dict[str, str] | None = None
    error_code: str | None = None
    error_message: str | None = None


@runtime_checkable
class NewsMacroAnalyst(Protocol):
    def analyze(self, request: NewsMacroAnalysisRequest) -> NewsMacroBrief:
        ...


@runtime_checkable
class NewsMacroBriefRepository(Protocol):
    def append(self, brief: NewsMacroBrief, *, date: str | None = None) -> dict[str, str]:
        ...


@runtime_checkable
class SituationMemoryRepository(Protocol):
    def ingest_brief(self, brief: NewsMacroBrief) -> dict:
        ...


def run_news_macro_analysis(
    request: NewsMacroAnalysisRequest,
    *,
    analyst: NewsMacroAnalyst,
    repository: NewsMacroBriefRepository,
    situation_repository: SituationMemoryRepository | None = None,
    date: str | None = None,
) -> NewsMacroAnalysisResult:
    """Run the analyst best-effort and persist the resulting brief."""

    try:
        brief = analyst.analyze(request)
        brief_ref = repository.append(brief, date=date)
    except Exception as exc:
        return NewsMacroAnalysisResult(
            triggered=True,
            written=False,
            error_code=exc.__class__.__name__,
            error_message=str(exc)[:500],
        )
    if situation_repository is not None:
        try:
            situation_repository.ingest_brief(brief)
        except Exception:  # noqa: BLE001 - derived index, JSONL brief remains canonical
            pass
    return NewsMacroAnalysisResult(
        triggered=True,
        written=True,
        brief_ref=brief_ref,
    )
