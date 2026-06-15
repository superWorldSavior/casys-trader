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


def compose_final(
    *,
    default_hot: list[str],
    sticky: set[str],
    cap_m: int,
) -> tuple[list[str], str | None]:
    """Compose la liste finale : sticky hors quota + non-sticky dans le quota restant.

    Returns:
        (final_list, alert) où alert vaut 'sticky_over_cap' si len(sticky) > cap_m.
    """
    alert: str | None = "sticky_over_cap" if len(sticky) > cap_m else None
    free = max(0, cap_m - len(sticky))

    # sticky triés alphabétiquement pour ordre déterministe, puis non-sticky
    sticky_sorted = sorted(sticky)
    from_default = [s for s in default_hot if s not in sticky][:free]

    # Déduplication ordre préservé
    seen: set[str] = set()
    final: list[str] = []
    for s in sticky_sorted + from_default:
        if s not in seen:
            seen.add(s)
            final.append(s)

    return final, alert


def sticky_symbols(
    *,
    positions: set[str],
    armed_plans: set[str],
    exit_watches: set[str],
    pending_orders: set[str],
) -> set[str]:
    """Union des 4 sets de symboles protégés (hors quota)."""
    return positions | armed_plans | exit_watches | pending_orders


def apply_override(
    *,
    default_hot: list[str],
    add: list[str],
    remove: list[str],
    pool: set[str],
    sticky: set[str],
    free_slots: int,
) -> tuple[list[str], list[dict]]:
    """Applique les overrides agent sur default_hot (liste de symboles non-sticky).

    Rejets machine-readable :
    - retrait d'un s in sticky → {"symbol": s, "reason": "sticky_protected"}
    - ajout d'un s not in pool → {"symbol": s, "reason": "out_of_pool"}
    - ajout dépassant free_slots → {"symbol": s, "reason": "cap_exceeded"}

    Returns:
        (result_list, rejections)
    """
    rejections: list[dict] = []
    result = list(default_hot)

    # Appliquer les retraits
    for s in remove:
        if s in sticky:
            rejections.append({"symbol": s, "reason": "sticky_protected"})
        else:
            if s in result:
                result.remove(s)

    # Appliquer les ajouts
    for s in add:
        if s not in pool:
            rejections.append({"symbol": s, "reason": "out_of_pool"})
        elif len(result) >= free_slots:
            rejections.append({"symbol": s, "reason": "cap_exceeded"})
        else:
            if s not in result:
                result.append(s)

    return result, rejections


def emergency_exits(
    hot_set: set[str],
    ranked: list[dict[str, Any]],
    *,
    emergency_floor: float,
    gap_adverse: frozenset[str] = frozenset(),
    daily_invalidated: frozenset[str] = frozenset(),
) -> set[str]:
    """Retourne les symboles de hot_set à évincer immédiatement.

    Un symbole est évincé si :
    - son attractiveness < emergency_floor (0.0 si absent de ranked) ;
    - il est dans gap_adverse ;
    - il est dans daily_invalidated.
    """
    attr_map: dict[str, float] = {item["symbol"]: item["attractiveness"] for item in ranked}

    return {
        s for s in hot_set
        if attr_map.get(s, 0.0) < emergency_floor
        or s in gap_adverse
        or s in daily_invalidated
    }
