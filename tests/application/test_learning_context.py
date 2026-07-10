from __future__ import annotations

from trader.agent.protocol.types import Decision
from trader.application.decide.learning_context import (
    attach_auto_recall_trace,
    build_auto_learning_recall,
)


def test_auto_recall_complete_le_symbole_par_la_famille_et_reste_borne() -> None:
    calls: list[dict] = []

    def provider(args: dict) -> dict:
        calls.append(dict(args))
        if args.get("symbol") == "SPY":
            return {"rows": [{"id": 1, "symbol": "SPY", "note": "local"}]}
        return {
            "rows": [
                {"id": 1, "symbol": "SPY", "note": "duplicate"},
                {"id": 2, "symbol": "QQQ", "note": "family"},
            ]
        }

    recalled = build_auto_learning_recall(provider, symbol="SPY", limit=2)

    assert recalled is not None
    assert [row["id"] for row in recalled["rows"]] == [1, 2]
    assert recalled["scope"] == "automatic_symbol_then_family"
    assert calls[0] == {"symbol": "SPY", "limit": 2}
    assert calls[1]["family"] == recalled["family"]


def test_auto_recall_fail_open() -> None:
    assert build_auto_learning_recall(lambda _args: (_ for _ in ()).throw(RuntimeError("down")), symbol="SPY") is None


def test_trace_auto_recall_preserve_les_outils_du_modele() -> None:
    decision = Decision(
        symbol="SPY",
        action="HOLD",
        quantity=0.0,
        confidence=0.5,
        rationale="wait",
        domain_tools={
            "tool_rounds": 1,
            "tool_calls": [{"id": "model-call", "tool": "get_freshness", "outcome": "ok"}],
        },
    )

    traced = attach_auto_recall_trace(
        decision,
        {
            "family": "us_indices",
            "rows": [{"id": 7}, {"id": 9}],
        },
    )

    assert traced.domain_tools is not None
    assert traced.domain_tools["tool_rounds"] == 1
    assert [call["id"] for call in traced.domain_tools["tool_calls"]] == ["model-call"]
    assert traced.domain_tools["automatic_recall"]["note_ids"] == [7, 9]
