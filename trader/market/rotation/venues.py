"""Per-venue rotation state helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trader.application.universe import (
    refresh_preopen_candidate_scope,
    update_venue_ranking,
    validate_prepared_hotlist,
)
from trader.domain.universe.candidate_scope import RADAR_CANDIDATES_TOP
from trader.domain.universe.selection import (
    apply_override,
    compose_active_universe,
)
from trader.domain.universe.user_overrides import apply_user_overrides
from trader.infrastructure.files.radar_cache import (
    purge_old_radar_cache as _purge_old_radar_cache,
)
from trader.infrastructure.files.universe_config import (
    load_user_overrides,
    write_universe_if_changed,
)
from trader.infrastructure.files.venue_state import (
    empty_venue_state as empty_venue_state,
    load_venue_state,
    save_venue_state,
)
from trader.market.rotation.ledger import log_rotation
from trader.market.rotation.schedule import (
    analyzable_venues,
    closed_sessions_since,
    load_sessions,
    preopen_venues,
)
from trader.market.rotation.wiring import build_rank_fn, venue_of
from trader.market.radar_config import load_radar_params

# Le scope final est fabriqué sur toute la fenêtre de préparation (90 min par
# défaut), mais la hotlist agent n'est activée qu'à l'approche du gong. Cela
# laisse aux deux runners async le temps de produire brief puis sélection.
UNIVERSE_ACTIVATION_WINDOW_MINUTES = 15

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
    universe_activation_observer=None,
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
        universe_activation_observer: projection best-effort du mandat réellement
            activé; une préparation seule ne lui est jamais transmise comme active.
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
            prepared = None
            fallback_used = False
            fallback_reason = None
            agent_run_id = None
            brief_ref = None
            contract_version = None
            rejected: list[dict] = []

            if prepared_universe_fn is not None:
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
                    final_hotlist, rejected = validate_prepared_hotlist(
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
            activation_observation_status = "not_configured"
            activation_observation_ref = None
            activation_observation_error = None
            if universe_activation_observer is not None:
                try:
                    activation_observation_ref = universe_activation_observer(
                        {
                            "venue": venue,
                            "candidate_scope_id": candidate_scope_id,
                            "as_of": now_iso,
                            "selected_hotlist": list(final_hotlist),
                            "agent_run_id": agent_run_id,
                            "fallback_used": fallback_used,
                            "fallback_reason": fallback_reason,
                        }
                    )
                    activation_observation_status = "persisted"
                except Exception as exc:  # noqa: BLE001 - mandate context never gates activation
                    activation_observation_status = "error"
                    activation_observation_error = exc.__class__.__name__
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
                venue_entry["last_universe_mandate_observation_status"] = activation_observation_status
                venue_entry["last_universe_mandate_observation_ref"] = activation_observation_ref
                venue_entry["last_universe_mandate_observation_error"] = activation_observation_error
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
