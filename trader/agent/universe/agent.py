"""LLM-backed adapter for the universe-composition application port."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from trader.agent import llm
from trader.agent.universe.prompt import (
    build_universe_prompt,
    build_universe_repair_prompt,
    parse_universe_completion,
)
from trader.application.universe import UniverseAgentDecision, UniverseCompositionRequest

DEFAULT_UNIVERSE_AGENT_TIMEOUT_S = 120
DEFAULT_UNIVERSE_AGENT_MODEL = llm.DEFAULT_ANALYST_MODEL
DEFAULT_UNIVERSE_AGENT_SESSION_LABEL = "casys-trader:universe-agent"


def build_universe_router_from_env(
    *,
    env_path: str | Path | None = llm.DEFAULT_ENV_PATH,
    acpx_bin: str | None = None,
    acpx_agent: str | None = None,
    model: str | None = None,
    session_label: str | None = None,
) -> llm.LlmRouter:
    """Build an isolated universe-agent profile on the shared LLM transport."""

    resolved_bin = acpx_bin or os.getenv("TRADER_UNIVERSE_ACPX_BIN") or "acpx"
    resolved_agent = acpx_agent or os.getenv("TRADER_UNIVERSE_ACPX_AGENT")
    resolved_model = model or os.getenv("TRADER_UNIVERSE_MODEL") or DEFAULT_UNIVERSE_AGENT_MODEL
    resolved_label = (
        session_label
        or os.getenv("TRADER_UNIVERSE_ACPX_SESSION_LABEL")
        or DEFAULT_UNIVERSE_AGENT_SESSION_LABEL
    )
    return llm.build_default_router_from_env(
        env_path=env_path,
        acpx_bin=resolved_bin,
        spark_model=resolved_model,
        acpx_provider="universe",
        acpx_agent=resolved_agent,
        acpx_session_label=resolved_label,
    )


class UniverseAgentError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        provider_fallback_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider
        self.model = model
        self.provider_fallback_reason = provider_fallback_reason


class UniverseAgentPayloadError(ValueError):
    """Invalid completion with transport metadata preserved for observability."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None,
        model: str | None,
        provider_fallback_reason: str | None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.provider_fallback_reason = provider_fallback_reason


class LlmUniverseAgent:
    """Compose a complete hotlist from one bounded per-venue request."""

    def __init__(
        self,
        router: llm.LlmRouter | None = None,
        *,
        timeout_s: int = DEFAULT_UNIVERSE_AGENT_TIMEOUT_S,
    ) -> None:
        self._router = router or build_universe_router_from_env()
        self._timeout_s = int(timeout_s)

    def compose(self, request: UniverseCompositionRequest) -> UniverseAgentDecision:
        prompt = build_universe_prompt(request)
        completion = self._router.complete(prompt, timeout_s=self._timeout_s)
        if isinstance(completion, llm.LlmFailure):
            raise UniverseAgentError(
                completion.code,
                completion.message,
                provider=completion.provider,
                model=completion.model,
                provider_fallback_reason=completion.fallback_reason,
            )
        decision, error = parse_universe_completion(completion.text, baseline=request.baseline)
        if decision is None:
            repair = build_universe_repair_prompt(
                request,
                invalid_response=completion.text,
                parse_error=error or "invalid_agent_response",
                company_context_index=False,
            )
            completion = self._router.complete(repair, timeout_s=self._timeout_s)
            if isinstance(completion, llm.LlmFailure):
                raise UniverseAgentError(
                    completion.code,
                    completion.message,
                    provider=completion.provider,
                    model=completion.model,
                    provider_fallback_reason=completion.fallback_reason,
                )
            decision, error = parse_universe_completion(completion.text, baseline=request.baseline)
            if decision is None:
                raise UniverseAgentPayloadError(
                    error or "invalid_agent_response",
                    provider=completion.provider,
                    model=completion.model,
                    provider_fallback_reason=completion.fallback_reason,
                )
        if decision.contract_version not in {"universe.v1", "universe.v2"}:
            raise UniverseAgentPayloadError(
                "legacy_contract_not_allowed_for_universe_agent",
                provider=completion.provider,
                model=completion.model,
                provider_fallback_reason=completion.fallback_reason,
            )
        return replace(
            decision,
            provider=completion.provider,
            model=completion.model,
            provider_fallback_reason=completion.fallback_reason,
        )
