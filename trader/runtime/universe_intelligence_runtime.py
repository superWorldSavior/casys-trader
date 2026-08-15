"""Asynchronous preparation of per-venue universe-agent selections."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yaml

from trader.application.universe import (
    UniverseCompositionAgent,
    UniverseCompositionRequest,
    build_universe_composition_request,
    compose_universe,
)
from trader.agent.universe.agent import (
    build_universe_router_from_env,
    universe_timeout_s,
)
from trader.agent.universe.global_posture_agent import (
    GlobalUniversePostureRequest,
    LlmGlobalPostureAgent,
)
from trader.agent.universe.tool_loop import compose_with_tool_loop
from trader.domain.universe import (
    SymbolMandate,
    UniverseSituationContext,
    UniverseMandate,
    build_global_family_board,
    project_company_briefs_to_universe_context,
    project_brief_to_universe_context,
)
from trader.domain.situation import build_global_situation_digest
from trader.market.radar_config import load_radar_params
from trader.market.rotation.schedule import load_sessions, preopen_venues
from trader.market.rotation.wiring import venue_of
from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.global_family_board_store import GlobalFamilyBoardStore
from trader.infrastructure.state_db.global_situation_digest_store import GlobalSituationDigestStore
from trader.infrastructure.state_db.global_universe_posture_store import GlobalUniversePostureStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.infrastructure.state_db.universe_run_store import UniverseRunStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore
from trader.infrastructure.files.venue_state import load_venue_state
from trader.runtime.protocols import LoggerLike
from trader.runtime.company_context_config import load_company_context_projection_limits
from trader.runtime._retry_policy import (
    DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
    DEFAULT_FAILURE_BACKOFF_MINUTES,
    ensure_utc as _ensure_utc,
    failure_backoff as _shared_failure_backoff,
    failure_delay_seconds as _shared_failure_delay_seconds,
    latest_failure as _latest_failure,
    parse_datetime as _parse_datetime,
)

VENUES = ("TW", "EU", "US")
# Retry delays are deliberately long enough to prevent an unavailable model
# from being called for every daemon tick, while retaining a short repair path
# for a local prepared-projection write failure.  The shared 30/360 ladder lives
# in ``trader.runtime._retry_policy``.
DEFAULT_PREPARED_WRITE_BACKOFF_MINUTES = 1
DEFAULT_PREPARED_WRITE_BACKOFF_MAX_MINUTES = 30
DEFAULT_REGIME_MAX_AGE_HOURS = 96
DEFAULT_ASYNC_STOP_TIMEOUT_S = 1.0
# Cadence of the advisory global universe posture: at most one LLM recompute per
# pre-open window (freshness before each venue gong) and one per cooldown
# otherwise. Anything more is wasted tokens (the posture barely moves intraday).
DEFAULT_POSTURE_REFRESH_COOLDOWN_HOURS = 4.0
# A failed advisory posture refresh must not turn every universe trigger into
# another LLM call. These intervals are intentionally independent from the
# regional-run retry values, even though their current policy is identical.
DEFAULT_GLOBAL_POSTURE_FAILURE_BACKOFF_MINUTES = 30
DEFAULT_GLOBAL_POSTURE_FAILURE_BACKOFF_MAX_MINUTES = 360
# Company briefs are produced by a small worker pool and therefore tend to land
# in waves.  The daemon feeds those events to the coalescing runner with this
# trailing-edge delay so one regional composition sees the latest whole wave
# instead of recalling the model once per company.
DEFAULT_COMPANY_BRIEF_DEBOUNCE_SECONDS = 120.0


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


class _ToolLoopUniverseAgent:
    """Adapte le tool loop à l'interface ``.compose`` attendue par ``compose_universe``.

    Le nombre de tours est le défaut interne de ``compose_with_tool_loop`` (self-limiting :
    un seul tour si l'agent ne demande aucun brief micro). Rien à régler : c'est le
    comportement de l'analyste univers, pas une option.
    """

    def __init__(self, *, router: Any, intelligence_store: Any) -> None:
        self._router = router
        self._intelligence_store = intelligence_store

    def compose(self, request: UniverseCompositionRequest):
        return compose_with_tool_loop(
            request,
            router=self._router,
            intelligence_store=self._intelligence_store,
            timeout_s=universe_timeout_s(),
        )


def tick_universe_intelligence(
    *,
    config_dir: Path,
    state_dir: Path,
    loop_now: datetime,
    logger: LoggerLike | None = None,
    agent: UniverseCompositionAgent | None = None,
    scope_store: CandidateScopeStore | None = None,
    brief_store: NewsMacroBriefStore | None = None,
    run_store: UniverseRunStore | None = None,
    family_board_store: GlobalFamilyBoardStore | None = None,
    global_situation_store: GlobalSituationDigestStore | None = None,
    posture_store: GlobalUniversePostureStore | None = None,
    posture_agent: Any | None = None,
    company_store: CompanyIntelligenceStore | None = None,
    mandate_store: UniverseMandateStore | None = None,
    venues: Iterable[str] = VENUES,
    refresh_venues: Iterable[str] = (),
    force: bool = False,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Prepare exact per-scope selections outside the synchronous daemon path."""

    if os.getenv("CASYS_UNIVERSE_INTELLIGENCE_ENABLED", "1") == "0":
        return {
            "prepared": [],
            "waiting": [],
            "skipped": [{"reason": "disabled"}],
            "errors": [],
        }

    log = logger or _default_logger()
    now = _ensure_utc(loop_now)
    state_path = Path(state_dir)
    scopes = scope_store or CandidateScopeStore(state_path / "candidate_scopes")
    briefs = brief_store or NewsMacroBriefStore(state_path / "news_briefs")
    runs = run_store or UniverseRunStore(state_path / "universe_runs")
    family_boards = family_board_store or GlobalFamilyBoardStore(
        state_path / "global_family_boards"
    )
    global_situations = global_situation_store or GlobalSituationDigestStore(
        state_path / "global_situation_digests"
    )
    postures = posture_store or GlobalUniversePostureStore(
        state_path / "global_universe_postures"
    )
    companies = company_store or CompanyIntelligenceStore(state_path / "company_intelligence")
    mandates = mandate_store or UniverseMandateStore(state_path / "universe_mandates")
    requested_venues = tuple(dict.fromkeys(str(item).strip().upper() for item in venues))
    explicit_refresh_venues = {
        str(item).strip().upper() for item in refresh_venues if str(item).strip()
    }
    activation_state = load_venue_state(state_path)
    activation_skips: list[dict[str, Any]] = []
    runnable_venues: list[str] = []
    for venue in requested_venues:
        scope = scopes.read_current(venue)
        scope_id = str((scope or {}).get("candidate_scope_id") or "").strip()
        if (
            scope_id
            and _scope_already_activated(
                activation_state,
                venue=venue,
                candidate_scope_id=scope_id,
            )
        ):
            activation_skips.append(
                {
                    "venue": venue,
                    "candidate_scope_id": scope_id,
                    "reason": "scope_already_activated",
                }
            )
            (log.info if venue in explicit_refresh_venues else log.debug)(
                "universe refresh skipped venue=%s scope=%s reason=scope_already_activated",
                venue,
                scope_id,
            )
            continue
        runnable_venues.append(venue)

    # In particular, a late micro callback for an already activated venue must
    # not incidentally refresh the global posture before discovering that the
    # regional run itself is terminal.
    if not runnable_venues:
        return {
            "prepared": [],
            "waiting": [],
            "skipped": activation_skips,
            "errors": [],
        }
    company_context_mode = _company_context_mode(Path(config_dir))
    company_context_limits = load_company_context_projection_limits(config_dir)
    market_context = _load_market_context(state_path / "last_regime.json", now=now)
    global_situation_digest, global_situation_ref = _prepare_global_situation_digest(
        briefs=briefs,
        store=global_situations,
        now=now,
        log=log,
    )
    global_family_board, family_board_ref = _prepare_global_family_board(
        scopes=scopes,
        briefs=briefs,
        store=family_boards,
        now=now,
        log=log,
        global_situation_ref=global_situation_ref,
    )
    # Cadence de la posture globale : fraîche avant chaque gong (venues en
    # pré-open), sinon au plus une fois par cooldown. `config_dir` pointe sur
    # ``<root>/config`` (cf. load_radar_params ci-dessus). `load_sessions(root_dir)`
    # attend la racine du repo et concatène lui-même ``config/sessions.yaml``.
    sessions = load_sessions(str(Path(config_dir).parent))
    preopen_window_minutes = load_radar_params(Path(config_dir)).preopen_window_minutes
    preopen_now = preopen_venues(
        now.isoformat(), sessions, window_minutes=preopen_window_minutes
    )
    global_universe_posture, global_universe_posture_ref = (
        _prepare_global_universe_posture(
            scopes=scopes,
            store=postures,
            now=now,
            log=log,
            global_family_board=global_family_board,
            global_situation_digest=global_situation_digest,
            agent=posture_agent,
            preopen_now=preopen_now,
            preopen_window_minutes=preopen_window_minutes,
        )
    )
    prepared: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = list(activation_skips)
    errors: list[dict[str, Any]] = []

    for raw_venue in runnable_venues:
        if stop_requested is not None and stop_requested():
            skipped.append({"reason": "stopping"})
            break
        venue = str(raw_venue).strip().upper()
        scope = scopes.read_current(venue)
        if scope is None:
            skipped.append({"venue": venue, "reason": "candidate_scope_missing"})
            continue
        if str(scope.get("scope_phase") or "").strip().lower() == "close":
            skipped.append({"venue": venue, "reason": "awaiting_preopen_scope"})
            continue
        scope_id = str(scope.get("candidate_scope_id") or "").strip()
        # Activation can race the background runner. Re-read the projection at
        # the last cheap boundary before building the regional request.
        if _scope_already_activated(
            load_venue_state(state_path),
            venue=venue,
            candidate_scope_id=scope_id,
        ):
            skipped.append(
                {
                    "venue": venue,
                    "candidate_scope_id": scope_id,
                    "reason": "scope_already_activated",
                }
            )
            (log.info if venue in explicit_refresh_venues else log.debug)(
                "universe refresh skipped venue=%s scope=%s reason=scope_already_activated",
                venue,
                scope_id,
            )
            continue
        candidates = tuple(
            dict(item)
            for item in scope.get("candidates") or []
            if isinstance(item, Mapping) and item.get("symbol")
        )
        baseline = tuple(str(symbol).strip() for symbol in scope.get("default_hotlist") or ())
        sticky = tuple(str(symbol).strip() for symbol in scope.get("sticky_context_at_close") or ())
        brief = briefs.read_latest(venue, at=now)
        if brief is None:
            _record_waiting_once(
                runs,
                scope=scope,
                now=now,
                reason="brief_missing",
                waiting=waiting,
                skipped=skipped,
            )
            continue
        input_refs = brief.input_refs if isinstance(brief.input_refs, dict) else {}
        if input_refs.get("candidate_scope_id") != scope_id:
            _record_waiting_once(
                runs,
                scope=scope,
                now=now,
                reason="brief_scope_mismatch",
                waiting=waiting,
                skipped=skipped,
                brief_ref=brief.ref(date=brief.as_of[:10]),
            )
            continue

        coverage_metadata = _coverage_metadata(input_refs.get("coverage"))
        situation_context = project_brief_to_universe_context(
            brief,
            venue=venue,
            candidate_symbols=(item["symbol"] for item in candidates),
            active_at=now,
            coverage_metadata=coverage_metadata,
        )
        candidate_symbols = tuple(item["symbol"] for item in candidates)
        company_context = project_company_briefs_to_universe_context(
            companies.read_current_many(candidate_symbols, depth="preferred"),
            candidate_symbols=candidate_symbols,
            active_at=now,
            mode=company_context_mode,
            limits=company_context_limits,
        )
        company_context_payload = company_context.to_dict()
        company_context_hash = _request_signature(company_context_payload)
        company_brief_refs = {
            symbol: dict(entry["brief_ref"])
            for symbol, entry in company_context.symbols.items()
            if isinstance(entry.get("brief_ref"), Mapping)
        }
        try:
            request = build_universe_composition_request(
                venue=venue,
                as_of=str(scope.get("as_of") or ""),
                candidates=candidates,
                baseline=baseline,
                sticky=sticky,
                market_context=market_context,
                situation_context=situation_context,
                company_context=company_context,
                global_family_board=global_family_board,
                global_situation_digest=global_situation_digest,
                global_universe_posture=global_universe_posture,
            )
        except Exception as exc:
            record = _base_run_record(
                scope=scope,
                now=now,
                brief=brief,
                input_signature=None,
            )
            record.update(
                {
                    "status": "invalid",
                    "error_code": "invalid_universe_request",
                    "error_message": str(exc)[:500],
                    "global_family_board": _family_board_observability(
                        global_family_board,
                        family_board_ref,
                    ),
                }
            )
            runs.append(record)
            errors.append({"venue": venue, "reason": "invalid_universe_request"})
            continue
        if request.candidate_scope_id != scope_id:
            record = _base_run_record(
                scope=scope,
                now=now,
                brief=brief,
                input_signature=None,
            )
            record.update(
                {
                    "status": "invalid",
                    "error_code": "candidate_scope_integrity_mismatch",
                    "computed_candidate_scope_id": request.candidate_scope_id,
                    "global_family_board": _family_board_observability(
                        global_family_board,
                        family_board_ref,
                    ),
                }
            )
            runs.append(record)
            errors.append({"venue": venue, "reason": "candidate_scope_integrity_mismatch"})
            continue

        request_payload = request.to_dict()
        # ``as_of`` values are observability/freshness metadata, not a change
        # in the decision evidence by themselves.  Keeping them in the retry
        # key made every daemon tick look novel and bypass the failure guard.
        input_signature = _material_request_signature(request_payload)
        refresh_signature = _universe_refresh_signature(
            venue=venue,
            scope_id=scope_id,
            brief_id=str(getattr(brief, "brief_id", "") or ""),
        )
        retry_lineage = _retry_lineage_key(venue=venue, scope_id=scope_id, brief=brief)
        latest = runs.read_latest(venue)
        refresh_requested = venue in explicit_refresh_venues
        # Full input hashes remain the exact provenance of each real call, but
        # volatile market/company payloads no longer define the ordinary
        # scheduling epoch. A new scope or regional brief runs automatically;
        # a company wave must arrive through the explicit, venue-bound refresh
        # event emitted by the daemon.
        reuse_latest = not force and (
            _same_success(latest, input_signature)
            or (
                not refresh_requested
                and _same_refresh_success(latest, refresh_signature)
            )
        )
        if reuse_latest:
            exact_prepared = runs.read_prepared(scope_id)
            if _same_persisted_success(exact_prepared, latest):
                skipped.append({"venue": venue, "reason": "already_prepared"})
                continue
            latest_failure = _latest_failure(latest)
            repair_backoff = _failure_backoff(latest, retry_lineage=retry_lineage, now=now)
            if (
                latest_failure is not None
                and latest_failure.get("error_code") == "prepared_write_error"
                and repair_backoff is not None
            ):
                skipped.append(
                    {
                        "venue": venue,
                        "reason": "failure_backoff",
                        "attempt": repair_backoff["attempt"],
                        "delay_seconds": repair_backoff["delay_seconds"],
                        "next_retry_at": repair_backoff["next_retry_at"],
                    }
                )
                _log_retry_deferred(
                    log,
                    venue=venue,
                    scope_id=scope_id,
                    backoff=repair_backoff,
                )
                continue
            try:
                runs.write_prepared(scope_id, latest)
            except Exception as exc:  # noqa: BLE001 - projection failure is observable/retryable
                repair_error = {
                    **{key: value for key, value in latest.items() if key != "latest_failure"},
                    "as_of": now.isoformat(),
                    "status": "error",
                    "error_code": "prepared_write_error",
                    "error_message": str(exc)[:500],
                    "source_agent_run_id": latest.get("agent_run_id"),
                    "retry_lineage": retry_lineage,
                }
                _set_failure_retry_metadata(repair_error, latest=latest, now=now)
                runs.append(repair_error)
                _log_failure_retry(log, repair_error)
                errors.append({"venue": venue, "reason": "prepared_write_error"})
            else:
                prepared.append(
                    {
                        "venue": venue,
                        "candidate_scope_id": scope_id,
                        "agent_run_id": latest.get("agent_run_id"),
                        "repaired": True,
                    }
                )
            continue
        backoff = (
            None
            if force
            else _failure_backoff(latest, retry_lineage=retry_lineage, now=now)
        )
        if backoff is not None:
            skipped.append(
                {
                    "venue": venue,
                    "reason": "failure_backoff",
                    "attempt": backoff["attempt"],
                    "delay_seconds": backoff["delay_seconds"],
                    "next_retry_at": backoff["next_retry_at"],
                }
            )
            _log_retry_deferred(
                log,
                venue=venue,
                scope_id=scope_id,
                backoff=backoff,
            )
            continue

        # A successful activation may have happened while global/company
        # context was being prepared above. Never begin an automatic regional
        # call for that now-terminal scope.
        if _scope_already_activated(
            load_venue_state(state_path),
            venue=venue,
            candidate_scope_id=scope_id,
        ):
            skipped.append(
                {
                    "venue": venue,
                    "candidate_scope_id": scope_id,
                    "reason": "scope_already_activated",
                }
            )
            (log.info if venue in explicit_refresh_venues else log.debug)(
                "universe refresh skipped venue=%s scope=%s reason=scope_already_activated",
                venue,
                scope_id,
            )
            continue

        if agent is None:
            agent = _ToolLoopUniverseAgent(
                router=build_universe_router_from_env(),
                intelligence_store=companies,
            )
        started = time.monotonic()
        result = compose_universe(request, agent=agent)
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        record = _base_run_record(
            scope=scope,
            now=now,
            brief=brief,
            input_signature=input_signature,
        )
        record["retry_lineage"] = retry_lineage
        record["refresh_signature"] = refresh_signature
        record["refresh_reason"] = (
            "explicit_force"
            if force
            else "company_brief_wave"
            if refresh_requested
            else "scope_or_brief_changed"
        )
        record.update(
            {
                "agent_run_id": f"universe:{venue}:{now.isoformat()}:{uuid4().hex[:12]}",
                "status": result.status,
                "latency_ms": latency_ms,
                "fallback_used": False,
                "retrieval_status": request.retrieval_status,
                "retrieval_refs": list(request.retrieval_refs),
                "request_payload_hash": _request_signature(request_payload),
                "market_context_status": (
                    str(request.market_context.get("status") or "present")
                    if request.market_context
                    else "missing"
                ),
                "market_context": dict(request.market_context),
                "family_snapshot": dict(request.family_snapshot),
                "global_family_board": _family_board_observability(
                    global_family_board,
                    family_board_ref,
                ),
                "global_situation_digest": global_situation_ref,
                "global_universe_posture": global_universe_posture_ref,
                "situation_context_hash": _request_signature(situation_context.to_dict()),
                "situation_point_count": situation_context.point_count,
                "situation_truncated": situation_context.truncated,
                "coverage": dict(situation_context.coverage),
                "company_context_mode": company_context.mode,
                "company_context_hash": company_context_hash,
                "company_brief_refs_by_symbol": company_brief_refs,
                "company_context_coverage": dict(company_context.coverage),
                "agent_provider": result.agent_provider,
                "agent_model": result.agent_model,
                "agent_provider_fallback_reason": result.agent_provider_fallback_reason,
            }
        )
        if result.status == "success" and result.decision is not None:
            decision = result.decision
            selected = list(decision.selected_hotlist)
            record.update(
                {
                    "selected_hotlist": selected,
                    "summary": decision.summary,
                    "family_postures": dict(decision.family_postures),
                    "symbol_rationales": dict(decision.symbol_rationales),
                    "symbol_mandates": dict(decision.symbol_mandates),
                    "contract_version": decision.contract_version,
                    "selected_challengers": _selected_challengers(candidates, selected),
                    "add": [symbol for symbol in selected if symbol not in baseline],
                    "remove": [symbol for symbol in baseline if symbol not in selected],
                    "selected_with_company_context": [
                        symbol for symbol in selected if symbol in company_brief_refs
                    ],
                    "selected_without_company_context": [
                        symbol for symbol in selected if symbol not in company_brief_refs
                    ],
                }
            )
            try:
                prepared_mandate = _build_prepared_mandate(
                    request=request,
                    decision=decision,
                    record=record,
                    valid_until=brief.valid_until,
                )
                record["mandate_prepared_ref"] = mandates.write_prepared(prepared_mandate)
            except Exception as exc:  # noqa: BLE001 - selection remains usable without trader context
                record["mandate_prepared_error"] = {
                    "code": exc.__class__.__name__,
                    "message": str(exc)[:300],
                }
            try:
                # The reconstructible projection is written first: a canonical
                # success must never claim readiness when no activation file exists.
                runs.write_prepared(scope_id, record)
            except Exception as exc:  # noqa: BLE001 - explicit retryable run status
                record.update(
                    {
                        "status": "error",
                        "error_code": "prepared_write_error",
                        "error_message": str(exc)[:500],
                    }
                )
                _set_failure_retry_metadata(record, latest=latest, now=now)
                runs.append(record)
                _log_failure_retry(log, record)
                errors.append(
                    {
                        "venue": venue,
                        "reason": "prepared_write_error",
                        "agent_run_id": record["agent_run_id"],
                    }
                )
                continue
            runs.append(record)
            prepared.append(
                {
                    "venue": venue,
                    "candidate_scope_id": scope_id,
                    "agent_run_id": record["agent_run_id"],
                    "global_family_board_id": global_family_board.get("board_id"),
                }
            )
            continue

        record.update(
            {
                "validation_errors": list(result.validation_errors),
                "error_code": result.error_code,
                "error_message": result.error_message,
            }
        )
        _set_failure_retry_metadata(record, latest=latest, now=now)
        runs.append(record)
        _log_failure_retry(log, record)
        errors.append(
            {
                "venue": venue,
                "reason": result.error_code or result.status,
                "agent_run_id": record["agent_run_id"],
            }
        )

    if errors:
        log.warning("universe intelligence errors: %s", errors)
    return {"prepared": prepared, "waiting": waiting, "skipped": skipped, "errors": errors}


def _prepare_global_family_board(
    *,
    scopes: CandidateScopeStore,
    briefs: NewsMacroBriefStore,
    store: GlobalFamilyBoardStore,
    now: datetime,
    log: LoggerLike,
    global_situation_ref: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    board_scopes: dict[str, Mapping[str, Any]] = {}
    situations: dict[str, UniverseSituationContext] = {}
    for venue in VENUES:
        try:
            scope = scopes.read_latest(venue, scope_phase="preopen")
            if scope is None:
                current = scopes.read_current(venue)
                if current is not None and not str(current.get("scope_phase") or "").strip():
                    scope = current
        except Exception:  # noqa: BLE001 - board absence never blocks local preparation
            scope = None
        if scope is None:
            continue
        board_scopes[venue] = scope
        candidates = tuple(
            dict(item)
            for item in scope.get("candidates") or ()
            if isinstance(item, Mapping) and item.get("symbol")
        )
        brief = briefs.read_latest(venue, at=now)
        if brief is None:
            situations[venue] = UniverseSituationContext.not_available(
                candidate_count=len(candidates)
            )
            continue
        refs = brief.input_refs if isinstance(brief.input_refs, dict) else {}
        if refs.get("candidate_scope_id") != scope.get("candidate_scope_id"):
            situations[venue] = UniverseSituationContext.not_available(
                candidate_count=len(candidates)
            )
            continue
        situations[venue] = project_brief_to_universe_context(
            brief,
            venue=venue,
            candidate_symbols=(item["symbol"] for item in candidates),
            active_at=now,
            coverage_metadata=_coverage_metadata(refs.get("coverage")),
        )

    board = build_global_family_board(
        as_of=now.isoformat(),
        scopes=board_scopes,
        situations=situations,
        global_situation_digest_ref=global_situation_ref,
    )
    try:
        stored, ref, changed = store.append_if_changed(board)
    except Exception as exc:  # noqa: BLE001 - derived observability is fail-open
        log.warning("global family board persistence failed: %s", exc)
        return board, {
            "board_id": board.get("board_id"),
            "status": board.get("status"),
            "persistence_status": "error",
            "persistence_error": exc.__class__.__name__,
        }
    return stored, {
        **ref,
        "persistence_status": "appended" if changed else "unchanged",
    }


def _prepare_global_situation_digest(
    *,
    briefs: NewsMacroBriefStore,
    store: GlobalSituationDigestStore,
    now: datetime,
    log: LoggerLike,
) -> tuple[dict[str, Any], dict[str, Any]]:
    regional_briefs = {
        venue: brief
        for venue in VENUES
        if (brief := briefs.read_latest(venue, at=now)) is not None
    }
    if not regional_briefs:
        return {}, {"status": "unavailable", "persistence_status": "skipped"}

    global_brief = None
    try:
        global_brief = briefs.read_latest("GLOBAL", at=now)
    except Exception as exc:  # noqa: BLE001 - global brief is advisory, never blocks digest
        log.warning("global brief read failed: %s", exc)

    digest_as_of = max((brief.as_of for brief in regional_briefs.values()), default=now.isoformat())
    digest = {
        **build_global_situation_digest(
            regional_briefs,
            as_of=digest_as_of,
            global_brief=global_brief,
        ).to_dict(),
        "status": "complete" if len(regional_briefs) == len(VENUES) else "partial",
        "valid_until": min(
            (brief.valid_until for brief in regional_briefs.values() if brief.valid_until),
            default=None,
        ),
    }
    try:
        stored, ref, changed = store.append_if_changed(digest)
    except Exception as exc:  # noqa: BLE001 - digest observability must not block universe work
        log.warning("global situation digest persistence failed: %s", exc)
        return digest, {"status": "error", "persistence_error": exc.__class__.__name__}
    return stored, {**ref, "persistence_status": "appended" if changed else "unchanged"}


def _posture_cooldown_hours() -> float:
    raw = os.getenv("CASYS_POSTURE_REFRESH_COOLDOWN_HOURS")
    if raw is None:
        return DEFAULT_POSTURE_REFRESH_COOLDOWN_HOURS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_POSTURE_REFRESH_COOLDOWN_HOURS
    # value == 0 is a valid "disable cooldown" opt-in (pre-open refresh only),
    # symmetric with _refresh_cooldown_hours; only a negative typo falls back.
    return value if value >= 0 else DEFAULT_POSTURE_REFRESH_COOLDOWN_HOURS


def decide_global_posture_refresh(
    *,
    now: datetime,
    current: Mapping[str, Any] | None,
    preopen_now: Iterable[str],
    cooldown: timedelta,
    preopen_window: timedelta,
) -> tuple[bool, str]:
    """Decide whether the advisory global posture must be recomputed (LLM call).

    Pure and deterministic (``now`` injected). Stateless beyond the stored
    posture ``as_of``: because a refresh resets ``as_of`` to inside the current
    pre-open window, the pre-open branch fires at most once per window, and the
    cooldown branch at most once per ``cooldown`` otherwise.
    """
    if current is None:
        return True, "bootstrap"
    last = _parse_datetime(current.get("as_of"))
    if last is None:
        return True, "no_as_of"
    age = now - last
    preopen = sorted({str(venue).strip() for venue in preopen_now if str(venue).strip()})
    if preopen and age >= preopen_window:
        return True, "preopen:" + ",".join(preopen)
    if cooldown > timedelta(0) and age >= cooldown:
        return True, "cooldown"
    return False, "fresh"


def _global_posture_ref(current: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        "posture_id": str((current or {}).get("posture_id") or ""),
        "as_of": str((current or {}).get("as_of") or ""),
        "gross_mode": str((current or {}).get("gross_mode") or ""),
        "net_bias": str((current or {}).get("net_bias") or ""),
    }


def _global_posture_retry_lineage(current: Mapping[str, Any] | None) -> str:
    """Return the retry epoch for one successful posture generation.

    Board and digest inputs legitimately move while an LLM/provider is down.
    They must not create a fresh retry budget, so the lineage deliberately uses
    only the last successfully persisted posture (or the stable bootstrap
    epoch).  A newly persisted success clears the marker and changes the epoch.
    """

    posture_id = str((current or {}).get("posture_id") or "").strip()
    return f"global-posture:current:{posture_id}" if posture_id else "global-posture:bootstrap"


def _global_posture_retry_attempt(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _global_posture_retry_delay_seconds(attempt: int) -> int:
    bounded_attempt = min(_global_posture_retry_attempt(attempt), 5)
    minutes = min(
        DEFAULT_GLOBAL_POSTURE_FAILURE_BACKOFF_MINUTES * (2 ** (bounded_attempt - 1)),
        DEFAULT_GLOBAL_POSTURE_FAILURE_BACKOFF_MAX_MINUTES,
    )
    return minutes * 60


def _global_posture_failure_backoff(
    failure: Mapping[str, Any] | None,
    *,
    retry_lineage: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Return the active persisted retry gate for the current success epoch."""

    if not isinstance(failure, Mapping) or failure.get("retry_lineage") != retry_lineage:
        return None
    attempt = _global_posture_retry_attempt(failure.get("retry_attempt"))
    delay_seconds = _global_posture_retry_delay_seconds(attempt)
    retry_at = _parse_datetime(failure.get("next_retry_at"))
    if retry_at is None:
        failed_at = _parse_datetime(failure.get("as_of"))
        if failed_at is None:
            return None
        retry_at = failed_at + timedelta(seconds=delay_seconds)
    if now >= retry_at:
        return None
    return {
        "attempt": attempt,
        "delay_seconds": delay_seconds,
        "next_retry_at": retry_at.isoformat(),
        "error": str(failure.get("error_code") or failure.get("status") or "unknown"),
    }


def _global_posture_failure_record(
    *,
    current: Mapping[str, Any] | None,
    previous_failure: Mapping[str, Any] | None,
    now: datetime,
    refresh_reason: str | None,
    error: Exception,
) -> dict[str, Any]:
    retry_lineage = _global_posture_retry_lineage(current)
    same_lineage = (
        isinstance(previous_failure, Mapping)
        and previous_failure.get("retry_lineage") == retry_lineage
    )
    previous_attempt = _global_posture_retry_attempt(
        previous_failure.get("retry_attempt") if isinstance(previous_failure, Mapping) else None
    )
    attempt = previous_attempt + 1 if same_lineage else 1
    delay_seconds = _global_posture_retry_delay_seconds(attempt)
    return {
        "as_of": now.isoformat(),
        "status": "error",
        "error_code": error.__class__.__name__,
        "error_message": str(error)[:500],
        "refresh_reason": refresh_reason or "unknown",
        "current_posture_id": str((current or {}).get("posture_id") or ""),
        "retry_lineage": retry_lineage,
        "retry_attempt": attempt,
        "retry_delay_seconds": delay_seconds,
        "next_retry_at": (now + timedelta(seconds=delay_seconds)).isoformat(),
    }


def _log_global_posture_retry_scheduled(log: LoggerLike, failure: Mapping[str, Any]) -> None:
    log.warning(
        "global posture retry scheduled attempt=%s delay_s=%s next_at=%s error=%s",
        failure.get("retry_attempt"),
        failure.get("retry_delay_seconds"),
        failure.get("next_retry_at"),
        failure.get("error_code") or failure.get("status"),
    )


def _log_global_posture_retry_deferred(log: LoggerLike, backoff: Mapping[str, Any]) -> None:
    log.info(
        "global posture retry deferred attempt=%s delay_s=%s next_at=%s error=%s",
        backoff.get("attempt"),
        backoff.get("delay_seconds"),
        backoff.get("next_retry_at"),
        backoff.get("error"),
    )


def _prepare_global_universe_posture(
    *,
    scopes: CandidateScopeStore,
    store: GlobalUniversePostureStore,
    now: datetime,
    log: LoggerLike,
    global_family_board: Mapping[str, Any],
    global_situation_digest: Mapping[str, Any],
    agent: Any | None = None,
    preopen_now: Iterable[str] = (),
    preopen_window_minutes: int = 90,
    cooldown_hours: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # The whole body is fail-open (advisory posture): a store read failure must
    # only skip the posture, never abort the surrounding universe tick.
    current: dict[str, Any] | None = None
    reason: str | None = None
    try:
        current = store.read_current()
        cooldown = timedelta(
            hours=cooldown_hours if cooldown_hours is not None else _posture_cooldown_hours()
        )
        refresh, reason = decide_global_posture_refresh(
            now=now,
            current=current,
            preopen_now=preopen_now,
            cooldown=cooldown,
            preopen_window=timedelta(minutes=max(0, int(preopen_window_minutes))),
        )
        if not refresh and current is not None:
            log.info(
                "global universe posture reuse (reason=%s as_of=%s)",
                reason,
                current.get("as_of"),
            )
            return current, {
                "posture_id": str(current.get("posture_id") or ""),
                "as_of": str(current.get("as_of") or ""),
                "gross_mode": str(current.get("gross_mode") or ""),
                "net_bias": str(current.get("net_bias") or ""),
                "persistence_status": "reused",
                "refresh_reason": reason,
            }
        previous_failure = store.read_latest_failure()
        backoff = _global_posture_failure_backoff(
            previous_failure,
            retry_lineage=_global_posture_retry_lineage(current),
            now=now,
        )
        if backoff is not None:
            _log_global_posture_retry_deferred(log, backoff)
            return dict(current or {}), {
                **_global_posture_ref(current),
                "status": "deferred",
                "persistence_status": "reused" if current is not None else "not_available",
                "refresh_reason": "failure_backoff",
                "retry_attempt": backoff["attempt"],
                "retry_delay_seconds": backoff["delay_seconds"],
                "next_retry_at": backoff["next_retry_at"],
                "error": backoff["error"],
            }
        sticky: set[str] = set()
        for venue in VENUES:
            scope = scopes.read_latest(venue, scope_phase="preopen")
            if scope is None:
                scope_current = scopes.read_current(venue)
                if scope_current is not None and not str(scope_current.get("scope_phase") or "").strip():
                    scope = scope_current
            if scope is not None:
                sticky.update(
                    str(symbol).strip()
                    for symbol in scope.get("sticky_context_at_close") or ()
                    if str(symbol).strip()
                )
        request = GlobalUniversePostureRequest(
            as_of=now.isoformat(),
            global_family_board=global_family_board,
            global_situation_digest=global_situation_digest,
            sticky=tuple(sorted(sticky)),
            venues=VENUES,
        )
        posture = (agent or LlmGlobalPostureAgent()).compose(request)
        posture_dict = posture.to_dict()
        stored, ref, changed = store.append_if_changed(posture)
    except Exception as exc:  # noqa: BLE001 - global posture is advisory and fail-open
        failure: dict[str, Any] | None = None
        failure_persistence_status = "skipped"
        try:
            failure = _global_posture_failure_record(
                current=current,
                previous_failure=store.read_latest_failure(),
                now=now,
                refresh_reason=reason,
                error=exc,
            )
            store.append_failure(failure)
        except Exception as failure_exc:  # noqa: BLE001 - preserve fail-open even when state is down
            log.warning("global universe posture failure persistence failed: %s", failure_exc)
        else:
            failure_persistence_status = "failure_recorded"
            _log_global_posture_retry_scheduled(log, failure)
        log.warning("global universe posture preparation failed: %s", exc)
        return dict(current or {}), {
            **_global_posture_ref(current),
            "status": "error",
            "persistence_status": failure_persistence_status,
            "error": exc.__class__.__name__,
            **(
                {
                    "retry_attempt": failure["retry_attempt"],
                    "retry_delay_seconds": failure["retry_delay_seconds"],
                    "next_retry_at": failure["next_retry_at"],
                }
                if failure is not None and failure_persistence_status == "failure_recorded"
                else {}
            ),
        }
    return stored or posture_dict, {
        **ref,
        "persistence_status": "appended" if changed else "unchanged",
        "refresh_reason": reason,
    }


def _family_board_observability(
    board: Mapping[str, Any],
    ref: Mapping[str, Any],
) -> dict[str, Any]:
    coverage = board.get("coverage")
    return {
        "board_id": board.get("board_id"),
        "status": board.get("status"),
        "role": board.get("role"),
        "coverage": dict(coverage) if isinstance(coverage, Mapping) else {},
        "ref": dict(ref),
    }


class UniverseIntelligenceRunner:
    """Single worker with separate immediate and trailing-edge trigger batches.

    Scheduled daemon ticks stay immediate. Company-brief events use ``delay_s``
    and are merged by venue until the wave goes quiet; an intervening scheduled
    tick does not consume or shorten that delayed batch.
    """

    def __init__(
        self,
        *,
        tick_fn: Callable[..., dict[str, list[dict[str, Any]]]] = tick_universe_intelligence,
        stop_timeout_s: float = DEFAULT_ASYNC_STOP_TIMEOUT_S,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tick_fn = tick_fn
        self._stop_timeout_s = max(0.0, float(stop_timeout_s))
        self._monotonic = monotonic_fn
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready_kwargs: dict[str, Any] | None = None
        self._delayed_kwargs: dict[str, Any] | None = None
        self._delayed_deadline = 0.0
        self._stopping = False

    def trigger(self, *, delay_s: float = 0.0, **kwargs: Any) -> dict[str, Any]:
        if os.getenv("CASYS_UNIVERSE_INTELLIGENCE_ENABLED", "1") == "0":
            return {"triggered": False, "reason": "disabled"}
        delay = max(0.0, float(delay_s))
        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            if delay > 0:
                replaced_pending = self._delayed_kwargs is not None
                self._delayed_kwargs = _merge_trigger_kwargs(
                    self._delayed_kwargs,
                    kwargs,
                )
                # Trailing edge: each brief in the same wave extends the quiet
                # period, while immediate post-cycle work stays in its own slot.
                self._delayed_deadline = self._monotonic() + delay
                queue_kind = "debounced"
            else:
                replaced_pending = self._ready_kwargs is not None
                self._ready_kwargs = _merge_trigger_kwargs(self._ready_kwargs, kwargs)
                queue_kind = "immediate"
            self._wake_event.set()
            if self._thread is not None:
                logger = kwargs.get("logger") or _default_logger()
                logger.info(
                    "universe trigger coalesced pending_replaced=%s kind=%s venues=%s",
                    replaced_pending,
                    queue_kind,
                    tuple(kwargs.get("venues") or VENUES),
                )
                return {"triggered": False, "reason": "queued_latest"}
            thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="universe-intelligence",
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                raise
        return {"triggered": True, "_thread": thread}

    def _run(self) -> None:
        while True:
            kwargs: dict[str, Any] | None = None
            wait_s: float | None = None
            with self._lock:
                if self._stopping:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return
                now = self._monotonic()
                if self._delayed_kwargs is not None and now >= self._delayed_deadline:
                    kwargs = self._delayed_kwargs
                    self._delayed_kwargs = None
                    self._delayed_deadline = 0.0
                elif self._ready_kwargs is not None:
                    kwargs = self._ready_kwargs
                    self._ready_kwargs = None
                elif self._delayed_kwargs is not None:
                    wait_s = max(0.0, self._delayed_deadline - now)
                    self._wake_event.clear()
                else:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return

            if kwargs is None:
                self._wake_event.wait(timeout=wait_s)
                continue
            logger = kwargs.get("logger") or _default_logger()
            try:
                self._tick_fn(**kwargs, stop_requested=self._stop_event.is_set)
            except Exception as exc:  # noqa: BLE001 - background work is best-effort
                logger.warning("universe intelligence background failure: %s", exc)

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._ready_kwargs = None
            self._delayed_kwargs = None
            self._delayed_deadline = 0.0
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._stop_timeout_s)


def trigger_company_brief_refresh(
    event: Mapping[str, Any],
    *,
    runner: UniverseIntelligenceRunner,
    config_dir: Path,
    state_dir: Path,
    loop_now: datetime,
    logger: LoggerLike | None = None,
    delay_s: float = DEFAULT_COMPANY_BRIEF_DEBOUNCE_SECONDS,
) -> dict[str, Any]:
    """Route one written company brief to its regional trailing-edge batch."""

    log = logger or _default_logger()
    symbol = str(event.get("symbol") or "").strip()
    if not symbol:
        log.warning("company brief universe refresh ignored: symbol missing")
        return {"triggered": False, "reason": "symbol_missing"}
    venue = venue_of(symbol)
    if venue not in VENUES:
        log.info(
            "company brief universe refresh ignored symbol=%s venue=%s",
            symbol,
            venue,
        )
        return {"triggered": False, "reason": "unsupported_venue", "venue": venue}
    return runner.trigger(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=loop_now,
        logger=log,
        venues=(venue,),
        refresh_venues=(venue,),
        delay_s=delay_s,
    )


def _merge_trigger_kwargs(
    current: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep latest runtime context while unioning every affected venue."""

    if current is None:
        return dict(incoming)
    merged = {**dict(current), **dict(incoming)}
    if "venues" in current or "venues" in incoming:
        merged["venues"] = _ordered_venue_union(
            current.get("venues", VENUES),
            incoming.get("venues", VENUES),
        )
    if "refresh_venues" in current or "refresh_venues" in incoming:
        merged["refresh_venues"] = _ordered_venue_union(
            current.get("refresh_venues", ()),
            incoming.get("refresh_venues", ()),
        )
    if "force" in current or "force" in incoming:
        merged["force"] = bool(current.get("force")) or bool(incoming.get("force"))
    return merged


def _ordered_venue_union(*raw_groups: Any) -> tuple[str, ...]:
    values: set[str] = set()
    for raw in raw_groups:
        items = (raw,) if isinstance(raw, str) else raw or ()
        values.update(str(item).strip().upper() for item in items if str(item).strip())
    return tuple(
        [venue for venue in VENUES if venue in values]
        + sorted(values.difference(VENUES))
    )


def _record_waiting_once(
    runs: UniverseRunStore,
    *,
    scope: Mapping[str, Any],
    now: datetime,
    reason: str,
    waiting: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    brief_ref: Mapping[str, str] | None = None,
) -> None:
    venue = str(scope.get("venue") or "").strip()
    scope_id = str(scope.get("candidate_scope_id") or "").strip()
    latest = runs.read_latest(venue)
    if (
        latest is not None
        and latest.get("candidate_scope_id") == scope_id
        and latest.get("status") == "waiting_brief"
        and latest.get("error_code") == reason
    ):
        skipped.append({"venue": venue, "reason": f"{reason}_already_recorded"})
        return
    record = _base_run_record(
        scope=scope,
        now=now,
        brief=None,
        input_signature=None,
    )
    record.update(
        {
            "status": "waiting_brief",
            "error_code": reason,
            "brief_ref": dict(brief_ref) if brief_ref else None,
            "fallback_used": False,
            "retrieval_status": "not_enabled",
            "retrieval_refs": [],
        }
    )
    runs.append(record)
    waiting.append({"venue": venue, "reason": reason})


def _base_run_record(
    *,
    scope: Mapping[str, Any],
    now: datetime,
    brief: Any,
    input_signature: str | None,
) -> dict[str, Any]:
    brief_ref = brief.ref(date=brief.as_of[:10]) if brief is not None else None
    venue = str(scope.get("venue") or "")
    scope_id = str(scope.get("candidate_scope_id") or "")
    brief_id = str(getattr(brief, "brief_id", "") or "")
    return {
        "schema_version": 1,
        "candidate_scope_id": scope_id,
        "candidate_run_ids": list(scope.get("candidate_run_ids") or []),
        "venue": venue,
        "as_of": now.isoformat(),
        "scope_as_of": scope.get("as_of"),
        "scope_phase": scope.get("scope_phase") or "legacy",
        "parent_candidate_scope_id": scope.get("parent_candidate_scope_id"),
        "brief_ref": brief_ref,
        "brief_status": "active" if brief is not None else "missing",
        "valid_until": brief.valid_until if brief is not None else None,
        "input_signature": input_signature,
        "refresh_signature": (
            _universe_refresh_signature(
                venue=venue,
                scope_id=scope_id,
                brief_id=brief_id,
            )
            if brief_id
            else None
        ),
        "candidate_count": len(scope.get("candidates") or []),
        "baseline": list(scope.get("default_hotlist") or []),
        "sticky_context": list(scope.get("sticky_context_at_close") or []),
    }


def _coverage_metadata(raw: Any) -> dict[str, Any]:
    coverage = dict(raw) if isinstance(raw, Mapping) else {}
    stale_labels = coverage.get("macro_series_stale_labels")
    macro_count = int(coverage.get("macro_series_count") or 0)
    return {
        "candidate_count": coverage.get("candidate_count"),
        "candidates_with_news": coverage.get("candidates_with_news"),
        "news_items_injected": coverage.get("news_items_injected"),
        "news_cap_hit": coverage.get("news_cap_reached"),
        "global_headlines": coverage.get("global_headlines_status"),
        "macro_series": (
            "stale"
            if isinstance(stale_labels, list) and stale_labels
            else "present" if macro_count else "missing"
        ),
    }


def _company_context_mode(config_dir: Path) -> str:
    if os.getenv("CASYS_UNIVERSE_COMPANY_CONTEXT_ENABLED", "1") == "0":
        return "observe"
    try:
        payload = yaml.safe_load((config_dir / "company_intelligence.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        payload = {}
    mode = str(payload.get("universe_company_context_mode") or "active").strip().lower()
    return mode if mode in {"observe", "active"} else "observe"


def _load_market_context(path: Path, *, now: datetime) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("regime_families"), Mapping):
        context = dict(payload)
    elif isinstance(payload.get("family_bias"), Mapping):
        context = {
            "schema_version": 0,
            "status": "legacy_family_bias",
            "regime_families": dict(payload["family_bias"]),
        }
    else:
        context = {
            "schema_version": 0,
            "status": "legacy_unversioned",
            "regime_families": dict(payload),
        }
    as_of = _parse_datetime(context.get("as_of"))
    if as_of is not None and now - as_of > timedelta(hours=DEFAULT_REGIME_MAX_AGE_HOURS):
        return {
            "schema_version": context.get("schema_version"),
            "status": "stale",
            "as_of": context.get("as_of"),
            "coverage": context.get("coverage") or {},
            "regime_families": {},
        }
    if not context.get("status"):
        coverage = context.get("coverage")
        coverage_status = coverage.get("status") if isinstance(coverage, Mapping) else None
        context["status"] = coverage_status or (
            "partial" if context.get("regime_families") else "missing"
        )
    return context


def _request_signature(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scope_already_activated(
    state: Mapping[str, Any],
    *,
    venue: str,
    candidate_scope_id: str,
) -> bool:
    """Match the exact terminal activation recorded by market rotation.

    ``last_universe_activation_scope_id`` is written only for a successful
    prepared activation. A fallback attempt intentionally remains eligible for
    a later preparation inside the same pre-open window.
    """

    venues = state.get("venues") if isinstance(state, Mapping) else None
    venue_state = venues.get(venue) if isinstance(venues, Mapping) else None
    return bool(
        candidate_scope_id
        and isinstance(venue_state, Mapping)
        and str(venue_state.get("last_universe_activation_scope_id") or "").strip()
        == candidate_scope_id
    )


def _universe_refresh_signature(*, venue: str, scope_id: str, brief_id: str) -> str:
    """Stable scheduling epoch, separate from exact consumed-input provenance."""

    return _request_signature(
        {
            "venue": str(venue).strip().upper(),
            "candidate_scope_id": str(scope_id).strip(),
            "brief_id": str(brief_id).strip(),
        }
    )


def _stored_refresh_signature(raw: Any) -> str | None:
    """Read the new key or derive it from legacy success records."""

    if not isinstance(raw, Mapping):
        return None
    explicit = str(raw.get("refresh_signature") or "").strip()
    if explicit:
        return explicit
    brief_ref = raw.get("brief_ref")
    brief_id = (
        str(brief_ref.get("brief_id") or "").strip()
        if isinstance(brief_ref, Mapping)
        else ""
    )
    venue = str(raw.get("venue") or "").strip()
    scope_id = str(raw.get("candidate_scope_id") or "").strip()
    if not venue or not scope_id or not brief_id:
        return None
    return _universe_refresh_signature(
        venue=venue,
        scope_id=scope_id,
        brief_id=brief_id,
    )


def _same_refresh_success(raw: Any, refresh_signature: str) -> bool:
    return bool(
        isinstance(raw, Mapping)
        and raw.get("status") == "success"
        and _stored_refresh_signature(raw) == refresh_signature
    )


def _same_persisted_success(prepared: Any, latest: Any) -> bool:
    """Whether the exact prepared projection already points to latest success."""

    if not isinstance(prepared, Mapping) or not isinstance(latest, Mapping):
        return False
    if prepared.get("status") != "success" or latest.get("status") != "success":
        return False
    if prepared.get("candidate_scope_id") != latest.get("candidate_scope_id"):
        return False
    latest_run_id = str(latest.get("agent_run_id") or "").strip()
    if latest_run_id:
        return str(prepared.get("agent_run_id") or "").strip() == latest_run_id
    return bool(
        latest.get("input_signature")
        and prepared.get("input_signature") == latest.get("input_signature")
    )


_NON_MATERIAL_TEMPORAL_FIELDS = frozenset(
    {
        "as_of",
        "valid_until",
        "created_at",
        "updated_at",
        "generated_at",
        "refreshed_at",
    }
)


def _material_request_signature(payload: Any) -> str:
    """Hash decision evidence while excluding temporal freshness metadata only.

    All non-temporal fields, including coverage statuses, source references,
    candidate scores, and context IDs, remain part of the key.  A genuine
    context change therefore resets the retry sequence without timestamps
    turning every otherwise identical daemon tick into a new request.
    """

    return _request_signature(_without_non_material_timestamps(payload))


def _without_non_material_timestamps(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        return {
            str(key): _without_non_material_timestamps(value)
            for key, value in payload.items()
            if str(key) not in _NON_MATERIAL_TEMPORAL_FIELDS
        }
    if isinstance(payload, list):
        return [_without_non_material_timestamps(value) for value in payload]
    if isinstance(payload, tuple):
        return tuple(_without_non_material_timestamps(value) for value in payload)
    return payload


def _same_success(raw: Any, input_signature: str) -> bool:
    return bool(
        isinstance(raw, Mapping)
        and raw.get("input_signature") == input_signature
        and raw.get("status") == "success"
    )


def _failure_backoff(raw: Any, *, retry_lineage: str, now: datetime) -> dict[str, Any] | None:
    """Return persisted retry state for the same scope-and-brief retry lineage."""

    return _shared_failure_backoff(
        raw,
        retry_lineage=retry_lineage,
        now=now,
        base_minutes=DEFAULT_FAILURE_BACKOFF_MINUTES,
        max_minutes=DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
        prepared_write_base_minutes=DEFAULT_PREPARED_WRITE_BACKOFF_MINUTES,
        prepared_write_max_minutes=DEFAULT_PREPARED_WRITE_BACKOFF_MAX_MINUTES,
    )


def _failure_delay_seconds(attempt: int, *, error_code: Any) -> int:
    return _shared_failure_delay_seconds(
        attempt,
        error_code=error_code,
        base_minutes=DEFAULT_FAILURE_BACKOFF_MINUTES,
        max_minutes=DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
        prepared_write_base_minutes=DEFAULT_PREPARED_WRITE_BACKOFF_MINUTES,
        prepared_write_max_minutes=DEFAULT_PREPARED_WRITE_BACKOFF_MAX_MINUTES,
    )


def _set_failure_retry_metadata(
    record: dict[str, Any], *, latest: Mapping[str, Any] | None, now: datetime
) -> None:
    """Persist the bounded exponential retry schedule alongside an error run."""

    previous = _latest_failure(latest)
    same_retry_lineage = (
        previous is not None and previous.get("retry_lineage") == record.get("retry_lineage")
    )
    attempt = max(1, int(previous.get("retry_attempt") or 1) + 1) if same_retry_lineage else 1
    delay_seconds = _failure_delay_seconds(attempt, error_code=record.get("error_code"))
    record.update(
        {
            "retry_attempt": attempt,
            "retry_delay_seconds": delay_seconds,
            "next_retry_at": (now + timedelta(seconds=delay_seconds)).isoformat(),
        }
    )


def _log_failure_retry(log: LoggerLike, record: Mapping[str, Any]) -> None:
    log.warning(
        "universe retry scheduled venue=%s scope=%s attempt=%s delay_s=%s next_at=%s error=%s",
        record.get("venue"),
        record.get("candidate_scope_id"),
        record.get("retry_attempt"),
        record.get("retry_delay_seconds"),
        record.get("next_retry_at"),
        record.get("error_code") or record.get("status"),
    )


def _log_retry_deferred(
    log: LoggerLike,
    *,
    venue: str,
    scope_id: str,
    backoff: Mapping[str, Any],
) -> None:
    log.info(
        "universe retry deferred venue=%s scope=%s attempt=%s delay_s=%s next_at=%s error=%s",
        venue,
        scope_id,
        backoff.get("attempt"),
        backoff.get("delay_seconds"),
        backoff.get("next_retry_at"),
        backoff.get("error"),
    )


def _retry_lineage_key(*, venue: str, scope_id: str, brief: Any) -> str:
    """Stable retry epoch, intentionally independent of fast-changing contexts.

    Company-card and market-context refreshes may legitimately alter the
    material input while a run is failing.  They must invalidate a success but
    not reset the failure budget: only a new candidate scope or macro brief
    starts a new retry lineage.
    """

    brief_id = str(getattr(brief, "brief_id", "") or "").strip()
    return _request_signature({"venue": venue, "candidate_scope_id": scope_id, "brief_id": brief_id})


def _selected_challengers(candidates: tuple[dict[str, Any], ...], selected: list[str]) -> list[str]:
    selected_set = set(selected)
    return [
        str(candidate["symbol"])
        for candidate in candidates
        if candidate.get("symbol") in selected_set and candidate.get("fresh_news")
    ]


def _build_prepared_mandate(
    *,
    request: Any,
    decision: Any,
    record: Mapping[str, Any],
    valid_until: str | None,
) -> UniverseMandate:
    family_by_symbol = {
        str(candidate.get("symbol") or ""): str(candidate.get("family") or "")
        for candidate in request.candidates
    }
    symbols: dict[str, SymbolMandate] = {}
    for symbol in decision.selected_hotlist:
        supplied = decision.symbol_mandates.get(symbol) if isinstance(decision.symbol_mandates, Mapping) else None
        raw = dict(supplied) if isinstance(supplied, Mapping) else {}
        family = family_by_symbol.get(symbol, "")
        company_context = request.company_context.symbols.get(symbol)
        company_entry = dict(company_context) if isinstance(company_context, Mapping) else {}
        raw.update(
            {
                "why_selected": raw.get("why_selected") or decision.symbol_rationales.get(symbol, ""),
                "family_context": {
                    "family": family,
                    "posture": (
                        decision.family_postures.get(family, "")
                        if isinstance(decision.family_postures, Mapping)
                        else ""
                    ),
                },
                "company_context": company_entry,
                "company_brief_ref": company_entry.get("brief_ref") or {},
                "confidence": (company_entry.get("selection_view") or {}).get("confidence", "unknown")
                if isinstance(company_entry.get("selection_view"), Mapping)
                else "unknown",
            }
        )
        symbols[symbol] = SymbolMandate.from_mapping(symbol, raw)
    agent_run_id = str(record.get("agent_run_id") or "")
    return UniverseMandate(
        mandate_id=f"universe-mandate:v1:{_request_signature([request.candidate_scope_id, agent_run_id])}",
        candidate_scope_id=request.candidate_scope_id,
        venue=request.venue,
        agent_run_id=agent_run_id,
        as_of=str(record.get("as_of") or request.as_of),
        valid_until=valid_until,
        status="prepared",
        symbols=symbols,
        family_postures=(
            dict(decision.family_postures)
            if isinstance(decision.family_postures, Mapping)
            else {}
        ),
        portfolio_posture=(
            dict(getattr(decision, "portfolio_posture", None))
            if isinstance(getattr(decision, "portfolio_posture", None), Mapping)
            else None
        ),
    )



