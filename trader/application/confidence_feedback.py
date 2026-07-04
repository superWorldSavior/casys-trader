"""Feedback helpers for confidence-gated decisions."""

from __future__ import annotations


def merge_gate_feedback(
    reason: str | None,
    context: str | None,
    note: str | None,
) -> str | None:
    """Append confidence-gate context to the learning note when useful."""
    if reason == "risk:confidence_below_required" and context:
        feedback = f"[gate confiance] rejet — {context}"
        return f"{note}\n{feedback}" if note else feedback
    return note
