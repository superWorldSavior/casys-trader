from __future__ import annotations

from trader.agent.protocol.types import Decision
from trader.application.record.decision_entries import (
    build_decision_entry,
    counts_as_llm_review,
    hold_reason_for_decision,
    runtime_tool_audit_fields,
)


def test_build_decision_entry_preserves_llm_metadata_and_tool_audit() -> None:
    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=12.0,
        confidence=0.74,
        rationale="breakout",
        next_wake_in_minutes=45.0,
        next_wake_event="session_open",
        context_request={"why": "vol"},
        intent="OPEN_LONG",
        decision_reason_code="ENTRY_SIGNAL",
        llm_provider="acpx",
        llm_model="gpt-5.5/medium",
        llm_fallback_reason="none",
        learning="note",
        thesis={"setup": "breakout"},
        risk_pct_target=0.01,
        domain_tools={
            "tool_rounds": 1,
            "tool_calls": [{"id": "1", "tool": "get_freshness", "outcome": "ok"}],
            "normalizations": [{"field": "qty"}],
        },
    )

    entry = build_decision_entry(
        symbol="SPY",
        decision=decision,
        effective_quantity=12.0,
        next_wake_in_minutes=30.0,
        next_wake_event_iso="2026-07-06T13:30:00+00:00",
        armed_plan_id=None,
        armed_plan_order=None,
        runtime_data_source="ib",
    )

    assert entry == {
        "symbol": "SPY",
        "action": "BUY",
        "qty": 12.0,
        "confidence": 0.74,
        "rationale": "breakout",
        "next_wake_in_minutes": 30.0,
        "next_wake_requested": 45.0,
        "next_wake_event": "session_open",
        "next_wake_event_iso": "2026-07-06T13:30:00+00:00",
        "context_request": {"why": "vol"},
        "intent": "OPEN_LONG",
        "decision_reason_code": "ENTRY_SIGNAL",
        "decision_source": "llm",
        "model_called": True,
        "llm_provider": "acpx",
        "llm_model": "gpt-5.5/medium",
        "llm_fallback_reason": "none",
        "llm_error": None,
        "learning": "note",
        "applied_learning_ids": [],
        "thesis": {"setup": "breakout"},
        "risk_pct_target": 0.01,
        "trade_plan_created": False,
        "indicator_watch_created": False,
        "indicator_watch_requested": False,
        "indicator_watch_rejections": [],
        "data_source": "ib",
        "tool_rounds": 1,
        "tool_calls": [{"id": "1", "tool": "get_freshness", "outcome": "ok"}],
        "tool_normalizations": [{"field": "qty"}],
    }


def test_build_decision_entry_marks_armed_plan_without_model_call() -> None:
    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=10.0,
        confidence=0.9,
        rationale="armed_plan:SPY:abc123",
        intent="OPEN_LONG",
        decision_reason_code="ARMED_PLAN",
    )

    entry = build_decision_entry(
        symbol="SPY",
        decision=decision,
        effective_quantity=10.0,
        next_wake_in_minutes=None,
        next_wake_event_iso=None,
        armed_plan_id="SPY:abc123",
        armed_plan_order={"action": "BUY", "qty": 10.0},
        runtime_data_source="yfinance",
    )

    assert entry["armed_plan_id"] == "SPY:abc123"
    assert entry["armed_plan_order"] == {"action": "BUY", "qty": 10.0}
    assert entry["decision_source"] == "armed_plan"
    assert entry["model_called"] is False
    assert entry["data_source"] == "yfinance"


def test_runtime_tool_audit_fields_keeps_shape_without_automatic_recall() -> None:
    assert runtime_tool_audit_fields(None) == {"tool_rounds": None, "tool_calls": None}
    assert runtime_tool_audit_fields({"tool_rounds": 2, "tool_calls": [], "normalizations": []}) == {
        "tool_rounds": 2,
        "tool_calls": [],
        "tool_normalizations": [],
    }
    assert "automatic_recall" not in runtime_tool_audit_fields(
        {"automatic_recall": {"note_ids": [1, 2], "mode": "automatic_push"}}
    )


def test_hold_reason_for_decision_distinguishes_infra_and_domain_noops() -> None:
    assert hold_reason_for_decision(decision_source="infra", rationale="no_decision_in_batch") == "no_decision_in_batch"
    assert hold_reason_for_decision(decision_source="llm", rationale="no_decision_in_batch") == "hold"
    assert hold_reason_for_decision(decision_source="llm", rationale="nothing_to_close") == "nothing_to_close"
    assert (
        hold_reason_for_decision(decision_source="armed_plan", rationale="scale_in_without_position")
        == "scale_in_without_position"
    )
    assert hold_reason_for_decision(decision_source="llm", rationale="wait") == "hold"


def test_counts_as_llm_review_requires_real_model_decision_without_failure() -> None:
    assert (
        counts_as_llm_review(
            Decision(
                symbol="SPY",
                action="HOLD",
                quantity=0.0,
                confidence=0.4,
                rationale="attente",
                intent="HOLD",
                llm_provider="acpx",
                llm_model="gpt-5.5/medium",
            )
        )
        is True
    )
    assert counts_as_llm_review(Decision.hold("SPY", "attente")) is False
    assert (
        counts_as_llm_review(
            Decision(
                symbol="SPY",
                action="HOLD",
                quantity=0.0,
                confidence=0.0,
                rationale="no_decision_in_batch",
                intent="HOLD",
                llm_provider="acpx",
            )
        )
        is False
    )
    assert (
        counts_as_llm_review(
            Decision(
                symbol="SPY",
                action="HOLD",
                quantity=0.0,
                confidence=0.4,
                rationale="attente",
                intent="HOLD",
                llm_provider="acpx",
                llm_error="",
            )
        )
        is True
    )
    assert (
        counts_as_llm_review(
            Decision(
                symbol="SPY",
                action="HOLD",
                quantity=0.0,
                confidence=0.0,
                rationale="llm_failed:acpx:timeout",
                intent="HOLD",
                llm_provider="acpx",
            )
        )
        is False
    )
    assert (
        counts_as_llm_review(
            Decision(
                symbol="SPY",
                action="HOLD",
                quantity=0.0,
                confidence=0.0,
                rationale="attente",
                intent="HOLD",
                llm_provider="acpx",
                llm_error="timeout",
            )
        )
        is False
    )
