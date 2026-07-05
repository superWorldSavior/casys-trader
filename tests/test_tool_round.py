"""T1b — primitive `run_one_round` (spec queue tool-round §6.7).

Cœur commun batch/queue : exécute une tournée d'outils sur un `ToolContext`
construit et enrichit les `note_ids` de `recall_learnings` dans la trace durable
(finding V5 : sans cet enrichissement, la table `recalls` reste vide).
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import trader.agent.tools as agent_tools
from trader.application.tool_round import run_one_round


def test_enrichit_les_note_ids_de_recall_learnings() -> None:
    ctx = agent_tools.ToolContext(
        now=datetime(2026, 7, 5, tzinfo=timezone.utc),
        allowed_symbols=frozenset({"AAPL"}),
        learnings_recall_provider=lambda args: {"rows": [{"id": 42}, {"id": 43}]},
    )
    request = SimpleNamespace(calls=[{"id": "c1", "tool": "recall_learnings", "args": {"symbol": "AAPL"}}])

    results_payload, runtime = run_one_round(request, context=ctx)

    recall_trace = next(tc for tc in runtime["tool_calls"] if tc["tool"] == "recall_learnings")
    assert recall_trace["detail"].get("note_ids") == [42, 43]
    assert runtime["tool_rounds"] == 1
    assert results_payload  # payload réinjectable non vide


def test_outil_non_recall_sans_note_ids() -> None:
    # get_active_plans lit le snapshot (pas de provider) → trace sans note_ids.
    ctx = agent_tools.ToolContext(
        now=datetime(2026, 7, 5, tzinfo=timezone.utc),
        allowed_symbols=frozenset({"AAPL"}),
        active_watches_by_symbol={"AAPL": [{"id": "w1", "kind": "stop"}]},
    )
    request = SimpleNamespace(calls=[{"id": "c1", "tool": "get_active_plans", "args": {"symbol": "AAPL"}}])

    _results, runtime = run_one_round(request, context=ctx)

    trace = next(tc for tc in runtime["tool_calls"] if tc["tool"] == "get_active_plans")
    assert "note_ids" not in trace.get("detail", {})
