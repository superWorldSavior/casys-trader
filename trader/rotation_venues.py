"""Per-venue rotation state helpers."""

from __future__ import annotations

import json
import os
import tempfile
from itertools import zip_longest
from pathlib import Path

import yaml

from trader.rotation import apply_hysteresis, emergency_exits, write_universe_atomic


def empty_venue_state() -> dict:
    """Return an empty per-venue rotation state."""
    return {"venues": {}}


def load_venue_state(state_dir) -> dict:
    """Load per-venue state, returning an empty state if unavailable."""
    path = Path(state_dir) / "venue_state.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        return empty_venue_state()

    if not isinstance(state, dict):
        return empty_venue_state()
    if not isinstance(state.get("venues"), dict):
        return empty_venue_state()
    return state


def save_venue_state(state_dir, state) -> None:
    """Persist per-venue state as indented JSON."""
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "venue_state.json"
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def compose_active_universe(state, open_venues, *, sticky, fx_cap=3) -> list[str]:
    """Compose the currently tradable universe from open venues and sticky symbols."""
    venues = state.get("venues", {})
    action_venues = [venue for venue in open_venues if venue != "FX"]
    action_hotlists = [
        venues.get(venue, {}).get("hotlist", [])
        for venue in action_venues
    ]
    action_candidates = [
        symbol
        for row in zip_longest(*action_hotlists)
        for symbol in row
        if symbol is not None
    ]
    fx = venues.get("FX", {}).get("hotlist", [])[:fx_cap] if "FX" in open_venues else []

    result: list[str] = []
    for symbol in sorted(sticky):
        if symbol not in result:
            result.append(symbol)
    for symbol in action_candidates + fx:
        if symbol not in sticky and symbol not in result:
            result.append(symbol)
    return result


def write_universe_if_changed(path, symbols) -> bool:
    """Write universe symbols only when the non-empty symbol set changes."""
    if not symbols:
        return False

    universe_path = Path(path)
    current_symbols = []
    try:
        data = yaml.safe_load(universe_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            current_symbols = data.get("symbols") or []
    except Exception:
        current_symbols = []

    if set(symbols) == set(current_symbols):
        return False

    write_universe_atomic(str(universe_path), symbols)
    return True


def update_venue_ranking(
    state,
    venue,
    venue_ranked,
    *,
    cap_per_venue,
    delta,
    dwell_days,
    emergency_floor,
    gap_adverse=frozenset(),
    as_of,
) -> dict:
    """Update one venue ranking while preserving other venue entries."""
    venues = state.get("venues", {})
    previous = venues.get(venue, {}) if isinstance(venues, dict) else {}
    old_hotlist = list(previous.get("hotlist", []))
    old_dwell = dict(previous.get("dwell", {}))

    default_hot = apply_hysteresis(
        venue_ranked,
        current=set(old_hotlist),
        dwell=old_dwell,
        cap_m=cap_per_venue,
        delta=delta,
        dwell_days=dwell_days,
    )
    evicted = emergency_exits(
        set(default_hot),
        venue_ranked,
        emergency_floor=emergency_floor,
        gap_adverse=gap_adverse,
    )
    hotlist = [symbol for symbol in default_hot if symbol not in evicted]

    old_hot_set = set(old_hotlist)
    dwell = {
        symbol: old_dwell.get(symbol, 0) + 1 if symbol in old_hot_set else 1
        for symbol in hotlist
    }
    ranked_scores = {item["symbol"]: item["attractiveness"] for item in venue_ranked}
    scores = {symbol: ranked_scores[symbol] for symbol in hotlist if symbol in ranked_scores}

    next_state = dict(state)
    next_venues = dict(venues) if isinstance(venues, dict) else {}
    next_venues[venue] = {
        "hotlist": hotlist,
        "scores": scores,
        "dwell": dwell,
        "last_close_at": as_of,
        "stale": False,
    }
    next_state["venues"] = next_venues
    return next_state
