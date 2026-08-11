"""Handler de décision grain-symbole — 1 symbole = 1 Decision (1 appel LLM,
ou round(s) d'outils + tour final quand le tour d'outils est actif).

Contrairement à batch_decide (qui absorbe les erreurs LLM en HOLD synthétique),
decide_one EXPOSE les erreurs via RetryableError pour que le worker les gère
(requeue + backpressure AIMD). Seul le HOLD délibéré du LLM (llm_error=None)
est retourné tel quel.

Distinction HOLD-délibéré vs erreur :
  _hold_from_llm_failure (codex_client.py) estampille toujours llm_error=failure.code
  → llm_error not None = synthétique (erreur LLM absorbée en amont).
  → llm_error is None = décision authentique du LLM (BUY, SELL, ou HOLD voulu).

TOUR D'OUTILS (spec queue tool-round, issue #2) :
  Avec agent_tools_enabled ET tool_services fournis, decide_one orchestre le tour
  d'outils grain-1 (resolve_symbol_decision) : round(s) d'outils bornés puis tour
  final — l'agent dispose de get_indicator_context / get_active_plans /
  recall_learnings / get_freshness comme en batch. REQUEST_CONTEXT legacy reste
  désactivé (Q4 : le tool round moderne EST la voie de recherche de contexte).
  Sans tool_services (None) : mode dégradé historique Lot A, un appel, aucun outil.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from trader.agent import llm
import trader.agent.tools as agent_tools
from trader.agent.context import build_indicator_resolver
from trader.domain.decisions import Decision
from trader.application.decide.context_projection import project_symbol_facts_for_prompt
from trader.application.decide.protocols import DecisionBatchPlanner
from trader.application.decide.tool_round import resolve_symbol_decision
from trader.application.queue.contracts import RetryableError

if TYPE_CHECKING:
    from trader.application.exit.exit_update import ExitUpdateValidation

log = logging.getLogger("trader.application.decide.one")

# Codes LLM signalant une surcharge de la ressource acpx/fournisseur.
# → is_overload=True : le pool AIMD réduit M (M×0.5).
# provider_error = internal error acpx + sortie vide (§4.5 design) — pression app-server,
# retryable avec decrease M au même titre que rate_limited/quota_exceeded.
_OVERLOAD_CODES: frozenset[str] = frozenset({"rate_limited", "quota_exceeded", "provider_error"})

# Garde-fou anti-runaway, PAS un budget fonctionnel : l'agent s'arrête de
# lui-même en 1-3 tours normalement.
SESSION_ROUND_BACKSTOP = 20


class _UnexpectedResponse(Exception):
    """Type de réponse inattendu de decide_batch (contrat cassé) — interne."""


@dataclass(frozen=True)
class ToolRoundServices:
    """Services au boot pour le tour d'outils en mode queue (spec §4).

    Injectés une fois dans ``make_decide_handler`` ; ``None`` = mode dégradé
    historique (aucun outil). Contrat étroit : uniquement ce que le round consomme.
    """

    get_bars: Callable[..., list]  # wrapper thread-safe (make_indirect_get_bars)
    learnings_recall_provider: Callable[[dict], dict] | None
    max_context_requests_per_symbol: int
    max_indicators_per_request: int
    worker_cycle_context: object | None = None
    action_validator: Callable[[str, dict], "ExitUpdateValidation"] | None = None
    action_validator_factory: Callable[[str | None], Callable[[str, dict], "ExitUpdateValidation"] | None] | None = None
    # Bornes d'outils PAR round. Les 24/3 de ToolRoundLimits sont calibrés batch
    # (chunk de 5 symboles) ; en grain-1, 8 calls pour LE symbole décidé — les
    # outils s'exécutent localement, ce relèvement ne coûte aucun appel acpx.
    max_tool_calls_per_round: int = 24
    max_tool_calls_per_symbol: int = 8

    def tool_limits(self) -> "agent_tools.ToolRoundLimits":
        return agent_tools.ToolRoundLimits(
            max_total_calls=self.max_tool_calls_per_round,
            max_calls_per_symbol=self.max_tool_calls_per_symbol,
        )


def _tool_context_from_facts(
    symbol: str,
    facts: dict,
    shared_context: dict,
    *,
    now: datetime,
    indicator_resolver,
    learnings_recall_provider,
    attribution=None,
    open_plans_provider=None,
    open_plans_as_of_provider=None,
) -> "agent_tools.ToolContext":
    """Reconstruit le ToolContext mono-symbole depuis les snapshots du payload.

    Mapping explicite facts→ToolContext (les formats diffèrent, review V1) :
    ``data_age_m`` (int arrondi) → ``data_age_by_symbol`` (float) ;
    ``execution``/``planning`` → ``market_context_by_symbol[symbol]`` ;
    ``active_watches`` → ``active_watches_by_symbol[symbol]``.
    ``attribution`` peut venir du snapshot complet hors prompt publié pour les
    workers ; fallback sur le contexte partagé pour la compatibilité batch/tests.
    """
    age = facts.get("data_age_m")
    market_ctx = {k: facts[k] for k in ("execution", "planning") if k in facts}
    return agent_tools.ToolContext(
        now=now,
        allowed_symbols=frozenset({symbol}),
        data_age_by_symbol={} if age is None else {symbol: float(age)},
        market_context_by_symbol={symbol: market_ctx} if market_ctx else {},
        active_watches_by_symbol={symbol: list(facts.get("active_watches") or [])},
        attribution=attribution if attribution is not None else shared_context.get("attribution"),
        # recent_decisions poussé dans les facts ; get_position_risk retiré (issue #4).
        indicator_resolver=indicator_resolver,
        learnings_recall_provider=learnings_recall_provider,
        open_plans_provider=open_plans_provider,
        open_plans_as_of_provider=open_plans_as_of_provider,
    )


def _open_plans_provider_for_cycle(
    tool_services: ToolRoundServices,
    cycle_id: str | None,
) -> Callable[[], list] | None:
    handle = tool_services.worker_cycle_context
    if handle is None:
        return None
    return lambda: handle.get_open_plan_rows(cycle_id)


def _open_plans_as_of_provider_for_cycle(
    tool_services: ToolRoundServices,
    cycle_id: str | None,
) -> Callable[[], str | None] | None:
    handle = tool_services.worker_cycle_context
    if handle is None:
        return None
    return lambda: handle.get_open_plans_as_of(cycle_id)


def _attribution_for_cycle(
    tool_services: ToolRoundServices,
    cycle_id: str | None,
    shared_context: dict,
) -> dict | None:
    """Prefer the full worker snapshot over the prompt's focused projection."""

    handle = tool_services.worker_cycle_context
    if handle is None or not hasattr(handle, "get_attribution"):
        value = shared_context.get("attribution")
        return value if isinstance(value, dict) else None
    try:
        value = handle.get_attribution(cycle_id)
    except RuntimeError:
        # Une tache d'un cycle remplace conserve le resume transporte dans son
        # payload, sans lire par erreur le snapshot complet du cycle suivant.
        value = None
    if value:
        return value
    fallback = shared_context.get("attribution")
    return fallback if isinstance(fallback, dict) else None


def _action_validator_for_cycle(
    tool_services: ToolRoundServices,
    cycle_id: str | None,
) -> Callable[[str, dict], "ExitUpdateValidation"] | None:
    if tool_services.action_validator is not None:
        return tool_services.action_validator
    if tool_services.action_validator_factory is None:
        return None
    return tool_services.action_validator_factory(cycle_id)


def _watch_validator(now_fn: Callable[[], datetime] | None) -> Callable[[str, dict], list[dict]]:
    """Dry-run de `propose_indicator_watch` avec le juge de l'application.

    Aucune dépendance d'infra à injecter, contrairement à `action_validator` :
    `build_indicator_watch` est pur (raw + symbole + `now`), d'où la construction
    ici plutôt qu'un élargissement de `ToolRoundServices`.
    """
    from trader.domain.planning.indicator_watch import build_indicator_watch

    def validate(symbol: str, raw_watch: dict) -> list[dict]:
        now = (now_fn or (lambda: datetime.now(timezone.utc)))()
        return build_indicator_watch(raw_watch, owner_symbol=symbol, now=now).rejections

    return validate


def decide_one(
    *,
    symbol: str,
    mandate: str,
    memory: str,
    shared_context: dict,
    per_symbol_facts: dict,
    decision_timeout_s: int,
    agent_tools_enabled: bool,
    codex_client: DecisionBatchPlanner,
    tool_services: ToolRoundServices | None = None,
    symbols_universe: list[str] | None = None,
    cycle_id: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
    session_backends: list | None = None,
    task_id: str | None = None,
    heartbeat: Callable[[], object] | None = None,
) -> tuple[Decision, int]:
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
        Plafond de CHAQUE appel LLM (en secondes).
    agent_tools_enabled
        Active, si tool_services est fourni, le tour d'outils grain-1
        (spec queue tool-round). Le contrat de sortie reste toujours symbol_calls.
    codex_client
        Module ou objet exposant decide_batch — injectable pour les tests.
    tool_services
        Services au boot pour le tour d'outils (spec §4). ``None`` = mode
        dégradé historique (aucun outil, un seul appel).
    symbols_universe
        Univers du cycle (payload) — requis par le resolver d'indicateurs
        (filtre + paires cross-asset). Fallback ``[symbol]`` si absent.
    now_fn
        Horloge injectable (tests). Défaut : UTC now.

    Retours / exceptions
    --------------------
    (Decision, int)
        Décision LLM authentique (BUY, SELL, ou HOLD délibéré) + nombre
        d'appels modèle réellement consommés (1 sans round, 2+ avec).
    RetryableError(is_overload=True)
        Erreur de surcharge fournisseur (rate_limited, quota_exceeded).
    RetryableError(is_overload=False)
        Autre erreur transitoire (timeout, nonzero_exit, bad_output, exception).
    """
    calls_made = 0
    prompt_facts = project_symbol_facts_for_prompt(per_symbol_facts, symbol=symbol)

    def _call_model(per_symbol: dict, *, allow_tool_calls: bool):
        nonlocal calls_made
        calls_made += 1
        return codex_client.decide_batch(
            symbols=[symbol],
            mandate=mandate,
            memory=memory,
            shared_context=shared_context,
            per_symbol=per_symbol,
            allow_context_request=False,
            allow_tool_calls=allow_tool_calls,
            use_symbol_calls_contract=True,
            timeout_s=decision_timeout_s,
            max_rounds=1,
        )

    tools_active = agent_tools_enabled and tool_services is not None
    try:
        if tools_active:
            resolver = build_indicator_resolver(
                symbols=list(symbols_universe or [symbol]),
                get_bars=tool_services.get_bars,
                max_requests=tool_services.max_context_requests_per_symbol,
                max_indicators=tool_services.max_indicators_per_request,
            )
            context = _tool_context_from_facts(
                symbol,
                per_symbol_facts,
                shared_context,
                now=(now_fn or (lambda: datetime.now(timezone.utc)))(),
                indicator_resolver=resolver,
                learnings_recall_provider=tool_services.learnings_recall_provider,
                attribution=_attribution_for_cycle(tool_services, cycle_id, shared_context),
                open_plans_provider=_open_plans_provider_for_cycle(tool_services, cycle_id),
                open_plans_as_of_provider=_open_plans_as_of_provider_for_cycle(tool_services, cycle_id),
            )
            tool_limits = tool_services.tool_limits()
            action_validator = _action_validator_for_cycle(tool_services, cycle_id)
            def _resolve(session):
                session_calls = 0
                if heartbeat is not None:
                    heartbeat()

                def _session_call_model(per_symbol: dict, *, allow_tool_calls: bool):
                    nonlocal calls_made, session_calls
                    session_followup = session_calls > 0
                    calls_made += 1
                    session_calls += 1
                    return codex_client.decide_batch(
                        symbols=[symbol],
                        mandate=mandate,
                        memory=memory,
                        shared_context=shared_context,
                        per_symbol=per_symbol,
                        allow_context_request=False,
                        allow_tool_calls=allow_tool_calls,
                        use_symbol_calls_contract=True,
                        timeout_s=decision_timeout_s,
                        complete_fn=llm.session_complete_fn(
                            session, call_ctx={"task_id": task_id, "symbol": symbol}
                        ),
                        session_followup=session_followup,
                        max_tool_calls_per_symbol=tool_limits.max_calls_per_symbol,
                        max_rounds=None,
                    )

                return resolve_symbol_decision(
                    symbol=symbol,
                    base_facts=prompt_facts,
                    tool_context=context,
                    call_model=_session_call_model,
                    max_rounds=SESSION_ROUND_BACKSTOP,
                    reinject="delta",
                    tool_limits=tool_limits,
                    heartbeat=heartbeat,
                    action_validator=action_validator,
                    watch_validator=_watch_validator(now_fn),
                )

            decision = llm.run_with_session_fallback(
                session_backends,
                task_id=task_id,
                resolve=_resolve,
                open_timeout_s=decision_timeout_s + 15,
            )
            if isinstance(decision, llm.LlmFailure):
                raise RetryableError(
                    decision.code,
                    is_overload=decision.code in _OVERLOAD_CODES,
                )
        else:
            # Mode dégradé historique : un seul appel, aucun outil.
            responses = _call_model({symbol: prompt_facts}, allow_tool_calls=False)
            # decide_batch retourne toujours un dict quand allow_tool_calls=False.
            # Guard défensif : si le contrat change, ne pas masquer silencieusement.
            if not isinstance(responses, dict):
                raise _UnexpectedResponse(type(responses).__name__)
            decision = responses.get(symbol)
    except _UnexpectedResponse as exc:
        raise RetryableError(
            f"unexpected_response_type:{exc}",
            is_overload=False,
        ) from exc
    except RetryableError:
        raise
    except Exception as exc:
        raise RetryableError(
            f"decide_batch_exception:{type(exc).__name__}:{exc}",
            is_overload=False,
        ) from exc

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

    log.debug("[decide_one] ok symbol=%s action=%s calls=%d", symbol, decision.action, calls_made)
    return decision, calls_made
