"""Per-venue rotation state helpers."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

import yaml

from trader.domain.universe import candidate_scope_id
from trader.market.rotation import apply_hysteresis, apply_override, emergency_exits, write_universe_atomic
from trader.market.rotation.user_overrides import apply_user_overrides, load_user_overrides
from trader.market.rotation.ledger import log_rotation
from trader.market.rotation.schedule import (
    analyzable_venues,
    closed_sessions_since,
    load_sessions,
    preopen_venues,
)
from trader.market.rotation.wiring import build_rank_fn, venue_of
from trader.market.radar_config import load_radar_params

# Baseline cheap du pool candidat. Les challengers news qualifiés sont ajoutés
# hors de ce quota et le pool composé n'a pas de cap global.
RADAR_CANDIDATES_TOP = 40

# Le scope final est fabriqué sur toute la fenêtre de préparation (90 min par
# défaut), mais la hotlist agent n'est activée qu'à l'approche du gong. Cela
# laisse aux deux runners async le temps de produire brief puis sélection.
UNIVERSE_ACTIVATION_WINDOW_MINUTES = 15

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
    sticky=frozenset(),
    news_challenger_fn=None,
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
    radar_symbols = {
        str(item.get("symbol") or "").strip()
        for item in venue_ranked[:RADAR_CANDIDATES_TOP]
        if item.get("symbol")
    }
    news_challengers = []
    challenger_run_ids: list[str] = []
    if news_challenger_fn is not None:
        try:
            news_challengers = news_challenger_fn(
                venue=venue,
                venue_ranked=venue_ranked,
                radar_symbols=radar_symbols,
            ) or []
            run_ids_by_venue = getattr(news_challenger_fn, "candidate_run_ids", {})
            if isinstance(run_ids_by_venue, dict):
                run_id = str(run_ids_by_venue.get(venue) or "").strip()
                if run_id:
                    challenger_run_ids.append(run_id)
        except Exception:  # noqa: BLE001 - advisory scout must not break rotation
            news_challengers = []
    return update_venue_ranking(
        state,
        venue,
        venue_ranked,
        cap_per_venue=cap,
        delta=delta,
        dwell_days=dwell_days,
        emergency_floor=emergency_floor,
        sticky=sticky,
        news_challengers=news_challengers,
        observed_candidate_run_ids=challenger_run_ids,
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
    prepared_universe_fn=None,
    candidate_scope_observer=None,
    radar_score_audit_observer=None,
    market_context=None,
    news_challenger_fn=None,
) -> dict:
    """Run one per-venue rotation cycle and reconcile the active universe.

    Args:
        override_fn: callable(payload) -> {"add": [...], "remove": [...]} injectée pour
            le test/prod. Si params.override_enabled=False, n'est JAMAIS appelée.
            Si None, pas d'override (rotation 100 % déterministe).
        prepared_universe_fn: lecteur pur d'une sélection agent préparée hors du
            chemin daemon. Quand présent, il remplace l'appel LLM synchrone.
        candidate_scope_observer: callback best-effort appelé pour chaque parent
            de clôture et chaque enfant final pré-open à persister hors
            ``venue_state``.
        radar_score_audit_observer: callback best-effort qui persiste le comparatif
            score courant vs shadow. Sa sortie ne participe jamais à la sélection.
        market_context: dict optionnel transmis au payload override (v1 : regime_families).
    """
    config_path = Path(config_dir)
    sessions = load_sessions(config_dir)
    params = load_radar_params(config_path)
    state = load_venue_state(state_dir)
    # Sticky must be known before venue closes are recomputed: it is outside the
    # hotlist quota and must not consume one of its slots even when top-ranked.
    sticky = sticky_fn() if sticky_fn is not None else set()

    dues = due_venues(now_iso, state, sessions)
    preopen_v = preopen_venues(now_iso, sessions, window_minutes=params.preopen_window_minutes)
    # La persistance canonique est la frontière qui permet aux runners async de
    # consommer le scope. Les appels unitaires/legacy sans observer conservent
    # donc leur comportement historique et ne fabriquent pas un enfant orphelin.
    preopen_scope_venues = preopen_v if candidate_scope_observer is not None else []
    if dues or preopen_scope_venues:
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
        score_audit_status = "not_available"
        score_audit_ref = None
        score_audit_error = None
        if radar_score_audit_observer is not None and isinstance(
            rank_obj.get("score_audit"), dict
        ):
            try:
                score_audit_ref = radar_score_audit_observer(rank_obj["score_audit"])
                score_audit_status = "persisted"
            except Exception as exc:  # noqa: BLE001 - audit never gates rotation
                score_audit_status = "error"
                score_audit_error = exc.__class__.__name__
        state = {
            **state,
            "radar_score_audit_observation": {
                "status": score_audit_status,
                "as_of": now_iso,
                "ref": score_audit_ref,
                "error": score_audit_error,
                "selection_effect": "none",
            },
        }
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
                sticky=sticky,
                # La clôture fige uniquement le parent quantitatif. Les news
                # arrivées après le gong seront fusionnées dans l'enfant pré-open.
                news_challenger_fn=None,
                as_of=now_iso,
            )
            state = _observe_candidate_scope(
                state,
                venue=venue,
                as_of=now_iso,
                observer=candidate_scope_observer,
            )

        for venue in preopen_scope_venues:
            venue_ranked = [
                item
                for item in rank_obj["ranked"]
                if venue_of(item["symbol"]) == venue
            ]
            state, scope_changed = refresh_preopen_candidate_scope(
                state,
                venue,
                venue_ranked,
                cap_per_venue=params.cap_m,
                sticky=sticky,
                news_challenger_fn=news_challenger_fn,
                as_of=now_iso,
            )
            if scope_changed:
                state = _observe_candidate_scope(
                    state,
                    venue=venue,
                    as_of=now_iso,
                    observer=candidate_scope_observer,
                )
        save_venue_state(state_dir, state)

    open_v = analyzable_venues(
        now_iso, sessions, preopen_window_minutes=params.preopen_window_minutes
    )
    # Activation pré-open par venue. Le chemin nominal lit une sélection déjà
    # préparée hors boucle ; ``override_fn`` reste une compatibilité synchrone.
    if (prepared_universe_fn is not None or override_fn is not None) and params.override_enabled:
        now_date = now_iso[:10]  # YYYY-MM-DD
        ledger_path = Path(state_dir) / "rotation_ledger.jsonl"
        activation_v = preopen_venues(
            now_iso,
            sessions,
            window_minutes=(
                min(
                    params.preopen_window_minutes,
                    UNIVERSE_ACTIVATION_WINDOW_MINUTES,
                )
                if candidate_scope_observer is not None
                else params.preopen_window_minutes
            ),
        )

        for venue in activation_v:
            venue_meta = state.get("venues", {}).get(venue, {})
            candidate_scope_id = str(venue_meta.get("candidate_scope_id") or "").strip()
            if prepared_universe_fn is not None:
                activation_key = candidate_scope_id or f"legacy:{venue}:{venue_meta.get('last_close_at', '')}"
                if venue_meta.get("last_universe_activation_scope_id") == activation_key:
                    continue
            # Compat legacy : au plus un appel synchrone par venue/jour.
            last_override_at = venue_meta.get("last_override_at", "")
            if prepared_universe_fn is None and last_override_at.startswith(now_date):
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
            pool = {c["symbol"] for c in candidates}
            default_hotlist = [
                symbol
                for symbol in venue_meta.get("default_hotlist", venue_meta.get("hotlist", []))
                if symbol in pool and symbol not in sticky
            ][: params.cap_m]

            final_hotlist = default_hotlist  # fail-safe
            fallback_used = False
            fallback_reason = None
            agent_run_id = None
            brief_ref = None
            contract_version = None
            rejected: list[dict] = []

            if prepared_universe_fn is not None:
                prepared = None
                if venue_meta.get("scope_phase") == "close":
                    # Le parent reste un fallback quantitatif valide, mais ne
                    # doit jamais être pris pour une préparation intelligente.
                    fallback_reason = "preopen_scope_missing"
                else:
                    try:
                        prepared = prepared_universe_fn(
                            venue=venue,
                            candidate_scope_id=candidate_scope_id,
                            candidates=candidates,
                            default_hot=default_hotlist,
                            sticky=sticky,
                            as_of=now_iso,
                        )
                    except Exception:  # noqa: BLE001 - activation must remain fail-safe
                        fallback_reason = "prepared_reader_error"
                if isinstance(prepared, dict):
                    agent_run_id = prepared.get("agent_run_id")
                    brief_ref = prepared.get("brief_ref")
                    contract_version = prepared.get("contract_version")
                if isinstance(prepared, dict) and prepared.get("status") == "success":
                    final_hotlist, rejected = _validate_prepared_hotlist(
                        prepared.get("selected_hotlist"),
                        pool=pool,
                        sticky=sticky,
                        cap=params.cap_m,
                    )
                    structural_rejects = [
                        item
                        for item in rejected
                        if item.get("reason") != "sticky_outside_quota"
                    ]
                    if structural_rejects or not final_hotlist:
                        fallback_reason = "prepared_invalid_selection"
                        final_hotlist = default_hotlist
                elif fallback_reason is None:
                    fallback_reason = (
                        str(prepared.get("reason") or prepared.get("status") or "prepare_missing")
                        if isinstance(prepared, dict)
                        else "prepare_missing"
                    )
                fallback_used = fallback_reason is not None
            else:
                payload = {
                    "ranked": candidates,
                    "default_hot": default_hotlist,
                    "sticky": sticky,
                    "market_context": market_context,
                }
                try:
                    override_result = override_fn(payload) or {"add": [], "remove": []}
                    add = override_result.get("add", [])
                    remove = override_result.get("remove", [])
                    final_hotlist, legacy_rejects = apply_override(
                        default_hot=default_hotlist,
                        add=add,
                        remove=remove,
                        pool=pool,
                        sticky=sticky,
                        free_slots=params.cap_m,
                    )
                    rejected = list(legacy_rejects)
                    contract_version = "legacy_delta_v1"
                except Exception:  # noqa: BLE001 — fail-safe : rotation jamais bloquée
                    add = []
                    remove = []
                    fallback_used = True
                    fallback_reason = "legacy_agent_error"

            # Sticky is unioned into the active universe after hotlist selection
            # and therefore never consumes a hotlist slot.
            final_hotlist = [symbol for symbol in final_hotlist if symbol not in sticky][
                : params.cap_m
            ]
            add = [symbol for symbol in final_hotlist if symbol not in default_hotlist]
            remove = [symbol for symbol in default_hotlist if symbol not in final_hotlist]
            selected_challengers = [
                symbol
                for symbol in final_hotlist
                if any(
                    candidate.get("symbol") == symbol and candidate.get("fresh_news")
                    for candidate in candidates
                )
            ]
            attempt_token = ":".join(
                (
                    candidate_scope_id or "missing-scope",
                    str(agent_run_id or "no-agent-run"),
                    str(fallback_reason or "success"),
                )
            )
            if venue_meta.get("last_universe_attempt_token") != attempt_token:
                log_rotation(
                    ledger_path,
                    as_of=now_iso,
                    default_hot=set(default_hotlist),
                    final_hot=set(final_hotlist),
                    sticky=sticky,
                    overrides={
                        "venue": venue,
                        "candidate_scope_id": candidate_scope_id or None,
                        "agent_run_id": agent_run_id,
                        "brief_ref": brief_ref,
                        "contract_version": contract_version,
                        "add": add,
                        "remove": remove,
                        "selected_hotlist": list(final_hotlist),
                        "selected_challengers": selected_challengers,
                        "fallback_used": fallback_used,
                        "fallback_reason": fallback_reason,
                    },
                    rejects={"rejects": rejected},
                    alerts=(
                        [{"code": "universe_fallback", "reason": fallback_reason}]
                        if fallback_used
                        else []
                    ),
                )

            # Persister la hotlist finale et last_override_at dans venue_state
            venues = dict(state.get("venues", {}))
            venue_entry = dict(venues.get(venue, {}))
            venue_entry["hotlist"] = final_hotlist
            venue_entry["last_override_at"] = now_iso
            if prepared_universe_fn is not None:
                venue_entry["last_universe_attempt_token"] = attempt_token
                venue_entry["last_universe_attempt_at"] = now_iso
                venue_entry["last_universe_agent_run_id"] = agent_run_id
                venue_entry["last_universe_brief_ref"] = brief_ref
                venue_entry["last_universe_fallback_used"] = fallback_used
                venue_entry["last_universe_fallback_reason"] = fallback_reason
                venue_entry["last_universe_activation_status"] = (
                    "fallback" if fallback_used else "success"
                )
                if not fallback_used:
                    # A pending/missing/error fallback is an attempt, not a
                    # terminal activation: a prepared run arriving later in the
                    # same pre-open window must still be activatable.
                    venue_entry["last_universe_activation_scope_id"] = activation_key
                    venue_entry["last_universe_activation_at"] = now_iso
            venues[venue] = venue_entry
            state = {**state, "venues": venues}

        save_venue_state(state_dir, state)

    final = compose_active_universe(state, open_v, sticky=sticky, fx_cap=fx_cap)
    user_overrides = load_user_overrides(config_path / "universe.yaml")
    final = apply_user_overrides(
        final, pin=user_overrides.pin, ban=user_overrides.ban, sticky=sticky
    )
    written = write_universe_if_changed(str(config_path / "universe.yaml"), final)
    return {"dues": dues, "open": open_v, "final": final, "written": written}


def _validate_prepared_hotlist(raw, *, pool: set[str], sticky: set[str], cap: int):
    """Validate a full agent selection again at activation time."""

    if not isinstance(raw, list):
        return [], [{"reason": "selected_hotlist_not_list"}]
    selected: list[str] = []
    rejects: list[dict] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            rejects.append({"symbol": value, "reason": "invalid_symbol"})
            continue
        symbol = value.strip()
        if symbol in selected:
            rejects.append({"symbol": symbol, "reason": "duplicate"})
            continue
        if symbol not in pool:
            rejects.append({"symbol": symbol, "reason": "out_of_pool"})
            continue
        if symbol in sticky:
            rejects.append({"symbol": symbol, "reason": "sticky_outside_quota"})
            continue
        if len(selected) >= cap:
            rejects.append({"symbol": symbol, "reason": "cap_exceeded"})
            continue
        selected.append(symbol)
    return selected, rejects


def update_venue_ranking(
    state,
    venue,
    venue_ranked,
    *,
    cap_per_venue,
    delta,
    dwell_days,
    emergency_floor,
    sticky=frozenset(),
    news_challengers=(),
    observed_candidate_run_ids=(),
    gap_adverse=frozenset(),
    as_of,
) -> dict:
    """Update one venue ranking while preserving other venue entries."""
    venues = state.get("venues", {})
    previous = venues.get(venue, {}) if isinstance(venues, dict) else {}
    retained_challengers = _retained_news_challengers(
        previous,
        venue_ranked,
        as_of=as_of,
    )
    combined_challengers = _merge_news_challengers(news_challengers, retained_challengers)
    candidates = _compose_candidate_pool(venue_ranked, combined_challengers)
    candidate_symbols = {candidate["symbol"] for candidate in candidates}
    hotlist_ranked = [
        item
        for item in venue_ranked
        if item.get("symbol") in candidate_symbols and item.get("symbol") not in sticky
    ]

    # Hystérésis depuis le déterministe pur ; fallback "hotlist" pour migration.
    # Un ancien incumbent sorti du pool candidat ne peut pas survivre par inertie.
    old_hotlist = [
        symbol
        for symbol in previous.get("default_hotlist", previous.get("hotlist", []))
        if symbol in candidate_symbols and symbol not in sticky
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
    candidate_run_ids = _candidate_run_ids(candidates, observed_candidate_run_ids)

    old_hot_set = set(old_hotlist)
    dwell = {
        symbol: old_dwell.get(symbol, 0) + 1 if symbol in old_hot_set else 1
        for symbol in hotlist
    }
    ranked_scores = {item["symbol"]: item["attractiveness"] for item in hotlist_ranked}
    scores = {symbol: ranked_scores[symbol] for symbol in hotlist if symbol in ranked_scores}

    next_state = dict(state)
    next_venues = dict(venues) if isinstance(venues, dict) else {}
    next_venues[venue] = {
        "schema_version": 1,
        "scope_phase": "close",
        "candidate_scope_id": scope_id,
        "candidate_scope_as_of": as_of,
        "candidate_run_ids": candidate_run_ids,
        "candidates": candidates,
        # default_hotlist = déterministe pur (base hystérésis + ledger alpha)
        # hotlist = effectif (initialisé = default_hotlist ; override pré-open l'ajuste)
        "default_hotlist": hotlist,
        "hotlist": hotlist,
        "scores": scores,
        "dwell": dwell,
        "last_close_at": as_of,
        "sticky_context_at_close": sorted(sticky),
        "stale": False,
    }
    next_state["venues"] = next_venues
    return next_state


def refresh_preopen_candidate_scope(
    state,
    venue,
    venue_ranked,
    *,
    cap_per_venue,
    sticky=frozenset(),
    news_challenger_fn=None,
    as_of,
) -> tuple[dict, bool]:
    """Create a stable final pre-open child from the latest close parent.

    Hysteresis and dwell remain close-time concerns.  Pre-open only refreshes
    the top-40 radar pool, merges fresh-news challengers and derives a valid
    deterministic fallback from the close baseline.
    """

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
        parent_default_hotlist = list(
            previous.get("default_hotlist", previous.get("hotlist", [])) or []
        )
    if not parent_scope_id:
        return state, False

    radar_symbols = {
        str(item.get("symbol") or "").strip()
        for item in venue_ranked[:RADAR_CANDIDATES_TOP]
        if isinstance(item, dict) and item.get("symbol")
    }
    news_challengers = []
    observed_run_ids: list[str] = []
    if news_challenger_fn is not None:
        try:
            news_challengers = news_challenger_fn(
                venue=venue,
                venue_ranked=venue_ranked,
                radar_symbols=radar_symbols,
            ) or []
            run_ids_by_venue = getattr(news_challenger_fn, "candidate_run_ids", {})
            if isinstance(run_ids_by_venue, dict):
                run_id = str(run_ids_by_venue.get(venue) or "").strip()
                if run_id:
                    observed_run_ids.append(run_id)
        except Exception:  # noqa: BLE001 - the deterministic fallback remains valid
            news_challengers = []

    retained = _retained_news_challengers(previous, venue_ranked, as_of=as_of)
    candidates = _compose_candidate_pool(
        venue_ranked,
        _merge_news_challengers(news_challengers, retained),
    )
    if not candidates:
        return state, False
    pool = {str(item.get("symbol") or "").strip() for item in candidates}
    baseline = [
        symbol
        for symbol in parent_default_hotlist
        if symbol in pool and symbol not in sticky
    ][:cap_per_venue]
    for item in venue_ranked:
        symbol = str(item.get("symbol") or "").strip()
        if len(baseline) >= cap_per_venue:
            break
        if symbol in pool and symbol not in sticky and symbol not in baseline:
            baseline.append(symbol)

    material_signature = candidate_scope_id(
        venue,
        candidates,
        baseline,
        "preopen-parent:"
        f"{parent_scope_id}:sticky:{','.join(sorted(str(symbol) for symbol in sticky))}",
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
    candidate_run_ids = _candidate_run_ids(candidates, observed_run_ids)
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
        "candidate_run_ids": candidate_run_ids,
        "candidates": candidates,
        "default_hotlist": baseline,
        "hotlist": baseline,
        "scores": {
            symbol: ranked_scores[symbol]
            for symbol in baseline
            if symbol in ranked_scores
        },
        # Dwell belongs to the close parent and is intentionally not advanced.
        "dwell": dict(previous.get("dwell") or {}),
        "sticky_context_at_close": sorted(sticky),
        "sticky_context_at_scope": sorted(sticky),
        "last_preopen_scope_at": as_of,
        "stale": False,
    }
    next_state = dict(state)
    next_venues = dict(venues)
    next_venues[venue] = next_entry
    next_state["venues"] = next_venues
    return next_state, True


def _observe_candidate_scope(state, *, venue, as_of, observer) -> dict:
    """Persist one current scope and expose observer failures in venue state."""

    if observer is None:
        return state
    observation_status = "persisted"
    observation_ref = None
    observation_error = None
    try:
        venue_entry = state.get("venues", {}).get(venue, {})
        observation_ref = observer({"venue": venue, "as_of": as_of, **dict(venue_entry)})
    except Exception as exc:  # noqa: BLE001 - rotation remains fail-safe
        observation_status = "error"
        observation_error = exc.__class__.__name__
    venues = dict(state.get("venues", {}))
    venue_entry = dict(venues.get(venue, {}))
    venue_entry["candidate_scope_observation_status"] = observation_status
    venue_entry["candidate_scope_observation_ref"] = observation_ref
    venue_entry["candidate_scope_observation_error"] = observation_error
    venues[venue] = venue_entry
    return {**state, "venues": venues}


def _compose_candidate_pool(venue_ranked, news_challengers) -> list[dict]:
    """Compose top-40 radar plus every eligible news challenger, without a total cap."""

    ranked_by_symbol = {
        str(item.get("symbol") or "").strip(): item
        for item in venue_ranked
        if isinstance(item, dict) and item.get("symbol")
    }
    candidates: list[dict] = []
    index_by_symbol: dict[str, int] = {}

    def radar_candidate(item, *, source: str) -> dict:
        return {
            "symbol": item["symbol"],
            "attractiveness": item["attractiveness"],
            "bias": item.get("bias", "long"),
            "candidate_source": source,
            "candidate_sources": [source],
        }

    for item in venue_ranked[:RADAR_CANDIDATES_TOP]:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol or symbol in index_by_symbol:
            continue
        index_by_symbol[symbol] = len(candidates)
        candidates.append(radar_candidate(item, source="radar"))

    for challenger in news_challengers:
        if not isinstance(challenger, dict):
            continue
        symbol = str(challenger.get("symbol") or "").strip()
        ranked_item = ranked_by_symbol.get(symbol)
        if ranked_item is None:
            # News can bypass the top-40 heuristic, never radar eligibility.
            continue
        provenance = {
            key: value
            for key, value in challenger.items()
            if key not in {"symbol", "attractiveness", "bias", "candidate_sources"}
        }
        if symbol in index_by_symbol:
            existing = candidates[index_by_symbol[symbol]]
            primary_source = existing.get("candidate_source") or "fresh_news"
            sources = list(existing.get("candidate_sources") or [existing.get("candidate_source")])
            if "fresh_news" not in sources:
                sources.append("fresh_news")
            existing.update(provenance)
            existing["candidate_source"] = primary_source
            existing["candidate_sources"] = [source for source in sources if source]
            continue
        candidate = radar_candidate(ranked_item, source="fresh_news")
        candidate.update(provenance)
        candidate["candidate_source"] = "fresh_news"
        candidate["candidate_sources"] = ["fresh_news"]
        index_by_symbol[symbol] = len(candidates)
        candidates.append(candidate)

    return candidates


def _candidate_run_ids(candidates, observed_run_ids) -> list[str]:
    values = {
        str(run_id).strip()
        for run_id in observed_run_ids
        if str(run_id).strip()
    }
    values.update(
        {
            str(metadata.get("candidate_run_id") or "").strip()
            for candidate in candidates
            for metadata in [candidate.get("metadata")]
            if isinstance(metadata, dict)
            and str(metadata.get("candidate_run_id") or "").strip()
        }
    )
    return sorted(values)


def _merge_news_challengers(current, retained) -> list[dict]:
    """Keep current selection order and append only non-refreshed retained symbols."""

    merged: list[dict] = []
    seen: set[str] = set()
    for challenger in [*current, *retained]:
        if not isinstance(challenger, dict):
            continue
        symbol = str(challenger.get("symbol") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        merged.append(challenger)
    return merged


def _retained_news_challengers(previous, venue_ranked, *, as_of) -> list[dict]:
    """Retain unexpired prior challengers while they remain radar-eligible."""

    moment = _parse_utc_datetime(as_of)
    if moment is None:
        return []
    eligible_symbols = {
        str(item.get("symbol") or "").strip()
        for item in venue_ranked
        if isinstance(item, dict) and item.get("symbol")
    }
    retained: list[dict] = []
    for candidate in previous.get("candidates", []) if isinstance(previous, dict) else []:
        if not isinstance(candidate, dict):
            continue
        sources = set(candidate.get("candidate_sources") or ())
        if candidate.get("candidate_source"):
            sources.add(candidate["candidate_source"])
        if "fresh_news" not in sources:
            continue
        symbol = str(candidate.get("symbol") or "").strip()
        if not symbol or symbol not in eligible_symbols:
            continue
        fresh_news = candidate.get("fresh_news")
        if not isinstance(fresh_news, dict):
            continue
        valid_until = _parse_utc_datetime(fresh_news.get("valid_until"))
        if valid_until is None or valid_until <= moment:
            continue
        retained.append(candidate)
    return retained


def _parse_utc_datetime(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
