"""Asynchronous preparation of per-venue universe-agent selections."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
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
    DEFAULT_UNIVERSE_AGENT_TIMEOUT_S,
    build_universe_router_from_env,
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
from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.global_family_board_store import GlobalFamilyBoardStore
from trader.infrastructure.state_db.global_situation_digest_store import GlobalSituationDigestStore
from trader.infrastructure.state_db.global_universe_posture_store import GlobalUniversePostureStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.infrastructure.state_db.universe_run_store import UniverseRunStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore
from trader.runtime.protocols import LoggerLike
from trader.runtime.company_context_config import load_company_context_projection_limits

VENUES = ("TW", "EU", "US")
DEFAULT_FAILURE_BACKOFF_MINUTES = 30
DEFAULT_PREPARED_WRITE_BACKOFF_MINUTES = 1
DEFAULT_REGIME_MAX_AGE_HOURS = 96
DEFAULT_ASYNC_STOP_TIMEOUT_S = 1.0
# Cadence of the advisory global universe posture: at most one LLM recompute per
# pre-open window (freshness before each venue gong) and one per cooldown
# otherwise. Anything more is wasted tokens (the posture barely moves intraday).
DEFAULT_POSTURE_REFRESH_COOLDOWN_HOURS = 4.0


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
            timeout_s=DEFAULT_UNIVERSE_AGENT_TIMEOUT_S,
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
    # ``<root>/config`` (cf. load_radar_params ci-dessus) ; load_sessions attend
    # la racine et rajoute lui-même ``config/sessions.yaml``.
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
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw_venue in venues:
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
            companies.read_current_many(candidate_symbols),
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

        input_signature = _request_signature(request.to_dict())
        latest = runs.read_latest(venue)
        if _same_success(latest, input_signature):
            exact_prepared = runs.read_prepared(scope_id)
            if _same_success(exact_prepared, input_signature):
                skipped.append({"venue": venue, "reason": "already_prepared"})
                continue
            try:
                runs.write_prepared(scope_id, latest)
            except Exception as exc:  # noqa: BLE001 - projection failure is observable/retryable
                repair_error = {
                    **latest,
                    "as_of": now.isoformat(),
                    "status": "error",
                    "error_code": "prepared_write_error",
                    "error_message": str(exc)[:500],
                    "source_agent_run_id": latest.get("agent_run_id"),
                }
                runs.append(repair_error)
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
        if _failure_backoff_active(latest, input_signature=input_signature, now=now):
            skipped.append({"venue": venue, "reason": "failure_backoff"})
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
        record.update(
            {
                "agent_run_id": f"universe:{venue}:{now.isoformat()}:{uuid4().hex[:12]}",
                "status": result.status,
                "latency_ms": latency_ms,
                "fallback_used": False,
                "retrieval_status": request.retrieval_status,
                "retrieval_refs": list(request.retrieval_refs),
                "request_payload_hash": _request_signature(request.to_dict()),
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
                runs.append(record)
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
        runs.append(record)
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
        sticky: set[str] = set()
        for venue in VENUES:
            scope = scopes.read_latest(venue, scope_phase="preopen")
            if scope is None:
                current = scopes.read_current(venue)
                if current is not None and not str(current.get("scope_phase") or "").strip():
                    scope = current
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
        log.warning("global universe posture preparation failed: %s", exc)
        return {}, {
            "status": "error",
            "persistence_status": "skipped",
            "error": exc.__class__.__name__,
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
    """Coalescing single-worker runner; triggers received in-flight are not lost."""

    def __init__(
        self,
        *,
        tick_fn: Callable[..., dict[str, list[dict[str, Any]]]] = tick_universe_intelligence,
        stop_timeout_s: float = DEFAULT_ASYNC_STOP_TIMEOUT_S,
    ) -> None:
        self._tick_fn = tick_fn
        self._stop_timeout_s = max(0.0, float(stop_timeout_s))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_kwargs: dict[str, Any] | None = None
        self._stopping = False

    def trigger(self, **kwargs: Any) -> dict[str, Any]:
        if os.getenv("CASYS_UNIVERSE_INTELLIGENCE_ENABLED", "1") == "0":
            return {"triggered": False, "reason": "disabled"}
        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            if self._thread is not None:
                self._pending_kwargs = dict(kwargs)
                return {"triggered": False, "reason": "queued_latest"}
            thread = threading.Thread(
                target=self._run,
                kwargs={"initial_kwargs": dict(kwargs)},
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

    def _run(self, *, initial_kwargs: dict[str, Any]) -> None:
        kwargs = initial_kwargs
        while True:
            logger = kwargs.get("logger") or _default_logger()
            try:
                self._tick_fn(**kwargs, stop_requested=self._stop_event.is_set)
            except Exception as exc:  # noqa: BLE001 - background work is best-effort
                logger.warning("universe intelligence background failure: %s", exc)
            with self._lock:
                if self._stopping or self._pending_kwargs is None:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return
                kwargs = self._pending_kwargs
                self._pending_kwargs = None

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._pending_kwargs = None
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._stop_timeout_s)


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
    return {
        "schema_version": 1,
        "candidate_scope_id": str(scope.get("candidate_scope_id") or ""),
        "candidate_run_ids": list(scope.get("candidate_run_ids") or []),
        "venue": str(scope.get("venue") or ""),
        "as_of": now.isoformat(),
        "scope_as_of": scope.get("as_of"),
        "scope_phase": scope.get("scope_phase") or "legacy",
        "parent_candidate_scope_id": scope.get("parent_candidate_scope_id"),
        "brief_ref": brief_ref,
        "brief_status": "active" if brief is not None else "missing",
        "valid_until": brief.valid_until if brief is not None else None,
        "input_signature": input_signature,
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


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _request_signature(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_success(raw: Any, input_signature: str) -> bool:
    return bool(
        isinstance(raw, Mapping)
        and raw.get("input_signature") == input_signature
        and raw.get("status") == "success"
    )


def _failure_backoff_active(raw: Any, *, input_signature: str, now: datetime) -> bool:
    if not isinstance(raw, Mapping) or raw.get("input_signature") != input_signature:
        return False
    if raw.get("status") not in {"invalid", "error"}:
        return False
    try:
        failed_at = datetime.fromisoformat(str(raw.get("as_of") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=timezone.utc)
    backoff_minutes = (
        DEFAULT_PREPARED_WRITE_BACKOFF_MINUTES
        if raw.get("error_code") == "prepared_write_error"
        else DEFAULT_FAILURE_BACKOFF_MINUTES
    )
    return now - failed_at < timedelta(minutes=backoff_minutes)


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


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
