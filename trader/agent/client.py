"""codex_client — appel programmatique à Codex (le brain décideur).

Invoque Codex en headless via `acpx --format quiet exec` (codex est l'agent par
défaut d'acpx). On lui passe le contexte + le mandat, et on force une sortie JSON
structurée (le schéma de décision). Aucune stratégie ici : juste le transport vers
le LLM et la validation stricte de sa réponse.

Décision PURE : on coupe les outils (`--allowed-tools ""`) et le terminal
(`--no-terminal`) — le brain ne fait que raisonner sur le contexte fourni, il ne
touche ni au FS ni au shell. `exec` = session jetable (pas d'état partagé) ->
isolation/idempotence : aucun appel ne contamine le suivant (un LLM échantillonne,
ce n'est donc pas du déterminisme bit-à-bit). L'état évolutif de l'agent est
externalisé — `memory.md` (boucle 1, humain) et `state/learnings.jsonl` (boucle 2,
machine) — et repassé dans le contexte à chaque réveil.

Fail-safe : toute erreur (timeout, binaire absent, JSON invalide) -> décision
HOLD. On ne trade JAMAIS sur une réponse douteuse.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from trader.agent import llm
from trader.agent.protocol.parsing import (
    MAX_LEARNING_CHARS as MAX_LEARNING_CHARS,
    _DECISION_KEYS as _DECISION_KEYS,
    _cancel_watch_ids as _cancel_watch_ids,
    _decision_from_dict as _decision_from_dict,
    _decision_reason_code as _decision_reason_code,
    _extract_json as _extract_json,
    _indicator_requests_from as _indicator_requests_from,
    _normalize_learning as _normalize_learning,
    _optional_dict as _optional_dict,
    _optional_float as _optional_float,
    _optional_upper_str as _optional_upper_str,
    _parse_batch_data as _parse_batch_data,
    _response_from_dict as _response_from_dict,
    parse_batch as parse_batch,
    parse_batch_or_tool_calls as parse_batch_or_tool_calls,
    parse_decision as parse_decision,
    parse_decision_or_context_request as parse_decision_or_context_request,
)
from trader.agent.protocol.prompts import (
    _BATCH_COMPACT_SUFFIX as _BATCH_COMPACT_SUFFIX,
    _BATCH_FINAL_CONTRACT as _BATCH_FINAL_CONTRACT,
    _COMPACT_OUTPUT_CONTRACT as _COMPACT_OUTPUT_CONTRACT,
    _DECISION_GUIDANCE as _DECISION_GUIDANCE,
    _OUTPUT_CONTRACT as _OUTPUT_CONTRACT,
    _REASON_CODE_ENUM as _REASON_CODE_ENUM,
    _SYMBOL_CALLS_FINAL_CONTRACT as _SYMBOL_CALLS_FINAL_CONTRACT,
    _TOOL_CATALOG as _TOOL_CATALOG,
    _WATCH_INDICATOR_ENUM as _WATCH_INDICATOR_ENUM,
    _WATCH_OPERATOR_ENUM as _WATCH_OPERATOR_ENUM,
    _batch_compact_contract as _batch_compact_contract,
    _batch_final_contract as _batch_final_contract,
    _exit_plan_contract as _exit_plan_contract,
    _indicator_watch_vocabulary as _indicator_watch_vocabulary,
    _symbol_calls_final_contract as _symbol_calls_final_contract,
    build_batch_prompt as build_batch_prompt,
    build_session_followup_prompt as build_session_followup_prompt,
    build_prompt as build_prompt,
)
from trader.agent.protocol.types import (
    Action as Action,
    BatchToolCallRequest as BatchToolCallRequest,
    ContextResearchRequest as ContextResearchRequest,
    Decision as Decision,
    IndicatorRequest as IndicatorRequest,
    Intent as Intent,
)

# Modèle du brain runtime. Son choix reste distinct des analystes spécialisés ;
# l'effort est imposé à xhigh par le CODEX_HOME de l'app.
DEFAULT_MODEL = llm.DEFAULT_TRADER_MODEL


def build_command(prompt: str, *, acpx_bin: str, model: str, timeout_s: int) -> list[str]:
    """Commande acpx pour une décision pure (codex, sans outils, sortie texte brute)."""
    return llm.build_acpx_command(prompt, acpx_bin=acpx_bin, model=model, timeout_s=timeout_s)


def _attach_llm_metadata(
    response: Decision | ContextResearchRequest | BatchToolCallRequest,
    completion: llm.LlmCompletion,
) -> Decision | ContextResearchRequest | BatchToolCallRequest:
    return replace(
        response,
        llm_provider=completion.provider,
        llm_model=completion.model,
        llm_fallback_reason=completion.fallback_reason,
    )


def _hold_from_llm_failure(symbol: str, failure: llm.LlmFailure) -> Decision:
    return replace(
        Decision.hold(symbol, f"llm_failed:{failure.provider}:{failure.code}:{failure.message[:160]}"),
        llm_provider=failure.provider,
        llm_model=failure.model,
        llm_fallback_reason=failure.fallback_reason,
        llm_error=failure.code,
    )


def decide(
    *,
    symbol: str,
    mandate: str,
    memory: str,
    context: dict,
    acpx_bin: str = "acpx",
    model: str = DEFAULT_MODEL,
    timeout_s: int = 900,
    allow_context_request: bool = False,
    llm_router: llm.LlmRouter | None = None,
) -> Decision | ContextResearchRequest:
    """Appelle Codex (via acpx) et renvoie une Decision validée. Tout échec -> HOLD."""
    prompt = build_prompt(
        mandate=mandate,
        memory=memory,
        context=context,
        allow_context_request=allow_context_request,
    )
    router = llm_router or llm.build_default_router_from_env(acpx_bin=acpx_bin, spark_model=model)
    completion = router.complete(prompt, timeout_s=timeout_s)
    if isinstance(completion, llm.LlmFailure):
        return _hold_from_llm_failure(symbol, completion)

    try:
        if allow_context_request:
            response = parse_decision_or_context_request(completion.text, symbol)
        else:
            response = parse_decision(completion.text, symbol)
        return _attach_llm_metadata(response, completion)
    except Exception as e:  # noqa: BLE001
        return replace(
            Decision.hold(symbol, f"codex_bad_output: {e}"),
            llm_provider=completion.provider,
            llm_model=completion.model,
            llm_fallback_reason=completion.fallback_reason,
            llm_error="bad_output",
        )


def decide_batch(
    *,
    symbols: list[str],
    mandate: str,
    memory: str,
    shared_context: dict,
    per_symbol: dict[str, dict],
    acpx_bin: str = "acpx",
    model: str = DEFAULT_MODEL,
    timeout_s: int = 900,
    allow_context_request: bool = False,
    allow_tool_calls: bool = False,
    use_symbol_calls_contract: bool = False,
    max_tool_calls_per_symbol: int = 3,
    max_rounds: int | None = 1,
    session_followup: bool = False,
    llm_router: llm.LlmRouter | None = None,
    complete_fn: Callable[[str, int], llm.LlmCompletion | llm.LlmFailure] | None = None,
) -> dict[str, Decision | ContextResearchRequest] | BatchToolCallRequest:
    """UN seul appel modèle pour TOUS les symboles dus : le contexte partagé n'est
    envoyé qu'une fois (vs N fois en mode par-symbole). Isolation per-élément +
    tout échec -> HOLD. Retourne un dict symbole -> Decision|ContextResearchRequest,
    ou un BatchToolCallRequest si le LLM demande une tournée d'outils (flag actif).

    ``session_followup`` est réservé à une session ACP déjà initialisée par ce
    même appel logique : il transporte uniquement les nouveaux ``tool_results``.
    Un nouveau backend/fallback doit toujours recommencer avec ``False``.
    """
    if not symbols:
        return {}
    payload = [{"symbol": sym, **(per_symbol.get(sym) or {})} for sym in symbols]
    if session_followup:
        prompt = build_session_followup_prompt(
            symbols_payload=payload,
            allow_tool_calls=allow_tool_calls,
        )
    else:
        prompt = build_batch_prompt(
            mandate=mandate,
            memory=memory,
            shared_context=shared_context,
            symbols_payload=payload,
            allow_context_request=allow_context_request,
            allow_tool_calls=allow_tool_calls,
            use_symbol_calls_contract=use_symbol_calls_contract,
            max_tool_calls_per_symbol=max_tool_calls_per_symbol,
            max_rounds=max_rounds,
        )
    if complete_fn is not None:
        completion = complete_fn(prompt, timeout_s)
    else:
        router = llm_router or llm.build_default_router_from_env(acpx_bin=acpx_bin, spark_model=model)
        completion = router.complete(prompt, timeout_s=timeout_s)
    if isinstance(completion, llm.LlmFailure):
        return {sym: _hold_from_llm_failure(sym, completion) for sym in symbols}
    if allow_tool_calls:
        results = parse_batch_or_tool_calls(completion.text, symbols, allow_context_request=allow_context_request)
        if isinstance(results, BatchToolCallRequest):
            return _attach_llm_metadata(results, completion)
        return {sym: _attach_llm_metadata(resp, completion) for sym, resp in results.items()}
    results = parse_batch(completion.text, symbols, allow_context_request=allow_context_request)
    return {sym: _attach_llm_metadata(resp, completion) for sym, resp in results.items()}
