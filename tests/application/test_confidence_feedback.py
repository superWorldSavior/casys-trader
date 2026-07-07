from __future__ import annotations


def test_merge_gate_feedback_appends_confidence_context_to_agent_note() -> None:
    from trader.application.record.confidence_feedback import merge_gate_feedback

    note = merge_gate_feedback(
        "risk:confidence_below_required",
        "confidence=0.58 required=0.7000 planned_risk_pct=0.005",
        "je tente un long sur cassure",
    )

    assert note is not None
    assert "je tente un long sur cassure" in note
    assert "required=0.7000" in note


def test_merge_gate_feedback_preserves_unrelated_notes() -> None:
    from trader.application.record.confidence_feedback import merge_gate_feedback

    assert merge_gate_feedback("ok", "ctx", "garde") == "garde"
    assert merge_gate_feedback("ok", None, None) is None
    assert merge_gate_feedback("risk:confidence_below_required", None, "x") == "x"
