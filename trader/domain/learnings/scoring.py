"""Pure FLAIR scoring for learning notes.

No database, filesystem, clock, network, or vector dependency belongs here.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

SIGNIFICANT_RETURN_BAND = 0.005

__all__ = [
    "SIGNIFICANT_RETURN_BAND",
    "apply_shrinkage",
    "classify_decision_quality",
    "compute_lift",
    "compute_outcome_scores",
]


def classify_decision_quality(
    action: str,
    forward_return: float | None,
    band: float = SIGNIFICANT_RETURN_BAND,
) -> str:
    """Classify one directional decision against a significant-return band."""
    if forward_return is None:
        return "non_evaluable"

    normalized = action.upper()
    if normalized == "BUY":
        if forward_return > band:
            return "gagnant"
        if forward_return < -band:
            return "perdant"
        return "neutre"

    if normalized == "SELL":
        if forward_return < -band:
            return "gagnant"
        if forward_return > band:
            return "perdant"
        return "neutre"

    if normalized == "HOLD":
        if forward_return > band:
            return "opportunite_manquee"
        if forward_return < -band:
            return "bonne_prudence"
        return "justifie"

    return "inconnu"


def _field(row: Mapping[str, Any], key: str) -> Any:
    return row[key]


def _base_rate(wins: int, losses: int, *, default: float = 0.5) -> float:
    total = wins + losses
    if total <= 0:
        return default
    return wins / total


def compute_lift(*, verdict: str, base_rate: float) -> float:
    """Return signed lift for a WIN/LOSS verdict against its base rate."""
    win_indicator = 1.0 if verdict == "WIN" else 0.0
    return win_indicator - base_rate


def apply_shrinkage(lift: float, *, shrinkage_k: float) -> float:
    """Shrink a one-observation lift toward zero."""
    return lift / (1.0 + shrinkage_k)


def compute_outcome_scores(
    rows: Iterable[Mapping[str, Any]],
    *,
    shrinkage_k: float = 5.0,
) -> dict:
    """Compute deterministic FLAIR scores for note rows.

    Each row must provide ``id``, ``symbol``, ``family``, and ``verdict``.
    Returns a persistence-agnostic payload:
    ``{"scored": n, "base_rates": {symbol: rate}, "scores": {id: score}}``.
    """
    materialized = list(rows)
    if not materialized:
        return {"scored": 0, "base_rates": {}, "scores": {}}

    wins_by_sym: dict[str, int] = defaultdict(int)
    losses_by_sym: dict[str, int] = defaultdict(int)
    wins_by_family: dict[str, int] = defaultdict(int)
    losses_by_family: dict[str, int] = defaultdict(int)
    global_wins = 0
    global_losses = 0

    for row in materialized:
        verdict = _field(row, "verdict")
        if verdict not in ("WIN", "LOSS"):
            continue
        sym = _field(row, "symbol") or ""
        fam = _field(row, "family") or ""
        if verdict == "WIN":
            wins_by_sym[sym] += 1
            global_wins += 1
            if fam:
                wins_by_family[fam] += 1
        else:
            losses_by_sym[sym] += 1
            global_losses += 1
            if fam:
                losses_by_family[fam] += 1

    global_base_rate = _base_rate(global_wins, global_losses)

    def base_rate_for(sym: str, fam: str) -> float:
        sym_total = wins_by_sym[sym] + losses_by_sym[sym]
        if sym_total >= 5:
            return _base_rate(wins_by_sym[sym], losses_by_sym[sym])
        if fam:
            fam_wins = wins_by_family.get(fam, 0)
            fam_losses = losses_by_family.get(fam, 0)
            if fam_wins + fam_losses >= 5:
                return _base_rate(fam_wins, fam_losses)
        return global_base_rate

    scores: dict[int, float] = {}
    base_rates: dict[str, float] = {}

    for row in materialized:
        note_id = int(_field(row, "id"))
        verdict = _field(row, "verdict")
        sym = _field(row, "symbol") or ""
        fam = _field(row, "family") or ""

        if verdict in ("WIN", "LOSS"):
            br = base_rate_for(sym, fam)
            base_rates[sym] = br
            lift = compute_lift(verdict=verdict, base_rate=br)
            scores[note_id] = apply_shrinkage(lift, shrinkage_k=shrinkage_k)
        else:
            scores[note_id] = 0.0

    return {"scored": len(scores), "base_rates": base_rates, "scores": scores}
