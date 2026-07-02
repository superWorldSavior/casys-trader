"""Batch planner orchestration for the daemon runtime."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from typing import Callable

import trader.agent_tools as agent_tools
from trader.agent import client as codex_client
from trader.agent.context import resolve_indicator_requests
from trader.planning.indicator_watch import summarize_watch
from trader.tools import market, scheduler

log = logging.getLogger(__name__)

DEFAULT_DECISION_BATCH_SIZE = 5
DEFAULT_DECISION_BATCH_PARALLELISM = 3


def _context_request_summary(
    req: codex_client.ContextResearchRequest,
    *,
    resolved: int,
) -> dict:
    return {
        "rounds": 1,
        "requested": [
            {
                "symbol": request.symbol,
                "indicators": list(request.indicators),
                "timeframe": request.timeframe,
            }
            for request in req.requests
        ],
        "resolved": resolved,
    }


def _active_watch_summaries_by_symbol(
    *,
    sched: scheduler.Scheduler | None,
    symbols: list[str],
    now: datetime,
) -> dict[str, list[dict]]:
    summaries = {symbol: [] for symbol in symbols}
    if sched is None:
        return summaries
    for watch in sched.active_indicator_watches(now=now):
        symbol = str(watch.get("symbol"))
        if symbol in summaries:
            summaries[symbol].append(summarize_watch(watch))
    return summaries


def _run_tool_round(
    request: codex_client.BatchToolCallRequest,
    *,
    chunk: list[str],
    now: datetime,
    data_age_by_symbol: dict[str, float],
    market_contexts: dict[str, dict],
    active_watches_by_symbol: dict[str, list],
    shared_context: dict,
    indicator_resolver: object,
    learnings_recall_provider: Callable[[dict], dict] | None = None,
) -> tuple[list[dict], dict]:
    """Exécute UNE tournée d'outils bornée et retourne :
    - results_payload : liste compacte réinjectable dans le prompt du tour final
    - runtime_payload : dict durable pour decisions.jsonl (runtime.tool_*)

    Toute erreur outil est absorbée en résultat compact — aucune exception ne
    remonte au daemon (AX §4 : machine-readable errors, §8 : structured outputs).
    """
    # Borner le contexte au chunk : évite la fuite inter-chunks en parallélisme>1
    # (un call sans symbol explicite comme get_attribution voyait TOUT le batch).
    chunk_set = frozenset(chunk)
    context = agent_tools.ToolContext(
        now=now,
        allowed_symbols=chunk_set,
        data_age_by_symbol={s: v for s, v in data_age_by_symbol.items() if s in chunk_set},
        market_context_by_symbol={s: v for s, v in market_contexts.items() if s in chunk_set},
        active_watches_by_symbol={s: v for s, v in active_watches_by_symbol.items() if s in chunk_set},
        attribution=shared_context.get("attribution"),
        # V0 : providers lourds non câblés — répondent "unavailable" proprement.
        # À brancher quand la mesure d'usage le justifie (Phase 2+).
        position_risk_provider=None,
        recent_decisions_provider=None,
        indicator_resolver=indicator_resolver,
        learnings_recall_provider=learnings_recall_provider,
    )
    results, traces = agent_tools.execute_tool_round(
        request.calls,
        context=context,
        limits=agent_tools.ToolRoundLimits(),
        allowed_tools=frozenset(agent_tools.TOOL_REGISTRY),
    )
    results_prompt = agent_tools.results_prompt_payload(results)
    runtime = agent_tools.round_runtime_payload(traces, rounds=1)

    # Enrichir le detail des appels recall_learnings avec les note_ids retournés.
    # Ces ids sont stockés dans le trace durable (decisions.jsonl runtime.tool_calls)
    # et utilisés par record_decision pour appeler store.record_recall (design §4.4).
    #
    # Association PAR POSITION : results et tool_calls sont ordonnés 1:1,
    # sauf une éventuelle trace sentinel "[sentinel]" en tête de tc_list
    # (OUTCOME_TRUNCATED, id="[sentinel]") sans résultat correspondant.
    tc_list = runtime["tool_calls"]
    sentinel_offset = 1 if (tc_list and tc_list[0].get("id") == "[sentinel]") else 0
    for pos, tc in enumerate(tc_list[sentinel_offset:]):
        if tc["tool"] == "recall_learnings" and tc["outcome"] == agent_tools.OUTCOME_OK:
            r = results[pos] if pos < len(results) else None
            if r and r.ok and isinstance(r.result, dict):
                rows = r.result.get("rows", [])
                note_ids = [
                    row["id"] for row in rows
                    if isinstance(row, dict) and isinstance(row.get("id"), int)
                ]
                tc["detail"] = {**tc["detail"], "note_ids": note_ids}

    return results_prompt, runtime


def batch_decide(
    *,
    decidable: list[str],
    mandate: str,
    memory: str,
    shared_context: dict,
    triggers_by_symbol: dict[str, list[dict]],
    tradable_bars_by_symbol: dict[str, list],
    tradable_symbols: list[str],
    runtime_interval: str,
    runtime_lookback: str,
    max_context_requests_per_symbol: int,
    max_indicators_per_request: int,
    max_model_calls: int,
    now: datetime,
    data_age_by_symbol: dict[str, float],
    sched: scheduler.Scheduler | None = None,
    last_review_by_symbol: dict[str, dict] | None = None,
    market_context_by_symbol: dict[str, dict] | None = None,
    decision_timeout_s: int = 900,
    decision_batch_size: int = DEFAULT_DECISION_BATCH_SIZE,
    decision_batch_parallelism: int = DEFAULT_DECISION_BATCH_PARALLELISM,
    agent_tools_enabled: bool = False,
    learnings_recall_provider: Callable[[dict], dict] | None = None,
    indicator_request_resolver: Callable = resolve_indicator_requests,
    event_appender: Callable[..., None] | None = None,
) -> tuple[dict[str, codex_client.Decision], int]:
    """Décide les symboles dus par chunks LLM bornés et parallélisables.

    Chaque chunk consomme un appel modèle et respecte `max_model_calls`. Les
    demandes REQUEST_CONTEXT sont résolues puis re-décidées en chunks séparés.
    Quand `agent_tools_enabled=True`, le LLM peut émettre UNE tournée d'outils
    lecture-seule (flag CASYS_AGENT_TOOLS_ENABLED) ; chaque chunk avec tournée
    consomme alors 2 appels modèle au lieu d'un.
    """
    if not decidable:
        return {}, 0
    if max_model_calls < 1:
        return {sym: codex_client.Decision.hold(sym, "model_call_budget_exhausted") for sym in decidable}, 0
    active_watches_by_symbol = _active_watch_summaries_by_symbol(sched=sched, symbols=decidable, now=now)

    reviews = last_review_by_symbol or {}
    market_contexts = market_context_by_symbol or {}

    def _symbol_facts(sym: str) -> dict:
        # Faits calculés par le code (pas des consignes en prose) : âge réel des
        # prix et état de la séance de la place du symbole. Âge inconnu = None.
        age = data_age_by_symbol.get(sym)
        session = market.session_snapshot(sym, now=now)
        facts = {
            "data_age_m": None if age is None else int(round(age)),
            "session": {"open": bool(session.get("open"))},
            "active_watches": active_watches_by_symbol.get(sym, []),
        }
        # Séparation analyse/exécution (§5.1) : le LLM voit s'il peut exécuter
        # (execution.enabled) distinctement de s'il peut seulement analyser/planifier
        # (planning.enabled) — il ne confond plus une thèse swing et un ordre immédiat.
        mc = market_contexts.get(sym)
        if mc:
            facts["execution"] = mc.get("execution")
            facts["planning"] = mc.get("planning")
        # Continuité de thèse : le dernier verdict LLM persisté dans le TradePlan
        # (sessions acpx jetables) est réinjecté au réveil d'une position ouverte.
        review = reviews.get(sym)
        if review:
            facts["last_llm_review"] = review
        return facts

    per_symbol = {
        sym: {"indicator_triggers": triggers_by_symbol.get(sym, []), **_symbol_facts(sym)}
        for sym in decidable
    }

    batch_size = max(1, int(decision_batch_size))
    parallelism = max(1, int(decision_batch_parallelism))

    # Résolveur d'indicateurs réutilisable pour la tournée d'outils (mêmes
    # bornes que REQUEST_CONTEXT — le coût d'un resolve reste identique).
    def _indicator_resolver(requests):
        return indicator_request_resolver(
            requests,
            tradable_bars_by_symbol,
            symbols=tradable_symbols,
            max_requests=max_context_requests_per_symbol,
            max_indicators=max_indicators_per_request,
            cached_interval=runtime_interval,
            cached_lookback=runtime_lookback,
        )

    def _chunks(symbols: list[str]) -> list[list[str]]:
        return [symbols[start : start + batch_size] for start in range(0, len(symbols), batch_size)]

    def _decide_chunks(
        *,
        symbols: list[str],
        per_symbol_payload: dict[str, dict],
        allow_context_request: bool,
        budget: int,
    ) -> tuple[dict[str, object], int, list[str]]:
        chunks = _chunks(symbols)
        # Pire cas budget en mode tournée : 2 appels par chunk (tournée + final).
        # On réserve ce pire cas quand allow_tool_calls est actif pour ce pass.
        _chunk_budget = budget // 2 if (agent_tools_enabled and allow_context_request) else budget
        allowed_chunks = chunks[: max(0, _chunk_budget)]
        skipped = [sym for chunk in chunks[max(0, _chunk_budget) :] for sym in chunk]
        log.debug(
            "planner_batch chunks=%d allowed=%d skipped=%d budget=%d tool_mode=%s",
            len(chunks),
            len(allowed_chunks),
            len(skipped),
            budget,
            bool(agent_tools_enabled and allow_context_request),
        )
        if not allowed_chunks:
            return {}, 0, skipped

        def _call(chunk: list[str]) -> tuple[dict[str, object], int]:
            """Retourne (réponses par symbole, nombre d'appels modèle consommés).

            Quand agent_tools_enabled, le premier appel peut rendre une tournée
            d'outils (BatchToolCallRequest) ; le daemon l'exécute et relance un
            tour final — tout en absorbant les erreurs (design §11, §4).
            """
            try:
                resp = codex_client.decide_batch(
                    symbols=chunk,
                    mandate=mandate,
                    memory=memory,
                    shared_context=shared_context,
                    per_symbol={sym: per_symbol_payload[sym] for sym in chunk},
                    allow_context_request=allow_context_request,
                    allow_tool_calls=agent_tools_enabled and allow_context_request,
                    timeout_s=decision_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                return (
                    {sym: codex_client.Decision.hold(sym, f"llm_failed:batch_exception:{type(exc).__name__}") for sym in chunk},
                    1,
                )

            if not (agent_tools_enabled and isinstance(resp, codex_client.BatchToolCallRequest)):
                # Comportement historique : réponse décision directe.
                return resp, 1

            # --- Tournée d'outils (flag actif, LLM a demandé des outils) ---
            results_payload, runtime_payload = _run_tool_round(
                resp,
                chunk=chunk,
                now=now,
                data_age_by_symbol=data_age_by_symbol,
                market_contexts=market_contexts,
                active_watches_by_symbol=active_watches_by_symbol,
                shared_context=shared_context,
                indicator_resolver=_indicator_resolver,
                learnings_recall_provider=learnings_recall_provider,
            )
            # Enrichir le payload par-symbole avec les tool_results filtrés.
            # Un call sans symbole explicite (scope global) est réinjecté à tous.
            trace_calls = runtime_payload["tool_calls"]
            per_symbol_round2 = {}
            for sym in chunk:
                sym_traces = agent_tools.calls_for_symbol(trace_calls, sym)
                sym_ids = {t["id"] for t in sym_traces}
                sym_results = [r for r in results_payload if r["id"] in sym_ids]
                per_symbol_round2[sym] = {**per_symbol_payload[sym], "tool_results": sym_results}

            # Tour final : le LLM DOIT décider — plus de tool_calls acceptés.
            try:
                resp2 = codex_client.decide_batch(
                    symbols=chunk,
                    mandate=mandate,
                    memory=memory,
                    shared_context=shared_context,
                    per_symbol=per_symbol_round2,
                    allow_context_request=False,
                    allow_tool_calls=False,
                    timeout_s=decision_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                return (
                    {sym: codex_client.Decision.hold(sym, f"llm_failed:batch_exception:{type(exc).__name__}") for sym in chunk},
                    2,
                )

            # Deuxième tournée d'outils au tour final → blocage HOLD (design §6.2).
            # Défense en profondeur : le parser (allow_tool_calls=False → parse_batch)
            # protège déjà en amont ; ce guard couvre un futur refactor.
            if isinstance(resp2, codex_client.BatchToolCallRequest):
                return (
                    {sym: codex_client.Decision.hold(sym, "tool_loop_blocked") for sym in chunk},
                    2,
                )

            # Attacher les traces d'outils à chaque décision pour persistance ledger.
            final_decisions: dict[str, object] = {}
            for sym in chunk:
                decision = resp2.get(sym, codex_client.Decision.hold(sym, "missing_in_batch"))
                if isinstance(decision, codex_client.Decision):
                    sym_traces = agent_tools.calls_for_symbol(trace_calls, sym)
                    decision = replace(decision, domain_tools={
                        "tool_rounds": runtime_payload["tool_rounds"],
                        "tool_calls": sym_traces,
                    })
                final_decisions[sym] = decision
            return final_decisions, 2

        responses_by_symbol: dict[str, object] = {}
        total_calls = 0
        if parallelism == 1 or len(allowed_chunks) == 1:
            for chunk in allowed_chunks:
                chunk_responses, chunk_n_calls = _call(chunk)
                responses_by_symbol.update(chunk_responses)
                total_calls += chunk_n_calls
        else:
            with ThreadPoolExecutor(max_workers=min(parallelism, len(allowed_chunks))) as executor:
                futures = [executor.submit(_call, chunk) for chunk in allowed_chunks]
                for future in as_completed(futures):
                    chunk_responses, chunk_n_calls = future.result()
                    responses_by_symbol.update(chunk_responses)
                    total_calls += chunk_n_calls
        return responses_by_symbol, total_calls, skipped

    responses, calls, skipped_symbols = _decide_chunks(
        symbols=decidable,
        per_symbol_payload=per_symbol,
        allow_context_request=True,
        budget=max_model_calls,
    )
    decisions: dict[str, codex_client.Decision] = {}
    need: dict[str, codex_client.ContextResearchRequest] = {}
    for sym, resp in responses.items():
        if isinstance(resp, codex_client.ContextResearchRequest):
            need[sym] = resp
        else:
            decisions[sym] = resp
    for sym in skipped_symbols:
        decisions[sym] = codex_client.Decision.hold(sym, "model_call_budget_exhausted")

    if need and calls >= max_model_calls:
        # Budget épuisé : pas de 2e batch pour résoudre les demandes de contexte.
        for sym, req in need.items():
            decisions[sym] = replace(
                codex_client.Decision.hold(sym, "model_call_budget_exhausted_after_context"),
                context_request=_context_request_summary(req, resolved=0),
            )
        need = {}

    if need:
        context_requests: dict[str, dict] = {}
        per_symbol2: dict[str, dict] = {}
        for sym, req in need.items():
            research = indicator_request_resolver(
                req.requests,
                tradable_bars_by_symbol,
                symbols=tradable_symbols,
                max_requests=max_context_requests_per_symbol,
                max_indicators=max_indicators_per_request,
                cached_interval=runtime_interval,
                cached_lookback=runtime_lookback,
            )
            if event_appender is not None:
                event_appender("context_resolved", symbol=sym, requested=len(req.requests), resolved=len(research["requests"]))
            context_requests[sym] = _context_request_summary(req, resolved=len(research["requests"]))
            per_symbol2[sym] = {
                "indicator_triggers": triggers_by_symbol.get(sym, []),
                **_symbol_facts(sym),
                "research": research,
                # Sessions jetables : le 2e batch n'a pas l'historique du 1er ; on
                # repasse la rationale de la demande pour reprendre le raisonnement.
                "prior_rationale": req.rationale,
            }
        responses2, calls2, skipped_context_symbols = _decide_chunks(
            symbols=list(need),
            per_symbol_payload=per_symbol2,
            allow_context_request=False,
            budget=max_model_calls - calls,
        )
        calls += calls2
        for sym in skipped_context_symbols:
            decisions[sym] = replace(
                codex_client.Decision.hold(sym, "model_call_budget_exhausted_after_context"),
                context_request=context_requests[sym],
            )
        for sym in need:
            if sym in skipped_context_symbols:
                continue
            resp2 = responses2.get(sym)
            decision = (
                resp2
                if isinstance(resp2, codex_client.Decision)
                else codex_client.Decision.hold(sym, "context_loop_blocked")
            )
            decisions[sym] = replace(decision, context_request=context_requests[sym])

    return decisions, calls
