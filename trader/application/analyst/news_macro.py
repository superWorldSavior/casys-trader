"""Application use case for the macro/news analyst."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from trader.domain.situation import NewsMacroBrief


@dataclass(frozen=True)
class NewsMacroAnalysisRequest:
    as_of: str
    valid_until: str
    news_items: tuple[dict, ...] = ()
    macro_next: tuple[dict, ...] = ()
    candidate_symbols: tuple[str, ...] = ()


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
    def write(self, brief: NewsMacroBrief, *, date: str | None = None) -> None:
        ...


def run_news_macro_analysis(
    request: NewsMacroAnalysisRequest,
    *,
    analyst: NewsMacroAnalyst,
    repository: NewsMacroBriefRepository,
    date: str | None = None,
) -> NewsMacroAnalysisResult:
    """Run the analyst best-effort and persist the resulting brief."""

    try:
        brief = analyst.analyze(request)
        repository.write(brief, date=date)
    except Exception as exc:
        return NewsMacroAnalysisResult(
            triggered=True,
            written=False,
            error_code=exc.__class__.__name__,
            error_message=str(exc)[:500],
        )
    date_key = date or brief.as_of[:10]
    return NewsMacroAnalysisResult(
        triggered=True,
        written=True,
        brief_ref=brief.ref(date=date_key),
    )
