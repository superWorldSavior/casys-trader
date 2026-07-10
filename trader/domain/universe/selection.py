"""Pure policies for universe selection and active hotlist composition."""

from __future__ import annotations

from itertools import zip_longest
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
    """Select ranked symbols while protecting eligible incumbents."""

    def attractiveness(symbol: str) -> float:
        for item in ranked:
            if item["symbol"] == symbol:
                return item["attractiveness"]
        return 0.0

    selected = [item["symbol"] for item in ranked if item["symbol"] in current][:cap_m]

    for item in ranked:
        symbol = item["symbol"]
        if symbol in current:
            continue
        if len(selected) < cap_m:
            selected.append(symbol)
            continue
        evictable = [
            incumbent for incumbent in selected if incumbent in current and dwell.get(incumbent, 0) >= dwell_days
        ]
        if not evictable:
            continue
        weakest = min(evictable, key=attractiveness)
        if item["attractiveness"] > attractiveness(weakest) + delta:
            selected.remove(weakest)
            selected.append(symbol)

    return selected


def emergency_exits(
    hot_set: set[str],
    ranked: list[dict[str, Any]],
    *,
    emergency_floor: float,
    gap_adverse: frozenset[str] = frozenset(),
    daily_invalidated: frozenset[str] = frozenset(),
) -> set[str]:
    """Return selected symbols invalidated by score or market conditions."""

    attractiveness = {item["symbol"]: item["attractiveness"] for item in ranked}
    return {
        symbol
        for symbol in hot_set
        if attractiveness.get(symbol, 0.0) < emergency_floor or symbol in gap_adverse or symbol in daily_invalidated
    }


def sticky_symbols(
    *,
    positions: set[str],
    armed_plans: set[str],
    exit_watches: set[str],
    pending_orders: set[str],
) -> set[str]:
    """Combine every source of symbols protected outside the hotlist quota."""

    return positions | armed_plans | exit_watches | pending_orders


def compose_final(
    *,
    default_hot: list[str],
    sticky: set[str],
    cap_m: int,
) -> tuple[list[str], str | None]:
    """Compose up to ``cap_m`` selected symbols plus every sticky symbol."""

    selected = [symbol for symbol in default_hot if symbol not in sticky][:cap_m]
    return list(dict.fromkeys([*sorted(sticky), *selected])), None


def apply_override(
    *,
    default_hot: list[str],
    add: list[str],
    remove: list[str],
    pool: set[str],
    sticky: set[str],
    free_slots: int,
) -> tuple[list[str], list[dict]]:
    """Apply a legacy add/remove override within deterministic guardrails."""

    rejections: list[dict] = []
    result = list(default_hot)

    for symbol in remove:
        if symbol in sticky:
            rejections.append({"symbol": symbol, "reason": "sticky_protected"})
        elif symbol in result:
            result.remove(symbol)

    for symbol in add:
        if symbol in sticky:
            rejections.append({"symbol": symbol, "reason": "already_sticky"})
        elif symbol not in pool:
            rejections.append({"symbol": symbol, "reason": "out_of_pool"})
        elif symbol in result:
            continue
        elif len(result) >= free_slots:
            rejections.append({"symbol": symbol, "reason": "cap_exceeded"})
        else:
            result.append(symbol)

    return result, rejections


def compose_active_universe(
    state: dict[str, Any],
    open_venues: list[str],
    *,
    sticky: set[str],
    fx_cap: int = 3,
) -> list[str]:
    """Interleave open venue hotlists after the protected sticky symbols."""

    venues = state.get("venues", {})
    action_venues = [venue for venue in open_venues if venue != "FX"]
    action_hotlists = [venues.get(venue, {}).get("hotlist", []) for venue in action_venues]
    action_candidates = [symbol for row in zip_longest(*action_hotlists) for symbol in row if symbol is not None]
    fx = venues.get("FX", {}).get("hotlist", [])[:fx_cap] if "FX" in open_venues else []
    return list(
        dict.fromkeys(
            [
                *sorted(sticky),
                *(symbol for symbol in [*action_candidates, *fx] if symbol not in sticky),
            ]
        )
    )
