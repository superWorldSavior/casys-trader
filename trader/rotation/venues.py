"""Per-venue rotation state helpers."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

import yaml

from trader.rotation import apply_hysteresis, apply_override, emergency_exits, write_universe_atomic
from trader.rotation.collectors import (
    build_plans_fn,
    build_positions_fn,
    sticky_collector,
)
from trader.rotation.ledger import log_rotation
from trader.rotation.schedule import (
    analyzable_venues,
    closed_sessions_since,
    load_sessions,
    preopen_venues,
)
from trader.rotation.wiring import build_rank_fn, venue_of
from trader.market.radar_config import load_radar_params

# Top N candidats radar persistés par venue pour l'override LLM pré-open (configurable plus tard)
OVERRIDE_CANDIDATES_TOP = 40

# Rétention des fichiers radar_cache (en jours). Fichiers YYYY-MM-DD.json plus vieux = purgés.
RADAR_CACHE_RETENTION_DAYS = 30


def _purge_old_radar_cache(cache_dir: Path, now_iso: str) -> None:
    """Supprime les fichiers radar_cache/YYYY-MM-DD.json de plus de RADAR_CACHE_RETENTION_DAYS jours.

    Les noms non conformes au pattern exact (YYYY-MM-DD.json) sont ignorés (non purgés).
    """
    if not cache_dir.is_dir():
        return
    try:
        now_date = datetime.fromisoformat(now_iso[:10])
    except ValueError:
        return
    for f in cache_dir.glob("*.json"):
        stem = f.stem  # YYYY-MM-DD
        if len(stem) != 10 or stem[4] != "-" or stem[7] != "-":
            continue
        try:
            file_date = datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        age_days = (now_date - file_date).days
        if age_days > RADAR_CACHE_RETENTION_DAYS:
            try:
                f.unlink()
            except OSError:
                pass


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


def run_venue_close(
    state,
    venue,
    rank_obj,
    *,
    cap_per_venue,
    fx_cap,
    delta,
    dwell_days,
    emergency_floor,
    as_of,
) -> dict:
    """Recompute one venue sleeve from a global radar ranking."""
    cap = fx_cap if venue == "FX" else cap_per_venue
    venue_ranked = [
        item for item in rank_obj["ranked"]
        if venue_of(item["symbol"]) == venue
    ]
    gap_venue = frozenset(
        symbol
        for symbol in rank_obj.get("gap_adverse", frozenset())
        if venue_of(symbol) == venue
    )
    return update_venue_ranking(
        state,
        venue,
        venue_ranked,
        cap_per_venue=cap,
        delta=delta,
        dwell_days=dwell_days,
        emergency_floor=emergency_floor,
        gap_adverse=gap_venue,
        as_of=as_of,
    )


def due_venues(now_iso, state, sessions, *, fx_refresh="22:00") -> list[str]:
    """Return venues whose sleeve should be recalculated at now_iso."""
    now = datetime.fromisoformat(now_iso).astimezone(timezone.utc)
    is_weekday = now.weekday() < 5
    venues_state = state.get("venues", {})
    due: set[str] = set()

    for venue in ("TW", "EU", "US"):
        if venue not in sessions:
            continue
        if venue not in venues_state:
            due.add(venue)
            continue
        last = venues_state.get(venue, {}).get("last_close_at", "")
        if closed_sessions_since(now_iso, last, {venue: sessions[venue]}):
            due.add(venue)

    if is_weekday:
        if "FX" not in venues_state:
            due.add("FX")
        else:
            last_fx = venues_state.get("FX", {}).get("last_close_at", "")
            fx_session = {"FX": {"open": "00:00", "close": fx_refresh}}
            if closed_sessions_since(now_iso, last_fx, fx_session):
                due.add("FX")

    return sorted(due)


def tick(
    config_dir,
    state_dir,
    now_iso,
    *,
    rank_fn=None,
    sticky_fn=None,
    fx_cap=3,
    override_fn=None,
    market_context=None,
) -> dict:
    """Run one per-venue rotation cycle and reconcile the active universe.

    Args:
        override_fn: callable(payload) -> {"add": [...], "remove": [...]} injectée pour
            le test/prod. Si params.override_enabled=False, n'est JAMAIS appelée.
            Si None, pas d'override (rotation 100 % déterministe).
        market_context: dict optionnel transmis au payload override (v1 : regime_families).
    """
    config_path = Path(config_dir)
    sessions = load_sessions(config_dir)
    params = load_radar_params(config_path)
    state = load_venue_state(state_dir)

    dues = due_venues(now_iso, state, sessions)
    if dues:
        if rank_fn is not None:
            scan_fn = rank_fn
        else:
            from trader.market.radar_data import download_daily_batch
            cache_dir = Path(state_dir) / "radar_cache"

            def _fetch(syms):
                # cache par JOUR (now_iso[:10]) pour réutiliser le batch dans la journée
                return download_daily_batch(syms, as_of=now_iso[:10], cache_dir=cache_dir)

            scan_fn = build_rank_fn(config_dir, fetch_fn=_fetch, as_of=now_iso)
        rank_obj = scan_fn()
        if rank_fn is None:
            _purge_old_radar_cache(Path(state_dir) / "radar_cache", now_iso)
        for venue in dues:
            state = run_venue_close(
                state,
                venue,
                rank_obj,
                cap_per_venue=params.cap_m,
                fx_cap=fx_cap,
                delta=params.delta,
                dwell_days=params.dwell_days,
                emergency_floor=params.emergency_score,
                as_of=now_iso,
            )
        save_venue_state(state_dir, state)

    open_v = analyzable_venues(
        now_iso, sessions, preopen_window_minutes=params.preopen_window_minutes
    )
    if sticky_fn is None:
        sticky = sticky_collector(
            positions_fn=build_positions_fn(state_dir),
            plans_fn=build_plans_fn(state_dir),
        )
    else:
        sticky = sticky_fn()

    # Hook override pré-open par venue (B3/B4)
    # Un appel LLM max par venue par jour, seulement si override_enabled
    if override_fn is not None and params.override_enabled:
        now_date = now_iso[:10]  # YYYY-MM-DD
        preopen_v = preopen_venues(now_iso, sessions, window_minutes=params.preopen_window_minutes)
        ledger_path = Path(state_dir) / "rotation_ledger.jsonl"

        for venue in preopen_v:
            # Vérifier si l'override a déjà tourné aujourd'hui pour cette venue
            venue_meta = state.get("venues", {}).get(venue, {})
            last_override_at = venue_meta.get("last_override_at", "")
            if last_override_at.startswith(now_date):
                continue  # déjà traité aujourd'hui

            # Normalise bias : un venue_state.json legacy (candidats persistés avant
            # l'ajout du bias) ferait lever build_override_prompt (KeyError). Défensif.
            candidates = [
                {**c, "bias": c.get("bias", "long")}
                for c in venue_meta.get("candidates", [])
            ]
            if not candidates:
                continue  # venue jamais classée, pas de shortlist

            # Base de l'override = déterministe pur ; fallback "hotlist" pour migration
            default_hotlist = list(venue_meta.get("default_hotlist", venue_meta.get("hotlist", [])))
            pool = {c["symbol"] for c in candidates}

            payload = {
                "ranked": candidates,
                "default_hot": default_hotlist,
                "sticky": sticky,
                "market_context": market_context,
            }

            final_hotlist = default_hotlist  # fail-safe
            try:
                override_result = override_fn(payload) or {"add": [], "remove": []}
                add = override_result.get("add", [])
                remove = override_result.get("remove", [])
                final_hotlist, _rejects = apply_override(
                    default_hot=default_hotlist,
                    add=add,
                    remove=remove,
                    pool=pool,
                    sticky=sticky,
                    free_slots=params.cap_m,
                )
                log_rotation(
                    ledger_path,
                    as_of=now_iso,
                    default_hot=set(default_hotlist),
                    final_hot=set(final_hotlist),
                    sticky=sticky,
                    overrides={"venue": venue, "add": add, "remove": remove},
                    rejects={"rejects": _rejects},
                    alerts=[],
                )
            except Exception:  # noqa: BLE001 — fail-safe : rotation jamais bloquée
                pass  # final_hotlist = default_hotlist (conservé)

            # Persister la hotlist finale et last_override_at dans venue_state
            venues = dict(state.get("venues", {}))
            venue_entry = dict(venues.get(venue, {}))
            venue_entry["hotlist"] = final_hotlist
            venue_entry["last_override_at"] = now_iso
            venues[venue] = venue_entry
            state = {**state, "venues": venues}

        save_venue_state(state_dir, state)

    final = compose_active_universe(state, open_v, sticky=sticky, fx_cap=fx_cap)
    written = write_universe_if_changed(str(config_path / "universe.yaml"), final)
    return {"dues": dues, "open": open_v, "final": final, "written": written}


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
    # Hystérésis depuis le déterministe pur ; fallback "hotlist" pour migration
    old_hotlist = list(previous.get("default_hotlist", previous.get("hotlist", [])))
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
    # bias inclus : build_override_prompt l'affiche au LLM (sens directionnel radar).
    # Le droper casse le prompt prod (KeyError 'bias') → override jamais exécuté.
    candidates = [
        {
            "symbol": item["symbol"],
            "attractiveness": item["attractiveness"],
            "bias": item.get("bias", "long"),
        }
        for item in venue_ranked[:OVERRIDE_CANDIDATES_TOP]
    ]
    next_venues[venue] = {
        "candidates": candidates,
        # default_hotlist = déterministe pur (base hystérésis + ledger alpha)
        # hotlist = effectif (initialisé = default_hotlist ; override pré-open l'ajuste)
        "default_hotlist": hotlist,
        "hotlist": hotlist,
        "scores": scores,
        "dwell": dwell,
        "last_close_at": as_of,
        "stale": False,
    }
    next_state["venues"] = next_venues
    return next_state
