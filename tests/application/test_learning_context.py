from __future__ import annotations

from trader.agent.protocol.parsing import parse_batch, parse_decision
from trader.agent.protocol.types import Decision
from trader.application.decide.learning_context import (
    allowed_global_learning_ids,
    filter_applied_learning_ids,
    normalize_applied_learning_ids,
)


def _shared_context() -> dict:
    return {
        "learnings": {
            "global": [
                {"rule_id": "rule-breakout", "note": "buy confirmations"},
                {"rule_id": "rule-fees", "note": "avoid fee drag"},
                {"rule_id": "rule-risk", "note": "respect stop"},
                {"note": "legacy rule without stable id"},
            ]
        }
    }


def test_global_rule_ids_and_citations_are_bounded_deduplicated_and_prompt_scoped() -> None:
    assert allowed_global_learning_ids(_shared_context()) == {
        "rule-breakout",
        "rule-fees",
        "rule-risk",
    }
    assert normalize_applied_learning_ids(
        [" rule-breakout ", "missing", "rule-breakout", "rule-fees", "rule-risk", "extra"],
        allowed_ids=allowed_global_learning_ids(_shared_context()),
    ) == ["rule-breakout", "rule-fees", "rule-risk"]


def test_filter_applied_ids_drops_ids_not_exposed_to_this_decision() -> None:
    decision = Decision(
        symbol="SPY",
        action="HOLD",
        quantity=0.0,
        confidence=0.5,
        rationale="wait",
        applied_learning_ids=["rule-breakout", "invented", "rule-breakout", "rule-fees"],
    )

    filtered = filter_applied_learning_ids(decision, shared_context=_shared_context())

    assert filtered.applied_learning_ids == ["rule-breakout", "rule-fees"]
    assert decision.applied_learning_ids == ["rule-breakout", "invented", "rule-breakout", "rule-fees"]


def test_protocol_normalizes_optional_applied_ids_for_legacy_and_symbol_calls() -> None:
    legacy = parse_decision(
        '{"symbol":"SPY","action":"HOLD","quantity":0,"confidence":0.4,'
        '"rationale":"wait","applied_learning_ids":[" r1 ","r1",2,"r2","r3","r4"]}',
        "SPY",
    )
    symbol_call = parse_batch(
        '{"decisions":[{"symbol":"SPY","confidence":0.4,"rationale":"wait",'
        '"applied_learning_ids":["r1","r1","r2","r3","r4"],"calls":[]}]}',
        ["SPY"],
        allow_context_request=False,
    )["SPY"]

    assert legacy.applied_learning_ids == ["r1", "r2", "r3"]
    assert symbol_call.applied_learning_ids == ["r1", "r2", "r3"]
