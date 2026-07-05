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

import trader.agent.tools as agent_tools


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
