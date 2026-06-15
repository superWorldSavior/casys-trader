"""Rotation / hysteresis logic — veille deux niveaux."""
from __future__ import annotations

from typing import Any


def apply_hysteresis(
    ranked: list[dict[str, Any]],
    *,
    current: set[str],
    dwell: dict[str, int],
    cap_m: int,
    delta: float,
    dwell_days: int,
) -> list[str]:
    """Sélection avec hysteresis incumbents + swap conditionnel.

    Args:
        ranked: items triés attractivité desc, chacun avec {'symbol', 'attractiveness'}.
        current: symboles actuellement chauds.
        dwell: nb de jours chaud par symbole.
        cap_m: nombre max de symboles chauds.
        delta: écart minimum d'attractivité pour déclencher un swap.
        dwell_days: nb de jours minimum avant qu'un incumbent soit évictable.

    Returns:
        Liste des symboles sélectionnés (≤ cap_m).
    """
    def attr(symbol: str) -> float:
        for item in ranked:
            if item["symbol"] == symbol:
                return item["attractiveness"]
        return 0.0

    # Incumbents présents dans ranked, triés attractivité desc, tronqués à cap_m
    incumbents_in_ranked = [
        item["symbol"]
        for item in ranked
        if item["symbol"] in current
    ][:cap_m]

    selected: list[str] = list(incumbents_in_ranked)

    # Entrants = non-incumbents dans l'ordre du ranking
    for item in ranked:
        sym = item["symbol"]
        if sym in current:
            continue  # incumbent déjà traité

        if len(selected) < cap_m:
            # slot libre
            selected.append(sym)
        else:
            # chercher l'incumbent évictable le plus faible
            evictable = [
                s for s in selected
                if s in current and dwell.get(s, 0) >= dwell_days
            ]
            if not evictable:
                continue
            weakest = min(evictable, key=attr)
            if item["attractiveness"] > attr(weakest) + delta:
                selected.remove(weakest)
                selected.append(sym)

    return selected
