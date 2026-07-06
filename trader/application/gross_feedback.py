"""Feedback helpers for gross exposure rejections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

GROSS_REJECT_REASON = "risk:gross_exposure_exceeded"
OPENING_INTENTS = frozenset({"OPEN_LONG", "OPEN_SHORT", "FLIP", "SCALE_IN"})


def summarize_gross_rejections(decisions: Sequence[Mapping[str, object]]) -> dict | None:
    """Summarize opening decisions rejected by the gross exposure gate."""
    symbols = sorted(
        str(decision.get("symbol"))
        for decision in decisions
        if decision.get("reason") == GROSS_REJECT_REASON
        and decision.get("intent") in OPENING_INTENTS
        and decision.get("symbol")
    )
    if not symbols:
        return None
    return {"rejected_opens": len(symbols), "symbols": symbols}
