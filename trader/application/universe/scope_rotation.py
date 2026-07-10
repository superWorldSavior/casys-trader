"""Application transitions from venue rankings to immutable candidate scopes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from trader.domain.universe import candidate_scope_id
from trader.domain.universe.candidate_scope import (
    RADAR_CANDIDATES_TOP,
    candidate_run_ids,
    compose_candidate_pool,
    merge_news_challengers,
    retained_news_challengers,
)
from trader.domain.universe.selection import apply_hysteresis, emergency_exits


class NewsChallengerSelector(Protocol):
    """Select fresh-news candidates without owning scope state transitions."""

    candidate_run_ids: dict[str, str]

    def __call__(
        self,
        *,
        venue: str,
        venue_ranked: list[dict[str, Any]],
        radar_symbols: set[str],
    ) -> list[dict[str, Any]]: ...


def update_venue_ranking(
    state: dict[str, Any],
    venue: str,
    venue_ranked: list[dict[str, Any]],
    *,
    cap_per_venue: int,
    delta: float,
    dwell_days: int,
    emergency_floor: float,
    sticky: Iterable[str] = (),
    news_challengers: Iterable[dict[str, Any]] = (),
    observed_candidate_run_ids: Iterable[str] = (),
    gap_adverse: frozenset[str] = frozenset(),
    as_of: str,
) -> dict[str, Any]:
    """Build one close-phase scope while preserving other venue entries."""

    sticky_symbols = set(sticky)
    venues = state.get("venues", {})
    previous = venues.get(venue, {}) if isinstance(venues, dict) else {}
    retained_challengers = retained_news_challengers(
        previous,
        venue_ranked,
        as_of=as_of,
    )
    combined_challengers = merge_news_challengers(
        list(news_challengers),
        retained_challengers,
    )
    candidates = compose_candidate_pool(venue_ranked, combined_challengers)
    candidate_symbols = {candidate["symbol"] for candidate in candidates}
    hotlist_ranked = [
        item
        for item in venue_ranked
        if item.get("symbol") in candidate_symbols and item.get("symbol") not in sticky_symbols
    ]

    old_hotlist = [
        symbol
        for symbol in previous.get("default_hotlist", previous.get("hotlist", []))
        if symbol in candidate_symbols and symbol not in sticky_symbols
    ]
    old_dwell = dict(previous.get("dwell", {}))
    default_hot = apply_hysteresis(
        hotlist_ranked,
        current=set(old_hotlist),
        dwell=old_dwell,
        cap_m=cap_per_venue,
        delta=delta,
        dwell_days=dwell_days,
    )
    evicted = emergency_exits(
        set(default_hot),
        hotlist_ranked,
        emergency_floor=emergency_floor,
        gap_adverse=gap_adverse,
    )
    hotlist = [symbol for symbol in default_hot if symbol not in evicted]
    scope_id = candidate_scope_id(venue, candidates, hotlist, as_of)
    scope_candidate_run_ids = candidate_run_ids(
        candidates,
        list(observed_candidate_run_ids),
    )

    old_hot_set = set(old_hotlist)
    dwell = {symbol: old_dwell.get(symbol, 0) + 1 if symbol in old_hot_set else 1 for symbol in hotlist}
    ranked_scores = {item["symbol"]: item["attractiveness"] for item in hotlist_ranked}
    scores = {symbol: ranked_scores[symbol] for symbol in hotlist if symbol in ranked_scores}

    next_venues = dict(venues) if isinstance(venues, dict) else {}
    next_venues[venue] = {
        "schema_version": 1,
        "scope_phase": "close",
        "candidate_scope_id": scope_id,
        "candidate_scope_as_of": as_of,
        "candidate_run_ids": scope_candidate_run_ids,
        "candidates": candidates,
        "default_hotlist": hotlist,
        "hotlist": hotlist,
        "scores": scores,
        "dwell": dwell,
        "last_close_at": as_of,
        "sticky_context_at_close": sorted(sticky_symbols),
        "stale": False,
    }
    return {**state, "venues": next_venues}


def refresh_preopen_candidate_scope(
    state: dict[str, Any],
    venue: str,
    venue_ranked: list[dict[str, Any]],
    *,
    cap_per_venue: int,
    sticky: Iterable[str] = (),
    news_challenger_fn: NewsChallengerSelector | None = None,
    as_of: str,
) -> tuple[dict[str, Any], bool]:
    """Create a stable pre-open child without advancing close-time dwell."""

    sticky_symbols = set(sticky)
    venues = state.get("venues", {})
    previous = venues.get(venue, {}) if isinstance(venues, dict) else {}
    if not isinstance(previous, dict) or not previous:
        return state, False

    previous_phase = str(previous.get("scope_phase") or "").strip().lower()
    if previous_phase == "preopen":
        parent_scope_id = str(previous.get("parent_candidate_scope_id") or "").strip()
        parent_close_at = str(previous.get("parent_close_at") or "").strip()
        parent_default_hotlist = list(previous.get("parent_default_hotlist") or [])
    else:
        parent_scope_id = str(previous.get("candidate_scope_id") or "").strip()
        parent_close_at = str(previous.get("last_close_at") or "").strip()
        parent_default_hotlist = list(previous.get("default_hotlist", previous.get("hotlist", [])) or [])
    if not parent_scope_id:
        return state, False

    radar_symbols = {
        str(item.get("symbol") or "").strip()
        for item in venue_ranked[:RADAR_CANDIDATES_TOP]
        if isinstance(item, dict) and item.get("symbol")
    }
    news_challengers: list[dict[str, Any]] = []
    observed_run_ids: list[str] = []
    if news_challenger_fn is not None:
        try:
            news_challengers = (
                news_challenger_fn(
                    venue=venue,
                    venue_ranked=venue_ranked,
                    radar_symbols=radar_symbols,
                )
                or []
            )
            run_ids_by_venue = getattr(news_challenger_fn, "candidate_run_ids", {})
            if isinstance(run_ids_by_venue, dict):
                run_id = str(run_ids_by_venue.get(venue) or "").strip()
                if run_id:
                    observed_run_ids.append(run_id)
        except Exception:  # noqa: BLE001 - deterministic fallback remains valid
            news_challengers = []

    retained = retained_news_challengers(previous, venue_ranked, as_of=as_of)
    candidates = compose_candidate_pool(
        venue_ranked,
        merge_news_challengers(news_challengers, retained),
    )
    if not candidates:
        return state, False
    pool = {str(item.get("symbol") or "").strip() for item in candidates}
    baseline = [symbol for symbol in parent_default_hotlist if symbol in pool and symbol not in sticky_symbols][
        :cap_per_venue
    ]
    for item in venue_ranked:
        symbol = str(item.get("symbol") or "").strip()
        if len(baseline) >= cap_per_venue:
            break
        if symbol in pool and symbol not in sticky_symbols and symbol not in baseline:
            baseline.append(symbol)

    material_signature = candidate_scope_id(
        venue,
        candidates,
        baseline,
        f"preopen-parent:{parent_scope_id}:sticky:{','.join(sorted(str(symbol) for symbol in sticky_symbols))}",
    )
    if (
        previous_phase == "preopen"
        and previous.get("scope_input_signature") == material_signature
        and previous.get("parent_candidate_scope_id") == parent_scope_id
    ):
        return state, False

    scope_id = candidate_scope_id(venue, candidates, baseline, as_of)
    ranked_scores = {
        str(item.get("symbol") or "").strip(): item.get("attractiveness")
        for item in venue_ranked
        if isinstance(item, dict) and item.get("symbol")
    }
    scope_candidate_run_ids = candidate_run_ids(candidates, observed_run_ids)
    next_entry = {
        **previous,
        "schema_version": 1,
        "scope_phase": "preopen",
        "candidate_scope_id": scope_id,
        "candidate_scope_as_of": as_of,
        "scope_input_signature": material_signature,
        "parent_candidate_scope_id": parent_scope_id,
        "parent_close_at": parent_close_at,
        "parent_default_hotlist": parent_default_hotlist,
        "candidate_run_ids": scope_candidate_run_ids,
        "candidates": candidates,
        "default_hotlist": baseline,
        "hotlist": baseline,
        "scores": {symbol: ranked_scores[symbol] for symbol in baseline if symbol in ranked_scores},
        "dwell": dict(previous.get("dwell") or {}),
        "sticky_context_at_close": sorted(sticky_symbols),
        "sticky_context_at_scope": sorted(sticky_symbols),
        "last_preopen_scope_at": as_of,
        "stale": False,
    }
    next_venues = dict(venues)
    next_venues[venue] = next_entry
    return {**state, "venues": next_venues}, True
