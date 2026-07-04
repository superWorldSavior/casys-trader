"""Handler de décision grain-symbole — 1 symbole = 1 appel LLM = 1 Decision.

Contrairement à batch_decide (qui absorbe les erreurs LLM en HOLD synthétique),
decide_one EXPOSE les erreurs via RetryableError pour que le worker les gère
(requeue + backpressure AIMD). Seul le HOLD délibéré du LLM (llm_error=None)
est retourné tel quel.

Distinction HOLD-délibéré vs erreur :
  _hold_from_llm_failure (codex_client.py) estampille toujours llm_error=failure.code
  → llm_error not None = synthétique (erreur LLM absorbée en amont).
  → llm_error is None = décision authentique du LLM (BUY, SELL, ou HOLD voulu).
"""
from __future__ import annotations

import logging

from trader.agent_protocol.types import Decision
from trader.queue.worker import RetryableError

log = logging.getLogger(__name__)

# Codes LLM signalant une surcharge de la ressource acpx/fournisseur.
# → is_overload=True : le pool AIMD réduit M (M×0.5).
# provider_error = internal error acpx + sortie vide (§4.5 design) — pression app-server,
# retryable avec decrease M au même titre que rate_limited/quota_exceeded.
_OVERLOAD_CODES: frozenset[str] = frozenset({"rate_limited", "quota_exceeded", "provider_error"})


def decide_one(
    *,
    symbol: str,
    mandate: str,
    memory: str,
    shared_context: dict,
    per_symbol_facts: dict,
    decision_timeout_s: int,
    agent_tools_enabled: bool,
    codex_client,
) -> Decision:
    """Décide UN symbole via LLM ; expose les erreurs pour retry/backpressure.

    Paramètres
    ----------
    symbol
        Symbole à décider.
    mandate, memory, shared_context
        Contexte partagé passé tel quel à codex_client.decide_batch.
    per_symbol_facts
        Faits calculés par le code pour CE symbole — construit par l'appelant
        (indicator_triggers, data_age_m, session, active_watches, execution,
        planning, last_llm_review). Correspond à per_symbol[symbol] de batch_decide.
    decision_timeout_s
        Plafond d'appel LLM (en secondes).
    agent_tools_enabled
        Activé = use_symbol_calls_contract=True ; allow_tool_calls=False conservé
        (tournée d'outils non supportée en grain-1 — YAGNI).
    codex_client
        Module ou objet exposant decide_batch — injectable pour les tests.

    Retours / exceptions
    --------------------
    Decision
        Décision LLM authentique (BUY, SELL, ou HOLD délibéré).
    RetryableError(is_overload=True)
        Erreur de surcharge fournisseur (rate_limited, quota_exceeded).
    RetryableError(is_overload=False)
        Autre erreur transitoire (timeout, nonzero_exit, bad_output, exception).
    """
    try:
        responses = codex_client.decide_batch(
            symbols=[symbol],
            mandate=mandate,
            memory=memory,
            shared_context=shared_context,
            per_symbol={symbol: per_symbol_facts},
            allow_context_request=False,
            allow_tool_calls=False,  # grain-1 : tournée outils non supportée (YAGNI)
            use_symbol_calls_contract=agent_tools_enabled,
            timeout_s=decision_timeout_s,
        )
    except Exception as exc:
        raise RetryableError(
            f"decide_batch_exception:{type(exc).__name__}:{exc}",
            is_overload=False,
        ) from exc

    # decide_batch retourne toujours un dict quand allow_tool_calls=False.
    # Guard défensif : si le contrat change, ne pas masquer silencieusement.
    if not isinstance(responses, dict):
        raise RetryableError(
            f"unexpected_response_type:{type(responses).__name__}",
            is_overload=False,
        )

    decision = responses.get(symbol)
    if not isinstance(decision, Decision):
        raise RetryableError(
            f"missing_or_invalid_decision_for:{symbol}",
            is_overload=False,
        )

    # Détection HOLD synthétique (erreur LLM absorbée par decide_batch).
    # _hold_from_llm_failure estampille systématiquement llm_error=failure.code (≠ None).
    # Un HOLD délibéré du LLM a llm_error=None.
    if decision.llm_error is not None:
        is_overload = decision.llm_error in _OVERLOAD_CODES
        log.warning(
            "[decide_one] llm_error=%s symbol=%s is_overload=%s → RetryableError",
            decision.llm_error, symbol, is_overload,
        )
        raise RetryableError(
            f"llm_error:{decision.llm_error}",
            is_overload=is_overload,
        )

    log.debug("[decide_one] ok symbol=%s action=%s", symbol, decision.action)
    return decision
