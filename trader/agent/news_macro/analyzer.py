"""LLM-backed implementation of the macro/news analyst port."""

from __future__ import annotations

import os
from pathlib import Path

from trader.agent import llm
from trader.agent.news_macro.prompt import build_news_macro_prompt, parse_news_macro_completion
from trader.application.analyst import NewsMacroAnalysisRequest
from trader.domain.situation import NewsMacroBrief

DEFAULT_NEWS_MACRO_ANALYST_SESSION_LABEL = "casys-trader:macro-news-analyst"
DEFAULT_NEWS_MACRO_ANALYST_TIMEOUT_S = 90
DEFAULT_NEWS_MACRO_ANALYST_MODEL = llm.DEFAULT_SPARK_MODEL


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
        )
        if brief is None:
            raise NewsMacroAnalystError(
                "invalid_payload",
                error or "macro/news analyst returned invalid payload",
                provider=completion.provider,
                model=completion.model,
            )
        return brief
