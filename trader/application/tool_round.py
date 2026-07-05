"""Orchestration du tour d'outils LLM — primitives partagées batch / queue.

`run_one_round` est le cœur commun : étant donné un `ToolContext` **déjà construit**
et la requête d'outils du LLM, il exécute UNE tournée bornée et retourne le payload
réinjectable + la trace runtime durable (avec les `note_ids` de recall enrichis, cf
finding V5 : sans cet enrichissement, la table `recalls` ne serait pas alimentée).

La construction du `ToolContext` reste chez l'appelant car elle diffère :
  - batch  : dicts du cycle filtrés par chunk (`planner_batch._run_tool_round`) ;
  - queue  : snapshots du payload + services au boot (grain-1, à venir).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable

import trader.agent.tools as agent_tools
from trader.agent import client as codex_client


def run_one_round(request, *, context, limits=None) -> tuple[list[dict], dict]:
    """Exécute UNE tournée d'outils bornée sur un `ToolContext` construit.

    Parameters
    ----------
    request:
        `BatchToolCallRequest` du LLM (expose `.calls`).
    context:
        `ToolContext` prêt (snapshots + providers) — construit par l'appelant.
    limits:
        `ToolRoundLimits` optionnel (défaut : bornes standard 24/3).

    Returns
    -------
    (results_payload, runtime_payload)
        `results_payload` : liste compacte réinjectable dans le prompt du tour final.
        `runtime_payload`  : dict durable pour `decisions.jsonl` (runtime.tool_*), avec
        les `note_ids` de `recall_learnings` enrichis dans chaque trace concernée.
    """
    results, traces = agent_tools.execute_tool_round(
        request.calls,
        context=context,
        limits=limits or agent_tools.ToolRoundLimits(),
        allowed_tools=frozenset(agent_tools.TOOL_REGISTRY),
    )
    results_prompt = agent_tools.results_prompt_payload(results)
    runtime = agent_tools.round_runtime_payload(traces, rounds=1)
    _enrich_recall_note_ids(runtime, results)
    return results_prompt, runtime


def _enrich_recall_note_ids(runtime: dict, results: list) -> None:
    """Injecte les `note_ids` retournés par `recall_learnings` dans la trace durable.

    Association PAR POSITION : `results` et `tool_calls` sont ordonnés 1:1, sauf une
    éventuelle trace sentinelle `"[sentinel]"` en tête (OUTCOME_TRUNCATED) sans résultat.
    Ces ids sont ensuite lus par `record_decision` → `store.record_recall` (design §4.4).
    """
    tc_list = runtime["tool_calls"]
    sentinel_offset = 1 if (tc_list and tc_list[0].get("id") == "[sentinel]") else 0
    for pos, tc in enumerate(tc_list[sentinel_offset:]):
        if tc["tool"] == "recall_learnings" and tc["outcome"] == agent_tools.OUTCOME_OK:
            r = results[pos] if pos < len(results) else None
            if r and r.ok and isinstance(r.result, dict):
                rows = r.result.get("rows", [])
                note_ids = [row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), int)]
                tc["detail"] = {**tc["detail"], "note_ids": note_ids}


def merge_domain_tools(*, decision_domain_tools: dict | None, runtime_payload: dict, symbol_tool_traces: list[dict]) -> dict:
    """Fusionne les traces d'outils du round avec le `domain_tools` de la décision finale.

    Les traces du round (`symbol_tool_traces`) précèdent celles éventuellement émises
    au tour final, et `tool_rounds` prend le max des deux. Déplacé de `planner_batch`
    ici pour être partagé avec le grain-1 queue sans import circulaire.
    """
    final_tool_calls: list = []
    final_rounds = 0
    final_normalizations = None
    if isinstance(decision_domain_tools, dict):
        raw_final_tool_calls = decision_domain_tools.get("tool_calls")
        if isinstance(raw_final_tool_calls, list):
            final_tool_calls = list(raw_final_tool_calls)
        final_normalizations = decision_domain_tools.get("normalizations")
        try:
            final_rounds = int(decision_domain_tools.get("tool_rounds") or 0)
        except (TypeError, ValueError):
            final_rounds = 0
    try:
        runtime_rounds = int(runtime_payload.get("tool_rounds") or 0)
    except (TypeError, ValueError):
        runtime_rounds = 0
    merged = {
        "tool_rounds": max(runtime_rounds, final_rounds),
        "tool_calls": [*symbol_tool_traces, *final_tool_calls],
    }
    if final_normalizations is not None:
        merged["normalizations"] = final_normalizations
    return merged


def resolve_symbol_decision(
    *,
    symbol: str,
    base_facts: dict,
    tool_context,
    call_model: Callable[..., object],
    max_rounds: int = 1,
    tool_limits=None,
    reinject: str = "cumul",
) -> "codex_client.Decision":
    """Orchestration grain-1 du tour d'outils : round(s) + tour final + merge des traces.

    Équivalent mono-symbole de l'orchestration batch (`planner_batch.py:326-394`),
    réutilisable par le worker de file. Ne connaît ni les flags `allow_*` ni le
    contrat symbol-calls : `call_model` les encapsule (contrat étroit, testable).

    Parameters
    ----------
    symbol:
        Symbole décidé.
    base_facts:
        Faits par-symbole du payload (réinjectés avec `tool_results` au tour suivant).
    tool_context:
        `ToolContext` construit par l'appelant (snapshots du payload + services boot).
    call_model:
        `(per_symbol: dict, *, allow_tool_calls: bool) -> Decision | dict | BatchToolCallRequest`.
        Encapsule l'appel LLM (decide_batch). Retourne un `BatchToolCallRequest` si le LLM
        demande des outils, sinon un dict `{symbol: Decision}` (ou une `Decision`).
    max_rounds:
        Nombre max de tournées d'outils avant le tour final forcé (défaut 1 = parité batch).
        `>1` = mécanique multi-tour (politique « approfondie » reste hors périmètre, cf #1).
    tool_limits:
        `ToolRoundLimits` optionnel transmis à chaque round.

    Returns
    -------
    Decision
        Décision finale, avec `domain_tools` mergé si au moins un round a eu lieu.
        `HOLD("tool_loop_blocked")` si le LLM redemande des outils au tour final.
    """
    if max_rounds < 1:
        raise ValueError("max_rounds doit être >= 1")
    if reinject not in {"cumul", "delta"}:
        raise ValueError("reinject doit être 'cumul' ou 'delta'")

    per_symbol = {symbol: base_facts}
    accumulated_traces: list[dict] = []
    accumulated_results: list[dict] = []
    rounds_done = 0

    for _ in range(max_rounds):
        resp = call_model(per_symbol, allow_tool_calls=True)
        if not isinstance(resp, codex_client.BatchToolCallRequest):
            return _finalize(_decision_of(resp, symbol), accumulated_traces, rounds_done)
        results_payload, runtime = run_one_round(resp, context=tool_context, limits=tool_limits)
        rounds_done += 1
        sym_traces = agent_tools.calls_for_symbol(runtime["tool_calls"], symbol)
        accumulated_traces.extend(sym_traces)
        sym_ids = {t["id"] for t in sym_traces}
        this_round_results = [r for r in results_payload if r["id"] in sym_ids]
        accumulated_results.extend(this_round_results)
        # Cumul stateless = historique complet ; delta session = seulement le dernier round,
        # mais les faits de base restent toujours présents.
        if reinject == "delta":
            per_symbol = {symbol: {**base_facts, "tool_results": list(this_round_results)}}
        else:
            per_symbol = {symbol: {**base_facts, "tool_results": list(accumulated_results)}}

    # Budget de tournées épuisé → tour final, outils interdits (le LLM DOIT décider).
    resp_final = call_model(per_symbol, allow_tool_calls=False)
    if isinstance(resp_final, codex_client.BatchToolCallRequest):
        # Défense en profondeur (design §6.2) : une 2e tournée au tour final est bloquée.
        return codex_client.Decision.hold(symbol, "tool_loop_blocked")
    return _finalize(_decision_of(resp_final, symbol), accumulated_traces, rounds_done)


def _decision_of(resp, symbol: str) -> "codex_client.Decision":
    """Extrait la Decision du symbole d'une réponse LLM (dict batch ou Decision nue)."""
    if isinstance(resp, codex_client.Decision):
        return resp
    if isinstance(resp, dict):
        candidate = resp.get(symbol)
        if isinstance(candidate, codex_client.Decision):
            return candidate
        if isinstance(candidate, codex_client.ContextResearchRequest):
            # Ne devrait pas arriver (allow_context_request=False en queue) ; rendre le
            # cas explicite plutôt que de le masquer en "missing_in_batch" (review R4).
            return codex_client.Decision.hold(symbol, "unexpected_context_request")
    return codex_client.Decision.hold(symbol, "missing_in_batch")


def _finalize(decision: "codex_client.Decision", traces: list[dict], rounds: int) -> "codex_client.Decision":
    """Attache les traces d'outils au `domain_tools` de la décision dès qu'un round a eu lieu.

    Conditionné sur `rounds` (pas sur `traces`) pour rester fidèle au batch : un round qui
    n'a produit aucune trace pour ce symbole reporte quand même `tool_rounds` (review R2).
    """
    if rounds == 0:
        return decision
    runtime_payload = {"tool_rounds": rounds, "tool_calls": traces}
    return replace(
        decision,
        domain_tools=merge_domain_tools(
            decision_domain_tools=decision.domain_tools,
            runtime_payload=runtime_payload,
            symbol_tool_traces=traces,
        ),
    )
