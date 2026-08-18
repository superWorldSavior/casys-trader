"""LLM-backed implementation of the company micro analyst port."""

from __future__ import annotations

import os
from pathlib import Path

from trader.agent import llm
from trader.agent.company_micro.prompt import build_company_micro_prompt, parse_company_micro_completion
from trader.application.analyst.company_micro import CompanyMicroAnalysisRequest
from trader.domain.company import CompanyIntelligenceBrief

DEFAULT_COMPANY_MICRO_SESSION_LABEL = "casys-trader:company-micro-analyst"
DEFAULT_COMPANY_MICRO_TIMEOUT_S = 240
DEFAULT_COMPANY_MICRO_MODEL = llm.DEFAULT_ANALYST_MODEL


def _default_timeout_s() -> int:
    """Timeout analyste micro (secondes), env ``TRADER_COMPANY_MICRO_TIMEOUT_S``.

    Aligné sur les knobs modèle/agent du même module : le paramètre explicite
    prime toujours, l'env ne remplace que le défaut. Utile pour les modèles
    lents type kimi-code/k3 qui dépassent le défaut de 240s sur les gros
    evidence packs.
    """
    raw = os.getenv("TRADER_COMPANY_MICRO_TIMEOUT_S")
    if raw is None:
        return DEFAULT_COMPANY_MICRO_TIMEOUT_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return DEFAULT_COMPANY_MICRO_TIMEOUT_S


class CompanyMicroAnalystError(RuntimeError):
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


def build_company_micro_router_from_env(
    *,
    env_path: str | Path | None = llm.DEFAULT_ENV_PATH,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
) -> llm.LlmRouter:
    resolved_bin = acpx_bin or os.getenv("TRADER_COMPANY_MICRO_ACPX_BIN") or "acpx"
    resolved_agent = acpx_agent or os.getenv("TRADER_COMPANY_MICRO_ACPX_AGENT")
    resolved_model = model or os.getenv("TRADER_COMPANY_MICRO_MODEL") or DEFAULT_COMPANY_MICRO_MODEL
    return llm.build_default_router_from_env(
        env_path=env_path,
        acpx_bin=resolved_bin,
        spark_model=resolved_model,
        spark_fallback_model=None,
        acpx_provider="company-micro",
        acpx_agent=resolved_agent,
        acpx_session_label=DEFAULT_COMPANY_MICRO_SESSION_LABEL,
    )


class LlmCompanyMicroAnalyst:
    def __init__(
        self,
        router: llm.LlmRouter | None = None,
        *,
        timeout_s: int | None = None,
    ) -> None:
        self._router = router or build_company_micro_router_from_env()
        self._timeout_s = int(timeout_s) if timeout_s is not None else _default_timeout_s()

    def analyze(self, request: CompanyMicroAnalysisRequest) -> CompanyIntelligenceBrief:
        completion = self._router.complete(
            build_company_micro_prompt(request),
            timeout_s=self._timeout_s,
        )
        if isinstance(completion, llm.LlmFailure):
            raise CompanyMicroAnalystError(
                completion.code,
                completion.message,
                provider=completion.provider,
                model=completion.model,
            )
        brief, error = parse_company_micro_completion(completion.text, request=request)
        if brief is None:
            raise CompanyMicroAnalystError(
                "invalid_payload",
                error or "company micro analyst returned invalid payload",
                provider=completion.provider,
                model=completion.model,
            )
        restricted = brief.restrict_sources(request.evidence.source_catalog())
        if request.evidence.source_catalog() and not restricted.source_refs:
            raise CompanyMicroAnalystError(
                "invalid_source_refs",
                "company micro analyst returned no valid top-level source_refs",
                provider=completion.provider,
                model=completion.model,
            )
        return restricted


__all__ = [
    "CompanyMicroAnalystError",
    "DEFAULT_COMPANY_MICRO_MODEL",
    "DEFAULT_COMPANY_MICRO_SESSION_LABEL",
    "DEFAULT_COMPANY_MICRO_TIMEOUT_S",
    "LlmCompanyMicroAnalyst",
    "build_company_micro_router_from_env",
]
