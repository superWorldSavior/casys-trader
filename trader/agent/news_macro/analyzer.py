"""LLM-backed implementation of the macro/news analyst port."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from trader.agent import llm
from trader.agent.news_macro.prompt import (
    build_news_macro_prompt,
    build_news_macro_source_catalog,
    parse_news_macro_completion,
)
from trader.application.analyst import NewsMacroAnalysisRequest
from trader.domain.situation import NewsMacroBrief, SituationPoint, SituationSection

DEFAULT_NEWS_MACRO_ANALYST_SESSION_LABEL = "casys-trader:macro-news-analyst"
DEFAULT_NEWS_MACRO_ANALYST_TIMEOUT_S = 240
DEFAULT_NEWS_MACRO_ANALYST_MODEL = llm.DEFAULT_ANALYST_MODEL


class NewsMacroAnalystError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.model = model


def build_news_macro_router_from_env(
    *,
    env_path: str | Path | None = llm.DEFAULT_ENV_PATH,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
) -> llm.LlmRouter:
    """Build the bounded analyst router using the consolidator-style profile."""

    resolved_bin = acpx_bin or os.getenv("TRADER_NEWS_MACRO_ACPX_BIN") or os.getenv("TRADER_CONSOLIDATOR_ACPX_BIN") or "acpx"
    resolved_agent = acpx_agent or os.getenv("TRADER_NEWS_MACRO_ACPX_AGENT") or os.getenv("TRADER_CONSOLIDATOR_ACPX_AGENT")
    resolved_model = model or os.getenv("TRADER_NEWS_MACRO_MODEL") or DEFAULT_NEWS_MACRO_ANALYST_MODEL
    return llm.build_default_router_from_env(
        env_path=env_path,
        acpx_bin=resolved_bin,
        spark_model=resolved_model,
        spark_fallback_model=None,
        acpx_provider="consolidator",
        acpx_agent=resolved_agent,
        acpx_session_label=DEFAULT_NEWS_MACRO_ANALYST_SESSION_LABEL,
    )


class LlmNewsMacroAnalyst:
    def __init__(
        self,
        router: llm.LlmRouter | None = None,
        *,
        timeout_s: int = DEFAULT_NEWS_MACRO_ANALYST_TIMEOUT_S,
    ) -> None:
        self._router = router or build_news_macro_router_from_env()
        self._timeout_s = timeout_s

    def analyze(self, request: NewsMacroAnalysisRequest) -> NewsMacroBrief:
        prompt = build_news_macro_prompt(request)
        completion = self._router.complete(prompt, timeout_s=self._timeout_s)
        if isinstance(completion, llm.LlmFailure):
            raise NewsMacroAnalystError(
                completion.code,
                completion.message,
                provider=completion.provider,
                model=completion.model,
            )
        brief, error = parse_news_macro_completion(
            completion.text,
            as_of=request.as_of,
            valid_until=request.valid_until,
            venue=request.venue,
            input_refs=request.input_refs,
        )
        if brief is None:
            raise NewsMacroAnalystError(
                "invalid_payload",
                error or "macro/news analyst returned invalid payload",
                provider=completion.provider,
                model=completion.model,
            )
        sourced_brief = _with_readable_sources(brief, request)
        if not _has_sourced_points(sourced_brief):
            raise NewsMacroAnalystError(
                "empty_after_source_validation",
                "macro/news analyst returned no point backed by the source catalog",
                provider=completion.provider,
                model=completion.model,
            )
        return sourced_brief


def _has_sourced_points(brief: NewsMacroBrief) -> bool:
    return bool(
        brief.alerts
        or any(section.points for section in brief.zones)
        or any(section.points for section in brief.families)
        or any(section.points for section in brief.symbols)
    )


def _with_readable_sources(
    brief: NewsMacroBrief,
    request: NewsMacroAnalysisRequest,
) -> NewsMacroBrief:
    catalog = build_news_macro_source_catalog(request)
    retained_points = 0
    dropped_points = 0

    def enrich_point(point: SituationPoint) -> SituationPoint | None:
        nonlocal retained_points, dropped_points
        names: list[str] = []
        refs: list[str] = []
        for ref in point.source_refs:
            label = _catalog_label(catalog, ref)
            if not label or _looks_like_uuid(label):
                continue
            if ref not in refs:
                refs.append(ref)
            if label not in names:
                names.append(label)
        if not refs:
            dropped_points += 1
            return None
        retained_points += 1
        # Names are derived exclusively from the authoritative input catalog;
        # model-supplied labels never become evidence by themselves.
        return replace(point, sources=tuple(names), source_refs=tuple(refs))

    def enrich_section(section: SituationSection) -> SituationSection:
        points = tuple(
            enriched
            for point in section.points
            if (enriched := enrich_point(point)) is not None
        )
        return replace(section, points=points)

    zones = tuple(
        section
        for raw_section in brief.zones
        if (section := enrich_section(raw_section)).points
    )
    families = tuple(
        section
        for raw_section in brief.families
        if (section := enrich_section(raw_section)).points
    )
    symbols = tuple(
        section
        for raw_section in brief.symbols
        if (section := enrich_section(raw_section)).points
    )
    alerts = tuple(
        enriched
        for point in brief.alerts
        if (enriched := enrich_point(point)) is not None
    )
    input_refs = dict(brief.input_refs or {})
    input_refs["source_validation"] = {
        "retained_points": retained_points,
        "dropped_unsourced_points": dropped_points,
    }

    return replace(
        brief,
        input_refs=input_refs,
        zones=zones,
        families=families,
        symbols=symbols,
        alerts=alerts,
    )


def _catalog_label(catalog: dict[str, str], ref: str) -> str | None:
    label = catalog.get(ref)
    if label:
        return label
    macro_label = catalog.get(f"macro_series:{ref}")
    if macro_label:
        return macro_label
    if ref.startswith("macro_next:"):
        event = ref.split(":", 2)[1]
        for key, candidate in catalog.items():
            if key.startswith(f"macro_next:{event}:"):
                return candidate
    return None


def _looks_like_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
