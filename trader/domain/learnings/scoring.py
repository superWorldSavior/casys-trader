"""Scoring FLAIR / MemRL des notes de learning.

In : lignes de notes (id, symbol, family, verdict) ou stats Q d'une citation.
Out : labels de qualité, lift, outcome_score, citation_utility.
Invariant : déterministe, sans I/O ; citation_utility est indépendant de robustness.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

SIGNIFICANT_RETURN_BAND = 0.005
MEMRL_MIN_UPDATES = 10

CITATION_UTILITY_UNKNOWN = "unknown"
CITATION_UTILITY_HELPS = "helps"
CITATION_UTILITY_HURTS = "hurts"
CITATION_UTILITY_NEUTRAL = "neutral"

__all__ = [
    "CITATION_UTILITY_HELPS",
    "CITATION_UTILITY_HURTS",
    "CITATION_UTILITY_NEUTRAL",
    "CITATION_UTILITY_UNKNOWN",
    "MEMRL_MIN_UPDATES",
    "SIGNIFICANT_RETURN_BAND",
    "apply_shrinkage",
    "citation_utility",
    "classify_decision_quality",
    "compute_lift",
    "compute_outcome_scores",
    "is_known_harmful_utility",
]


def classify_decision_quality(
    action: str,
    forward_return: float | None,
    band: float = SIGNIFICANT_RETURN_BAND,
) -> str:
    """BUY/SELL → gagnant|perdant|neutre ; HOLD → justifie|inconnu ; None → non_evaluable."""
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
        # Cette API action-only ne connaît pas le sens de l'opportunité. Un
        # mouvement matériel ne permet donc de conclure ni « raté » ni
        # « prudence ». Les HOLD directionnels passent par decision_benchmark.
        return "justifie" if abs(forward_return) < band else "inconnu"

    return "inconnu"


def _field(row: Mapping[str, Any], key: str) -> Any:
    """Champ obligatoire ; KeyError si absent."""
    return row[key]


def _base_rate(wins: int, losses: int, *, default: float = 0.5) -> float:
    """wins / (wins+losses), ou ``default`` si aucun verdict signé."""
    total = wins + losses
    if total <= 0:
        return default
    return wins / total


def compute_lift(*, verdict: str, base_rate: float) -> float:
    """lift = 1−base_rate si WIN, sinon 0−base_rate."""
    win_indicator = 1.0 if verdict == "WIN" else 0.0
    return win_indicator - base_rate


def apply_shrinkage(lift: float, *, shrinkage_k: float) -> float:
    """outcome_score = lift / (1 + shrinkage_k)."""
    return lift / (1.0 + shrinkage_k)


def is_known_harmful_utility(
    *,
    q_value: float,
    q_updates: int,
    min_updates: int = MEMRL_MIN_UPDATES,
) -> bool:
    """True si q_updates ≥ min_updates et q_value < 0 (Q brut, non shrinké)."""
    return int(q_updates) >= int(min_updates) and float(q_value) < 0.0


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def citation_utility(
    *,
    q_value: float,
    q_updates: int,
    min_updates: int = MEMRL_MIN_UPDATES,
) -> str:
    """helps|hurts|neutral|unknown d'après Q shrinké ; unknown si q_updates < min_updates."""

    updates = _nonnegative_int(q_updates)
    if updates < min_updates:
        return CITATION_UTILITY_UNKNOWN
    shrunk = float(q_value) * updates / (updates + 5.0)
    if shrunk > 0.0:
        return CITATION_UTILITY_HELPS
    if shrunk < 0.0:
        return CITATION_UTILITY_HURTS
    return CITATION_UTILITY_NEUTRAL


def compute_outcome_scores(
    rows: Iterable[Mapping[str, Any]],
    *,
    shrinkage_k: float = 5.0,
) -> dict:
    """Score FLAIR déterministe : {scored, base_rates[symbol], scores[id]}.

    Chaque ligne exige ``id``, ``symbol``, ``family``, ``verdict``.
    WIN/LOSS : lift vs base (symbole ≥5, sinon famille ≥5, sinon global), puis shrinkage.
    Autres verdicts : score 0.
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
