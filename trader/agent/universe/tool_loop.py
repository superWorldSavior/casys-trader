"""Bounded multi-turn tool loop for universe composition."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from trader.agent import llm
from trader.agent.tools.core import (
    ToolRoundLimits,
    execute_tool_round,
    results_prompt_payload,
    round_runtime_payload,
)
from trader.agent.universe.agent import UniverseAgentError, UniverseAgentPayloadError
from trader.agent.universe.prompt import (
    build_universe_followup_prompt,
    build_universe_prompt,
    build_universe_repair_prompt,
    parse_universe_completion,
)
from trader.agent.universe.tools import make_get_company_briefs_spec
from trader.application.universe import UniverseAgentDecision, UniverseCompositionRequest

_ALLOWED_TOOLS = frozenset({"get_company_briefs"})
# Fusible anti-boucle-infinie, PAS un réglage : l'agent fait autant de pulls micro
# qu'il veut jusqu'à ce plafond haut (jamais atteint en usage normal). Analogue au
# SESSION_ROUND_BACKSTOP du trader.
_TOOL_LOOP_BACKSTOP = 8


def _extract_tool_calls(text: str) -> list[dict[str, Any]] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        calls = payload.get("tool_calls")
        if isinstance(calls, list) and calls:
            return calls
    return None


def _raise_failure(completion: llm.LlmFailure) -> None:
    raise UniverseAgentError(
        completion.code,
        completion.message,
        provider=completion.provider,
        model=completion.model,
        provider_fallback_reason=completion.fallback_reason,
    )


def _parse_final(
    completion: llm.LlmCompletion,
    *,
    baseline: tuple[str, ...],
) -> UniverseAgentDecision:
    decision, error = parse_universe_completion(completion.text, baseline=baseline)
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


def _with_tool_trace(
    decision: UniverseAgentDecision,
    traces: list,
    *,
    rounds: int,
    results: list[dict[str, Any]],
) -> UniverseAgentDecision:
    payload = round_runtime_payload(traces, rounds=max(0, int(rounds)))
    return replace(
        decision,
        tool_rounds=int(payload["tool_rounds"]),
        tool_calls=tuple(payload["tool_calls"]),
        tool_results=tuple(dict(item) for item in results),
    )


def _parse_final_with_repair(
    completion: llm.LlmCompletion,
    *,
    request: UniverseCompositionRequest,
    router: llm.LlmRouter,
    timeout_s: int,
    tool_results: list[dict[str, Any]],
) -> UniverseAgentDecision:
    """Retry once only when the final payload is structurally invalid."""

    try:
        return _parse_final(completion, baseline=request.baseline)
    except UniverseAgentPayloadError as exc:
        if "legacy_contract_not_allowed" in str(exc):
            raise
        repair_prompt = build_universe_repair_prompt(
            request,
            invalid_response=completion.text,
            parse_error=str(exc),
            tool_results=tool_results,
            company_context_index=True,
        )
        repaired = router.complete(repair_prompt, timeout_s=timeout_s)
        if isinstance(repaired, llm.LlmFailure):
            _raise_failure(repaired)
        return _parse_final(repaired, baseline=request.baseline)


def compose_with_tool_loop(
    request: UniverseCompositionRequest,
    *,
    router: llm.LlmRouter,
    intelligence_store: Any,
    max_rounds: int = _TOOL_LOOP_BACKSTOP,
    timeout_s: int = 120,
) -> UniverseAgentDecision:
    registry = {"get_company_briefs": make_get_company_briefs_spec(intelligence_store)}
    context = SimpleNamespace(intelligence_store=intelligence_store)
    accumulated: list[dict[str, Any]] = []
    traces: list = []
    rounds = 0
    prompt = build_universe_prompt(request, allow_tools=True)

    for _ in range(max(0, int(max_rounds))):
        completion = router.complete(prompt, timeout_s=timeout_s)
        if isinstance(completion, llm.LlmFailure):
            _raise_failure(completion)
        tool_calls = _extract_tool_calls(completion.text)
        if tool_calls is None:
            return _with_tool_trace(
                _parse_final_with_repair(
                    completion,
                    request=request,
                    router=router,
                    timeout_s=timeout_s,
                    tool_results=accumulated,
                ),
                traces,
                rounds=rounds,
                results=accumulated,
            )
        results, round_traces = execute_tool_round(
            tool_calls,
            context=context,
            limits=ToolRoundLimits(max_total_calls=10, max_calls_per_symbol=3),
            allowed_tools=_ALLOWED_TOOLS,
            registry=registry,
        )
        rounds += 1
        traces.extend(round_traces)
        accumulated.extend(results_prompt_payload(results))
        prompt = build_universe_followup_prompt(request, tool_results=accumulated)

    if accumulated:
        prompt = build_universe_followup_prompt(
            request,
            tool_results=accumulated,
            allow_tools=False,
        )
    else:
        prompt = build_universe_prompt(request, allow_tools=False)
    completion = router.complete(prompt, timeout_s=timeout_s)
    if isinstance(completion, llm.LlmFailure):
        _raise_failure(completion)
    return _with_tool_trace(
        _parse_final_with_repair(
            completion,
            request=request,
            router=router,
            timeout_s=timeout_s,
            tool_results=accumulated,
        ),
        traces,
        rounds=rounds,
        results=accumulated,
    )


__all__ = ["compose_with_tool_loop"]
