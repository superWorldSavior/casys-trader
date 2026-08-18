"""Runtime adapter for macro/news analyst ticks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from trader.application.analyst import NewsMacroAnalysisRequest, run_news_macro_analysis
from trader.domain.semantic.catalog import family_for_symbol
from trader.domain.universe import candidate_scope_id, project_company_briefs_to_universe_context
from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore
from trader.market import macro_calendar
from trader.market.rotation.wiring import venue_of
from trader.runtime.protocols import LoggerLike
from trader.runtime._retry_policy import (
    DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
    DEFAULT_FAILURE_BACKOFF_MINUTES,
    ensure_utc as _ensure_utc,
    failure_backoff as _shared_failure_backoff,
    failure_delay_seconds as _shared_failure_delay_seconds,
    latest_failure as _shared_latest_failure,
    parse_datetime as _parse_datetime,
    positive_int as _positive_int,
)

VENUES = ("TW", "EU", "US")
DEFAULT_LOOKBACK_DAYS = 1
DEFAULT_VALID_HOURS = 20
# A failed macro report must never turn a four-minute daemon cadence into a
# four-minute model retry.  Keep this in step with the universe-composition
# retry ladder: 30 / 60 / 120 / 240 / 360 minutes (see ``_retry_policy``).
# Kept as a compatibility alias for callers which used the former fixed
# six-hour guard.  Runtime retry decisions use the minute constants above.
DEFAULT_FAILURE_BACKOFF_HOURS = DEFAULT_FAILURE_BACKOFF_MAX_MINUTES // 60
DEFAULT_SUCCESS_REFRESH_COOLDOWN_HOURS = 4
DEFAULT_MAX_VENUE_NEWS_ITEMS = 80
DEFAULT_MAX_GLOBAL_NEWS_ITEMS = 120
DEFAULT_MAX_MACRO_SERIES = 30
DEFAULT_MACRO_SERIES_STALE_DAYS = 90
DEFAULT_ASYNC_STOP_TIMEOUT_S = 1.0
DEFAULT_MAX_GDELT_EVENTS = 60


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def tick_news_macro_analysis(
    *,
    config_dir: Path,
    state_dir: Path,
    loop_now: datetime,
    logger: LoggerLike | None = None,
    analyst: object | None = None,
    brief_store: NewsMacroBriefStore | None = None,
    situation_store: SituationMemoryStore | None = None,
    company_store: CompanyIntelligenceStore | None = None,
    scope_store: CandidateScopeStore | None = None,
    venues: Iterable[str] = VENUES,
    include_global: bool | None = None,
    force: bool = False,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, list[dict]]:
    """Run macro/news analyst briefs, best-effort and never blocking trading.

    ``include_global`` keeps the historical environment-driven behaviour when
    omitted. Manual callers can set it explicitly to target regional venues
    without also launching the GLOBAL pass. ``force`` bypasses only freshness,
    cooldown and failure-backoff gates; input and scope validation still apply.
    """

    del config_dir  # reserved for future source config; keep signature aligned with rotation tick
    log = logger or _default_logger()
    if os.getenv("CASYS_NEWS_MACRO_ANALYST_ENABLED", "1") == "0":
        return {"triggered": [], "skipped": [{"reason": "disabled"}], "errors": []}

    now = _ensure_utc(loop_now)
    store = brief_store or NewsMacroBriefStore(Path(state_dir) / "news_briefs")
    memory = situation_store
    scopes = scope_store or CandidateScopeStore(Path(state_dir) / "candidate_scopes")
    companies = company_store or CompanyIntelligenceStore(Path(state_dir) / "company_intelligence")
    status_path = Path(state_dir) / "news_macro_analysis_status.json"
    status = _load_status(status_path)
    state_path = Path(state_dir)
    news_items = _read_recent_news_items(state_path / "news_items", now=now)
    global_news_items = _read_recent_global_news_items(state_path, now=now)
    macro_next = tuple(macro_calendar.macro_next(now, macro_calendar.load_calendar(state_path / "macro_calendar.json")))
    macro_series = tuple(_read_macro_series_snapshot(state_path / "macro_series"))

    triggered: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    status_dirty = False

    for venue in venues:
        if stop_requested is not None and stop_requested():
            skipped.append({"reason": "stopping"})
            break
        venue = str(venue)
        venue_scope = _candidate_scope_for_venue(state_path, venue, scope_store=scopes)
        candidate_records = venue_scope["candidates"]
        candidate_symbols = tuple(_candidate_symbols(candidate_records))
        scope_id = str(venue_scope.get("candidate_scope_id") or "").strip()
        scope_error = str(venue_scope.get("error") or "").strip()
        if scope_error or not scope_id or not candidate_symbols:
            reason = scope_error or "candidate_scope_missing"
            skipped.append({"venue": venue, "reason": reason})
            if scope_error:
                log.warning("news macro analyst scope unavailable for %s: %s", venue, scope_error)
            continue
        if str(venue_scope.get("scope_phase") or "").strip().lower() == "close":
            skipped.append({"venue": venue, "reason": "awaiting_preopen_scope"})
            continue
        venue_news = tuple(
            _filter_news_for_venue(
                news_items,
                venue=venue,
                candidate_symbols=candidate_symbols,
                candidate_records=candidate_records,
            )
        )
        venue_global_news = tuple(_filter_global_news_for_venue(global_news_items, venue=venue))
        family_context = _family_context(
            candidate_symbols=candidate_symbols,
            news_items=venue_news,
            global_news_items=venue_global_news,
        )
        company_anchor_symbols = _company_anchor_symbols(
            candidate_records=candidate_records,
            venue_news=venue_news,
        )
        company_context = project_company_briefs_to_universe_context(
            companies.read_current_many(company_anchor_symbols, depth="preferred"),
            candidate_symbols=company_anchor_symbols,
            active_at=now,
            mode="active",
        )
        company_anchors = _company_news_anchors(company_context.to_dict().get("symbols") or {})
        company_brief_refs = {
            symbol: dict(anchor["brief_ref"])
            for symbol, anchor in company_anchors.items()
            if isinstance(anchor.get("brief_ref"), Mapping)
        }
        if not venue_news and not venue_global_news and not macro_next and not macro_series:
            skipped.append({"venue": venue, "reason": "no_inputs"})
            continue

        input_refs = {
            "news_item_uuids": _news_uuids(venue_news),
            "news_item_count": len(venue_news),
            "global_news_uuids": _news_uuids(venue_global_news),
            "global_news_count": len(venue_global_news),
            "macro_series_labels": [str(item.get("label")) for item in macro_series if item.get("label")],
            "families": sorted(family_context),
            "candidate_symbols": list(candidate_symbols),
            "candidate_scope_id": scope_id,
            "candidate_scope_as_of": venue_scope.get("as_of"),
            "candidate_scope_phase": venue_scope.get("scope_phase") or "legacy",
            "parent_candidate_scope_id": venue_scope.get("parent_candidate_scope_id"),
            "coverage": _coverage_metadata(
                state_path=state_path,
                now=now,
                candidate_symbols=candidate_symbols,
                venue_news=venue_news,
                global_news_items=venue_global_news,
                macro_series=macro_series,
            ),
            "company_brief_refs": company_brief_refs,
        }
        success_signature = _input_signature(
            venue=venue,
            macro_next=macro_next,
            macro_series=macro_series,
            input_refs=input_refs,
        )
        retry_lineage = _regional_retry_lineage_key(
            venue=venue,
            candidate_scope_id=scope_id,
        )
        active_brief = store.read_latest(venue, at=now)
        venue_status = status.get(venue)
        active_success_matches = (
            not force
            and _brief_matches_candidate_scope(active_brief, scope_id)
            and _last_success_matches_signature(venue_status, success_signature)
        )
        if active_success_matches:
            skipped.append({"venue": venue, "reason": "active_brief_same_scope_and_inputs"})
            continue
        if not force:
            backoff = _failure_backoff(
                venue_status,
                retry_lineage=retry_lineage,
                now=now,
            )
            if backoff is not None:
                skipped.append(_failure_backoff_skip(venue=venue, backoff=backoff))
                _log_retry_deferred(
                    log,
                    venue=venue,
                    scope_id=scope_id,
                    backoff=backoff,
                )
                continue
        if not force and _brief_matches_candidate_scope(active_brief, scope_id):
            # Company briefs remain material inputs (and their exact refs stay
            # in ``input_refs``), but they must not become an out-of-band
            # scheduler for the regional macro analyst.  The next scheduled
            # refresh will consume the newest refs; only an explicit force or
            # a new candidate scope may bypass this success cooldown.
            if _success_refresh_cooldown_active(venue_status, now=now):
                skipped.append(
                    {"venue": venue, "reason": "active_brief_changed_inputs_cooldown"}
                )
                continue
        try:
            situation_feedback = _situation_feedback_for_venue(
                state_path, venue, store=memory
            )
        except Exception:  # noqa: BLE001 - digest must never block an analysis
            situation_feedback = {}
        request = NewsMacroAnalysisRequest(
            as_of=now.isoformat(),
            valid_until=(now + timedelta(hours=DEFAULT_VALID_HOURS)).isoformat(),
            venue=venue,
            news_items=venue_news,
            global_news_items=venue_global_news,
            macro_next=macro_next,
            macro_series=macro_series,
            candidate_symbols=candidate_symbols,
            family_context=family_context,
            company_anchors=company_anchors,
            input_refs=input_refs,
            situation_feedback=situation_feedback or None,
        )
        if analyst is None:
            from trader.agent.news_macro import LlmNewsMacroAnalyst

            analyst = LlmNewsMacroAnalyst()
        if memory is None:
            memory = SituationMemoryStore(Path(state_dir) / "situation_memory.db")
        result = run_news_macro_analysis(
            request,
            analyst=analyst,  # type: ignore[arg-type]
            repository=store,
            situation_repository=memory,
        )
        if result.written:
            status[venue] = _success_status(
                existing=venue_status,
                now=now,
                success_signature=success_signature,
                brief_ref=result.brief_ref,
                company_brief_refs=company_brief_refs,
            )
            status_dirty = True
            triggered.append({"venue": venue, "brief_ref": result.brief_ref})
        else:
            failure_status = _failure_status(
                existing=venue_status,
                now=now,
                retry_lineage=retry_lineage,
                input_signature=success_signature,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            status[venue] = failure_status
            status_dirty = True
            failure = _latest_failure(failure_status) or {}
            _log_failure_retry(log, venue=venue, scope_id=scope_id, failure=failure)
            errors.append(
                _failure_error_result(
                    venue=venue,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    failure=failure,
                )
            )

    # ------------------------------------------------------------------
    # Passe GLOBAL — brief macro/géopolitique international sans scope régional
    # ------------------------------------------------------------------
    global_enabled = os.getenv("CASYS_NEWS_MACRO_GLOBAL_ENABLED", "1") != "0"
    if include_global is not None:
        global_enabled = global_enabled and include_global
    if global_enabled:
        geopolitical_events = tuple(_read_recent_gdelt_events(state_path, now=now))
        if not global_news_items and not macro_series and not macro_next and not geopolitical_events:
            skipped.append({"venue": "GLOBAL", "reason": "no_inputs"})
        else:
            global_input_refs: dict[str, Any] = {
                "global_news_uuids": _news_uuids(tuple(global_news_items)),
                "global_news_count": len(global_news_items),
                "macro_series_labels": [str(item.get("label")) for item in macro_series if item.get("label")],
                "geopolitical_event_count": len(geopolitical_events),
                "geopolitical_event_urls": [
                    str(ev.get("url") or "")
                    for ev in geopolitical_events
                    if ev.get("url")
                ][:60],
            }
            global_success_signature = _input_signature(
                venue="GLOBAL",
                macro_next=macro_next,
                macro_series=macro_series,
                input_refs=global_input_refs,
            )
            global_status = status.get("GLOBAL")
            global_retry_lineage = _global_retry_lineage_key(
                last_brief_ref=(
                    global_status.get("last_brief_ref")
                    if isinstance(global_status, Mapping)
                    else None
                )
            )
            if not force and _last_success_matches_signature(global_status, global_success_signature):
                skipped.append({"venue": "GLOBAL", "reason": "active_brief_same_inputs"})
            else:
                backoff = (
                    _failure_backoff(
                        global_status,
                        retry_lineage=global_retry_lineage,
                        now=now,
                    )
                    if not force
                    else None
                )
                if backoff is not None:
                    skipped.append(_failure_backoff_skip(venue="GLOBAL", backoff=backoff))
                    _log_retry_deferred(
                        log,
                        venue="GLOBAL",
                        scope_id="GLOBAL",
                        backoff=backoff,
                    )
                elif (
                    not force
                    and store.read_latest("GLOBAL", at=now) is not None
                    and _success_refresh_cooldown_active(global_status, now=now)
                ):
                    skipped.append({"venue": "GLOBAL", "reason": "active_brief_changed_inputs_cooldown"})
                else:
                    try:
                        global_feedback = _situation_feedback_for_venue(
                            state_path, "GLOBAL", store=memory
                        )
                    except Exception:  # noqa: BLE001 - digest must never block an analysis
                        global_feedback = {}
                    global_request = NewsMacroAnalysisRequest(
                        as_of=now.isoformat(),
                        valid_until=(now + timedelta(hours=DEFAULT_VALID_HOURS)).isoformat(),
                        venue="GLOBAL",
                        global_news_items=tuple(global_news_items),
                        macro_next=macro_next,
                        macro_series=macro_series,
                        geopolitical_events=geopolitical_events,
                        input_refs=global_input_refs,
                        situation_feedback=global_feedback or None,
                    )
                    if analyst is None:
                        from trader.agent.news_macro import LlmNewsMacroAnalyst

                        analyst = LlmNewsMacroAnalyst()
                    if memory is None:
                        memory = SituationMemoryStore(Path(state_dir) / "situation_memory.db")
                    global_result = run_news_macro_analysis(
                        global_request,
                        analyst=analyst,  # type: ignore[arg-type]
                        repository=store,
                        situation_repository=memory,
                    )
                    if global_result.written:
                        status["GLOBAL"] = _success_status(
                            existing=global_status,
                            now=now,
                            success_signature=global_success_signature,
                            brief_ref=global_result.brief_ref,
                        )
                        status_dirty = True
                        triggered.append({"venue": "GLOBAL", "brief_ref": global_result.brief_ref})
                    else:
                        failure_status = _failure_status(
                            existing=global_status,
                            now=now,
                            retry_lineage=global_retry_lineage,
                            input_signature=global_success_signature,
                            error_code=global_result.error_code,
                            error_message=global_result.error_message,
                        )
                        status["GLOBAL"] = failure_status
                        status_dirty = True
                        failure = _latest_failure(failure_status) or {}
                        _log_failure_retry(log, venue="GLOBAL", scope_id="GLOBAL", failure=failure)
                        errors.append(
                            _failure_error_result(
                                venue="GLOBAL",
                                error_code=global_result.error_code,
                                error_message=global_result.error_message,
                                failure=failure,
                            )
                        )

    try:
        if status_dirty:
            _write_status(status_path, status)
        if errors:
            log.warning("news macro analyst errors: %s", errors)
        return {"triggered": triggered, "skipped": skipped, "errors": errors}
    finally:
        if situation_store is None and memory is not None:
            memory.close()


class NewsMacroAnalysisRunner:
    """Single-flight background runner owned by one daemon process."""

    def __init__(
        self,
        *,
        tick_fn: Callable[..., dict[str, list[dict]]] = tick_news_macro_analysis,
        stop_timeout_s: float = DEFAULT_ASYNC_STOP_TIMEOUT_S,
        on_briefs_written: Callable[[tuple[dict[str, Any], ...]], None] | None = None,
    ) -> None:
        self._tick_fn = tick_fn
        self._stop_timeout_s = max(0.0, float(stop_timeout_s))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_kwargs: dict[str, Any] | None = None
        self._stopping = False
        self.on_briefs_written = on_briefs_written

    def trigger(self, **kwargs: Any) -> dict[str, Any]:
        """Start one background tick and return without waiting for the LLM."""

        if os.getenv("CASYS_NEWS_MACRO_ANALYST_ENABLED", "1") == "0":
            return {"triggered": False, "reason": "disabled"}
        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            if self._thread is not None:
                replaced_pending = self._pending_kwargs is not None
                self._pending_kwargs = dict(kwargs)
                logger = kwargs.get("logger") or _default_logger()
                logger.info(
                    "news macro trigger coalesced pending_replaced=%s venues=%s",
                    replaced_pending,
                    tuple(kwargs.get("venues") or VENUES),
                )
                return {"triggered": False, "reason": "queued_latest"}
            thread = threading.Thread(
                target=self._run,
                kwargs={"initial_kwargs": dict(kwargs)},
                daemon=True,
                name="news-macro-analysis",
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
            result: dict[str, Any] | None = None
            try:
                result = self._tick_fn(**kwargs, stop_requested=self._stop_event.is_set)
            except Exception as exc:  # noqa: BLE001 - background analysis is best-effort
                logger.warning("news macro analyst background failure: %s", exc)
            written = tuple(result.get("triggered") or ()) if isinstance(result, Mapping) else ()
            if written and self.on_briefs_written is not None:
                try:
                    self.on_briefs_written(written)
                except Exception as exc:  # noqa: BLE001 - callback must not kill the runner
                    logger.warning("news macro brief callback failed: %s", exc)
            with self._lock:
                if self._stopping or self._pending_kwargs is None:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return
                kwargs = self._pending_kwargs
                self._pending_kwargs = None

    def stop(self) -> None:
        """Prevent new ticks and wait briefly for the current daemon thread."""

        with self._lock:
            self._stopping = True
            self._pending_kwargs = None
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._stop_timeout_s)


def _situation_feedback_for_venue(
    state_dir: Path,
    venue: str,
    *,
    store: SituationMemoryStore | None = None,
) -> dict[str, Any]:
    """Market-as-judge digest for this venue. Empty when the sample is still noise."""

    source = store
    close_after = False
    try:
        from trader.application.analyst.situation_attribution import (
            situation_feedback_digest,
        )

        if source is None:
            db_path = Path(state_dir) / "situation_memory.db"
            if not db_path.is_file():
                return {}
            source = SituationMemoryStore(db_path)
            close_after = True
        digest = situation_feedback_digest(source.load_outcomes(), venue=venue)
    except Exception:  # noqa: BLE001 - advisory context, never block analysis
        return {}
    finally:
        if close_after and source is not None:
            source.close()
    if digest.get("status") != "observed":
        return {}
    return digest


def brief_ref_for_symbol(
    *,
    state_dir: Path,
    symbol: str,
    at: datetime,
    brief_store: NewsMacroBriefStore | None = None,
) -> dict[str, str] | None:
    """Resolve the active macro/news brief reference for a symbol's venue."""

    store = brief_store or NewsMacroBriefStore(Path(state_dir) / "news_briefs")
    return store.active_ref(venue_of(symbol), at=at)


def _read_recent_news_items(base_dir: Path, *, now: datetime, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[dict]:
    return _read_recent_jsonl_items(
        base_dir,
        now=now,
        lookback_days=lookback_days,
        limit=None,
    )


def _read_recent_global_news_items(state_dir: Path, *, now: datetime) -> list[dict]:
    rows: list[dict] = []
    for dirname in ("macro_headlines", "global_news_items"):
        rows.extend(
            _read_recent_jsonl_items(
                state_dir / dirname,
                now=now,
                lookback_days=DEFAULT_LOOKBACK_DAYS,
                limit=None,
            )
        )
    return _dedupe_rows_by_uuid(rows)[:DEFAULT_MAX_GLOBAL_NEWS_ITEMS]


def _read_recent_gdelt_events(state_dir: Path, *, now: datetime) -> list[dict]:
    """Lit state/gdelt/events.jsonl et retourne les événements récents bornés.

    Best-effort — jamais d'exception. Cap DEFAULT_MAX_GDELT_EVENTS, triés par
    ts_collected décroissant (les plus récents en tête).
    """
    path = Path(state_dir) / "gdelt" / "events.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    seen_urls: set[str] = set()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        rows.append(row)
        if len(rows) >= DEFAULT_MAX_GDELT_EVENTS:
            break
    return rows


def _read_recent_jsonl_items(
    base_dir: Path,
    *,
    now: datetime,
    lookback_days: int,
    limit: int | None,
) -> list[dict]:
    seen: set[str] = set()
    rows: list[dict] = []
    # Current day and most recently appended rows first: downstream venue caps
    # must retain the freshest items, not the beginning of yesterday's file.
    for offset in range(0, lookback_days + 1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = base_dir / f"{day}.jsonl"
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            uid = str(row.get("uuid") or "")
            if uid:
                if uid in seen:
                    continue
                seen.add(uid)
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                return rows
    return rows


def _dedupe_rows_by_uuid(rows: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        uid = str(row.get("uuid") or "")
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        result.append(row)
    return result


def _read_macro_series_snapshot(base_dir: Path) -> list[dict]:
    if not base_dir.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(base_dir.glob("*.jsonl")):
        row = _read_last_jsonl_object(path)
        if row is None:
            continue
        compact = _compact_macro_series_row(path.stem, row)
        if compact:
            rows.append(compact)
        if len(rows) >= DEFAULT_MAX_MACRO_SERIES:
            break
    return rows


def _read_last_jsonl_object(path: Path) -> dict | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            return row
    return None


def _compact_macro_series_row(label: str, row: dict) -> dict:
    compact = {
        "label": label,
        "series_id": row.get("series_id"),
        "period": row.get("period"),
        "value": row.get("value"),
        "ts_collected": row.get("ts_collected"),
    }
    return {key: value for key, value in compact.items() if value is not None}


def _coverage_metadata(
    *,
    state_path: Path,
    now: datetime,
    candidate_symbols: tuple[str, ...],
    venue_news: tuple[dict, ...],
    global_news_items: tuple[dict, ...],
    macro_series: tuple[dict, ...],
) -> dict[str, Any]:
    candidates = set(candidate_symbols)
    candidates_with_news = {
        str(item.get("symbol") or "").strip()
        for item in venue_news
        if str(item.get("symbol") or "").strip() in candidates
    }
    return {
        # The archive is populated by observed/fetched symbols, not by an exhaustive
        # market-wide collector. Absence must therefore never mean "no event".
        "status": "partial",
        "candidate_count": len(candidate_symbols),
        "candidates_with_news": len(candidates_with_news),
        "news_items_injected": len(venue_news),
        "news_item_cap": DEFAULT_MAX_VENUE_NEWS_ITEMS,
        "news_cap_reached": len(venue_news) >= DEFAULT_MAX_VENUE_NEWS_ITEMS,
        "global_news_items_injected": len(global_news_items),
        "global_headlines_status": _global_headlines_status(state_path, now, global_news_items),
        "macro_calendar_status": (
            "file_present" if (state_path / "macro_calendar.json").is_file() else "fallback_only"
        ),
        "macro_series_count": len(macro_series),
        "macro_series_stale_labels": _stale_macro_series_labels(macro_series, now=now),
    }


def _global_headlines_status(
    state_path: Path,
    now: datetime,
    global_news_items: tuple[dict, ...],
) -> str:
    if global_news_items:
        return "present"
    for base_name in ("macro_headlines", "global_news_items"):
        base_dir = state_path / base_name
        for offset in range(DEFAULT_LOOKBACK_DAYS + 1):
            day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            if (base_dir / f"{day}.jsonl").is_file():
                return "empty"
    return "missing"


def _stale_macro_series_labels(
    macro_series: tuple[dict, ...],
    *,
    now: datetime,
) -> list[str]:
    stale: list[str] = []
    for item in macro_series:
        label = str(item.get("label") or "").strip()
        period = _parse_macro_period(item.get("period"))
        if label and period is not None and now - period > timedelta(days=DEFAULT_MACRO_SERIES_STALE_DAYS):
            stale.append(label)
    return sorted(stale)


def _parse_macro_period(raw: Any) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    for candidate in (value, f"{value}-01", f"{value}-01-01"):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc)
    return None


def _candidate_shortlist_for_venue(state_dir: Path, venue: str) -> list[str]:
    """Read the complete upstream shortlist; this is not the universe hotlist."""

    return _candidate_symbols(_candidate_records_for_venue(state_dir, venue))


def _candidate_records_for_venue(state_dir: Path, venue: str) -> list[dict]:
    """Read candidate records, including persisted fresh-news evidence."""

    return list(_candidate_scope_for_venue(state_dir, venue)["candidates"])


def _candidate_scope_for_venue(
    state_dir: Path,
    venue: str,
    *,
    scope_store: CandidateScopeStore | None = None,
) -> dict[str, Any]:
    """Read the canonical scope, repairing only an explicitly scoped venue state."""

    repository = scope_store or CandidateScopeStore(state_dir / "candidate_scopes")
    try:
        current = repository.read_current(venue)
    except Exception:  # noqa: BLE001 - reported to the caller as an observable skip
        return {
            "candidates": [],
            "default_hotlist": [],
            "candidate_scope_id": "",
            "as_of": "",
            "error": "candidate_scope_store_error",
        }
    if isinstance(current, dict):
        return {
            "candidates": [
                dict(item)
                for item in current.get("candidates") or []
                if isinstance(item, dict) and item.get("symbol")
            ],
            "default_hotlist": list(current.get("default_hotlist") or []),
            "candidate_scope_id": str(current.get("candidate_scope_id") or "").strip(),
            "as_of": str(current.get("as_of") or "").strip(),
            "candidate_run_ids": list(current.get("candidate_run_ids") or []),
            "scope_phase": str(current.get("scope_phase") or "").strip(),
            "parent_candidate_scope_id": str(
                current.get("parent_candidate_scope_id") or ""
            ).strip(),
        }

    try:
        payload = json.loads((state_dir / "venue_state.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"candidates": [], "default_hotlist": [], "candidate_scope_id": "", "as_of": ""}
    venue_state = ((payload.get("venues") or {}).get(venue) or {}) if isinstance(payload, dict) else {}
    raw_candidates = venue_state.get("candidates") or []
    records = [dict(item) for item in raw_candidates if isinstance(item, dict) and item.get("symbol")]
    baseline = [
        str(symbol).strip()
        for symbol in venue_state.get("default_hotlist") or venue_state.get("hotlist") or []
        if str(symbol).strip()
    ]
    scope_as_of = str(
        venue_state.get("candidate_scope_as_of")
        or venue_state.get("last_close_at")
        or "legacy"
    ).strip()
    scope_id = str(venue_state.get("candidate_scope_id") or "").strip()
    if not records or not scope_id:
        return {
            "candidates": [],
            "default_hotlist": [],
            "candidate_scope_id": "",
            "as_of": scope_as_of,
        }
    computed_scope_id = candidate_scope_id(venue, records, baseline, scope_as_of)
    if computed_scope_id != scope_id:
        return {
            "candidates": [],
            "default_hotlist": [],
            "candidate_scope_id": "",
            "as_of": scope_as_of,
            "error": "candidate_scope_integrity_mismatch",
        }
    repaired = {
        "candidates": records,
        "default_hotlist": baseline,
        "candidate_scope_id": scope_id,
        "as_of": scope_as_of,
        "candidate_run_ids": list(venue_state.get("candidate_run_ids") or []),
        "scope_phase": str(venue_state.get("scope_phase") or "").strip(),
        "parent_candidate_scope_id": str(
            venue_state.get("parent_candidate_scope_id") or ""
        ).strip(),
        "venue": venue,
    }
    try:
        repository.append(repaired)
    except Exception:  # noqa: BLE001 - never analyze against a divergent scope source
        return {
            "candidates": [],
            "default_hotlist": [],
            "candidate_scope_id": "",
            "as_of": scope_as_of,
            "error": "candidate_scope_store_error",
        }
    return repaired


def _brief_matches_candidate_scope(brief: Any, scope_id: str) -> bool:
    if brief is None or not scope_id:
        return False
    refs = brief.input_refs if isinstance(brief.input_refs, dict) else {}
    return refs.get("candidate_scope_id") == scope_id


def _candidate_symbols(candidate_records: Iterable[dict]) -> list[str]:
    return _unique_nonempty(str(item.get("symbol") or "").strip() for item in candidate_records)


def _company_anchor_symbols(
    *,
    candidate_records: Iterable[dict],
    venue_news: Iterable[dict],
) -> tuple[str, ...]:
    symbols = [str(item.get("symbol") or "").strip() for item in venue_news]
    symbols.extend(
        str(candidate.get("symbol") or "").strip()
        for candidate in candidate_records
        if isinstance(candidate.get("fresh_news"), Mapping)
    )
    return tuple(_unique_nonempty(symbols))


def _company_news_anchors(raw_symbols: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    anchors: dict[str, dict[str, Any]] = {}
    for raw_symbol, raw_entry in raw_symbols.items():
        symbol = str(raw_symbol or "").strip()
        entry = dict(raw_entry) if isinstance(raw_entry, Mapping) else {}
        brief_ref = entry.get("brief_ref")
        if not symbol or not isinstance(brief_ref, Mapping):
            continue
        brief_id = str(brief_ref.get("brief_id") or "").strip()
        if not brief_id:
            continue
        anchors[symbol] = {
            "anchor_ref": f"company_brief:{symbol}:{brief_id}",
            "brief_ref": dict(brief_ref),
            "status": entry.get("status"),
            "company_thesis_status": entry.get("company_thesis_status"),
            "summary": entry.get("summary"),
            "drivers": list(entry.get("drivers") or ())[:2],
            "catalysts": list(entry.get("catalysts") or ())[:2],
            "risks": list(entry.get("risks") or ())[:2],
            "selection_view": dict(entry.get("selection_view") or {}),
            "security_readiness": entry.get("security_readiness"),
        }
    return anchors


def _filter_news_for_venue(
    news_items: list[dict],
    *,
    venue: str,
    candidate_symbols: tuple[str, ...],
    candidate_records: Iterable[dict] = (),
) -> list[dict]:
    candidates = set(candidate_symbols)
    rows: list[dict] = []
    seen_uuids: set[str] = set()

    # L'évidence challenger persistée porte l'attribution autoritative. Le
    # symbole brut de l'archive peut n'être que celui pour lequel le flux a été
    # fetché ; on le remplace donc par le symbole candidat attribué.
    for candidate in candidate_records:
        attributed_symbol = str(candidate.get("symbol") or "").strip()
        fresh_news = candidate.get("fresh_news")
        if not attributed_symbol or not isinstance(fresh_news, dict):
            continue
        evidence = fresh_news.get("evidence") or []
        for raw_evidence in evidence:
            if not isinstance(raw_evidence, dict):
                continue
            uid = str(raw_evidence.get("uuid") or raw_evidence.get("source_ref") or "").strip()
            if not uid or uid in seen_uuids:
                continue
            evidence_row = _compact_news_item(
                {**raw_evidence, "uuid": uid, "symbol": attributed_symbol}
            )
            evidence_row["candidate_source"] = "fresh_news"
            candidate_sources = candidate.get("candidate_sources")
            if isinstance(candidate_sources, (list, tuple, set)):
                evidence_row["candidate_sources"] = _unique_nonempty(
                    str(source) for source in candidate_sources
                )
            rows.append(evidence_row)
            seen_uuids.add(uid)
            if len(rows) >= DEFAULT_MAX_VENUE_NEWS_ITEMS:
                return rows

    for row in news_items:
        symbol = str(row.get("symbol") or "").strip()
        if candidates:
            keep = symbol in candidates
        else:
            keep = bool(symbol) and venue_of(symbol) == venue
        uid = str(row.get("uuid") or "").strip()
        if keep and (not uid or uid not in seen_uuids):
            rows.append(_compact_news_item(row))
            if uid:
                seen_uuids.add(uid)
        if len(rows) >= DEFAULT_MAX_VENUE_NEWS_ITEMS:
            break
    return rows


def _filter_global_news_for_venue(news_items: list[dict], *, venue: str) -> list[dict]:
    rows: list[dict] = []
    venue_key = venue.upper()
    for row in news_items:
        regions = _upper_tokens(row.get("regions") or row.get("venues") or row.get("zones"))
        keep = not regions or bool(regions & {venue_key, "GLOBAL", "ALL", "WORLD"})
        if keep:
            rows.append(_compact_global_news_item(row))
        if len(rows) >= DEFAULT_MAX_GLOBAL_NEWS_ITEMS:
            break
    return rows


def _compact_news_item(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "uuid",
            "symbol",
            "title",
            "publisher",
            "published_at",
            "fetched_at",
            "link",
            "fetched_for",
            "attribution",
            "event_type",
            "score",
            "candidate_source",
            "candidate_sources",
        )
        if row.get(key) is not None
    }


def _compact_global_news_item(row: dict) -> dict:
    keys = (
        "uuid",
        "title",
        "publisher",
        "published_at",
        "fetched_at",
        "link",
        "topic",
        "category",
        "regions",
        "venues",
        "zones",
        "families",
        "symbols",
        "severity",
    )
    return {key: row.get(key) for key in keys if row.get(key) is not None}


def _family_context(
    *,
    candidate_symbols: tuple[str, ...],
    news_items: tuple[dict, ...],
    global_news_items: tuple[dict, ...],
) -> dict[str, dict]:
    context: dict[str, dict] = {}

    def entry(family: str) -> dict:
        return context.setdefault(
            family,
            {
                "candidate_symbols": [],
                "news_item_uuids": [],
                "global_news_uuids": [],
                "fresh_news_count": 0,
                "global_news_count": 0,
            },
        )

    for symbol in candidate_symbols:
        family = family_for_symbol(symbol)
        if family is None:
            continue
        _append_unique(entry(family)["candidate_symbols"], symbol)

    for item in news_items:
        symbol = str(item.get("symbol") or "").strip()
        family = family_for_symbol(symbol) if symbol else None
        if family is None:
            continue
        data = entry(family)
        uid = str(item.get("uuid") or "").strip()
        if uid:
            _append_unique(data["news_item_uuids"], uid)
        data["fresh_news_count"] = int(data["fresh_news_count"]) + 1

    for item in global_news_items:
        uid = str(item.get("uuid") or "").strip()
        families = _string_tokens(item.get("families"))
        symbols = _string_tokens(item.get("symbols"))
        for symbol in symbols:
            family = family_for_symbol(symbol)
            if family:
                _append_unique(families, family)
        for family in families:
            data = entry(family)
            if uid:
                _append_unique(data["global_news_uuids"], uid)
            data["global_news_count"] = int(data["global_news_count"]) + 1

    return {family: data for family, data in sorted(context.items())}


def _string_tokens(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    return _unique_nonempty(str(value).strip() for value in values)


def _upper_tokens(raw: Any) -> set[str]:
    return {token.upper() for token in _string_tokens(raw)}


def _append_unique(items: list, value: str) -> None:
    if value and value not in items:
        items.append(value)


def _news_uuids(news_items: Iterable[dict]) -> list[str]:
    return _unique_nonempty(str(item.get("uuid") or "") for item in news_items)[:500]


def _unique_nonempty(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _input_signature(
    *,
    venue: str,
    macro_next: tuple[dict, ...],
    macro_series: tuple[dict, ...],
    input_refs: dict,
) -> str:
    """Fingerprint material evidence used to deduplicate a successful brief.

    Freshness timestamps are useful in the prompt and persisted brief, but do
    not by themselves make a new piece of evidence.  Keeping them out of the
    success signature also prevents a harmless collector refresh from masking
    a last successful brief as stale.
    """

    payload = {
        "venue": venue,
        # ``in_h`` changes every loop without new information. Only stable event
        # identity belongs in a material-input signature.
        "macro_next": [
            {"event": item.get("event"), "at": item.get("at")}
            for item in macro_next
        ],
        "macro_series": list(macro_series),
        "input_refs": input_refs,
    }
    return _request_signature(_without_non_material_timestamps(payload))


_NON_MATERIAL_TEMPORAL_FIELDS = frozenset(
    {
        "as_of",
        "valid_until",
        "created_at",
        "updated_at",
        "modified_at",
        "generated_at",
        "refreshed_at",
        "fetched_at",
        "retrieved_at",
        "ingested_at",
        "collected_at",
        "observed_at",
        "received_at",
        "processed_at",
        "checked_at",
        "last_checked_at",
        "last_modified_at",
        "ts_collected",
        "seendate",
        "timestamp",
        "timestamp_ms",
    }
)
def _request_signature(payload: Any) -> str:
    blob = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _without_non_material_timestamps(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        return {
            str(key): _without_non_material_timestamps(value)
            for key, value in payload.items()
            if not _is_non_material_temporal_field(str(key))
        }
    if isinstance(payload, list):
        return [_without_non_material_timestamps(value) for value in payload]
    if isinstance(payload, tuple):
        return tuple(_without_non_material_timestamps(value) for value in payload)
    if isinstance(payload, set):
        return sorted(_without_non_material_timestamps(value) for value in payload)
    return payload


def _is_non_material_temporal_field(key: str) -> bool:
    lowered = key.lower()
    return lowered in _NON_MATERIAL_TEMPORAL_FIELDS or lowered.endswith("_as_of")


def _regional_retry_lineage_key(*, venue: str, candidate_scope_id: str) -> str:
    """Return a retry epoch bound only to the regional candidate scope.

    News, company cards and macro series can update while a model is down.
    Those updates must invalidate a successful brief, but cannot reset the
    failure budget until the candidate scope genuinely changes.
    """

    return _request_signature(
        {
            "kind": "regional",
            "venue": str(venue).upper(),
            "candidate_scope_id": str(candidate_scope_id),
        }
    )


def _global_retry_lineage_key(*, last_brief_ref: Any) -> str:
    """Return the GLOBAL retry epoch anchored to the last successful brief.

    Global news and macro collectors can update continuously while the model is
    unavailable.  They remain material for success deduplication, but never
    reset a failing retry sequence.  Only a newly persisted GLOBAL brief starts
    a new epoch; before the first success all failures share ``bootstrap``.
    ``force`` may bypass the active backoff gate, but does not fabricate a
    successful epoch.
    """

    brief_id = ""
    if isinstance(last_brief_ref, Mapping):
        brief_id = str(last_brief_ref.get("brief_id") or "").strip()
    return _request_signature(
        {
            "kind": "global",
            "venue": "GLOBAL",
            "last_success_brief_id": brief_id or "bootstrap",
        }
    )


def _last_success_matches_signature(raw: Any, signature: str) -> bool:
    return bool(
        isinstance(raw, Mapping)
        and raw.get("last_success_at")
        and _last_success_signature(raw) == signature
    )


def _last_success_signature(raw: Mapping[str, Any]) -> Any:
    # ``last_input_signature`` was the old mixed success/failure field.  Read
    # it for existing successful state files, while all new writes use the
    # unambiguous key.  Old failure records overwrote it, so never mistake their
    # failed input for a successful brief signature.
    explicit = raw.get("last_success_input_signature")
    if explicit:
        return explicit
    if raw.get("last_failure_at"):
        return None
    return raw.get("last_input_signature")


def _success_refresh_cooldown_active(raw: Any, *, now: datetime) -> bool:
    if not isinstance(raw, Mapping) or not raw.get("last_success_at"):
        return False
    last_success = _parse_datetime(raw.get("last_success_at"))
    if last_success is None:
        return False
    return now - last_success < timedelta(hours=DEFAULT_SUCCESS_REFRESH_COOLDOWN_HOURS)


def _failure_backoff(
    raw: Any,
    *,
    retry_lineage: str,
    now: datetime,
) -> dict[str, Any] | None:
    """Return active persisted backoff for exactly one retry lineage."""

    return _shared_failure_backoff(
        raw,
        retry_lineage=retry_lineage,
        now=now,
        legacy_aliases=True,
        base_minutes=DEFAULT_FAILURE_BACKOFF_MINUTES,
        max_minutes=DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
    )


def _failure_delay_seconds(attempt: int) -> int:
    return _shared_failure_delay_seconds(
        attempt,
        base_minutes=DEFAULT_FAILURE_BACKOFF_MINUTES,
        max_minutes=DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
    )


def _success_status(
    *,
    existing: Any,
    now: datetime,
    success_signature: str,
    brief_ref: Mapping[str, Any] | None,
    company_brief_refs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist success and clear only this venue's latest failure."""

    status = dict(existing) if isinstance(existing, Mapping) else {}
    status.update(
        {
            "last_success_at": now.isoformat(),
            "last_success_input_signature": success_signature,
            # Backward-compatible read field for previously persisted status.
            "last_input_signature": success_signature,
            "last_brief_ref": dict(brief_ref) if isinstance(brief_ref, Mapping) else brief_ref,
            "last_failure_at": None,
            "last_error": None,
            "last_attempt_input_signature": success_signature,
            "retry_lineage": None,
            "latest_failure": None,
        }
    )
    if company_brief_refs is None:
        status.pop("last_company_brief_refs", None)
    else:
        status["last_company_brief_refs"] = dict(company_brief_refs)
    return status


def _failure_status(
    *,
    existing: Any,
    now: datetime,
    retry_lineage: str,
    input_signature: str,
    error_code: str | None,
    error_message: str | None,
) -> dict[str, Any]:
    """Persist an exponentially scheduled failure without replacing success."""

    previous_status = dict(existing) if isinstance(existing, Mapping) else {}
    status = dict(previous_status)
    if (
        status.get("last_success_at")
        and not status.get("last_success_input_signature")
        and not status.get("last_failure_at")
    ):
        legacy_success_signature = status.get("last_input_signature")
        if legacy_success_signature:
            status["last_success_input_signature"] = legacy_success_signature

    previous = _latest_failure(previous_status)
    same_lineage = previous is not None and previous.get("retry_lineage") == retry_lineage
    previous_attempt = (
        _positive_int(
            previous.get("retry_attempt") or previous.get("attempt"),
            default=1,
        )
        if same_lineage and previous is not None
        else 0
    )
    attempt = previous_attempt + 1 if same_lineage else 1
    delay_seconds = _failure_delay_seconds(attempt)
    failed_at = now.isoformat()
    code = str(error_code or "analysis_not_written")
    message = str(error_message or "")[:500]
    next_retry_at = (now + timedelta(seconds=delay_seconds)).isoformat()
    failure = {
        "failed_at": failed_at,
        "input_signature": input_signature,
        "retry_lineage": retry_lineage,
        "retry_attempt": attempt,
        "retry_delay_seconds": delay_seconds,
        "next_retry_at": next_retry_at,
        "error_code": code,
        "error_message": message,
        # Readable aliases make the status directly useful to simple clients.
        "attempt": attempt,
        "delay_seconds": delay_seconds,
        "next_at": next_retry_at,
        "error": {"code": code, "message": message},
    }
    status.update(
        {
            "last_failure_at": failed_at,
            "last_error": {"code": code, "message": message},
            "last_attempt_input_signature": input_signature,
            "retry_lineage": retry_lineage,
            "latest_failure": failure,
        }
    )
    return status


def _latest_failure(raw: Any) -> dict[str, Any] | None:
    # Nested ``latest_failure`` is returned as-is (no status filter).  Root
    # ``last_failure_at`` / ``last_error`` remain readable for the previous
    # fixed-backoff release; they have no lineage, so they cannot block a
    # new retry epoch.
    failure = _shared_latest_failure(raw, legacy_aliases=True)
    return dict(failure) if failure is not None else None


def _failure_backoff_skip(*, venue: str, backoff: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "venue": venue,
        "reason": "failure_backoff",
        "attempt": backoff.get("attempt"),
        "delay_seconds": backoff.get("delay_seconds"),
        "next_retry_at": backoff.get("next_retry_at"),
        "error": backoff.get("error"),
    }


def _failure_error_result(
    *,
    venue: str,
    error_code: str | None,
    error_message: str | None,
    failure: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "venue": venue,
        "code": str(error_code or "analysis_not_written"),
        "message": str(error_message or ""),
        "attempt": failure.get("retry_attempt") or failure.get("attempt"),
        "delay_seconds": failure.get("retry_delay_seconds") or failure.get("delay_seconds"),
        "next_retry_at": failure.get("next_retry_at") or failure.get("next_at"),
    }


def _log_failure_retry(
    log: LoggerLike,
    *,
    venue: str,
    scope_id: str,
    failure: Mapping[str, Any],
) -> None:
    log.warning(
        "news macro retry scheduled venue=%s scope=%s attempt=%s delay_s=%s next_at=%s error=%s",
        venue,
        scope_id,
        failure.get("retry_attempt") or failure.get("attempt"),
        failure.get("retry_delay_seconds") or failure.get("delay_seconds"),
        failure.get("next_retry_at") or failure.get("next_at"),
        _failure_error_code(failure),
    )


def _log_retry_deferred(
    log: LoggerLike,
    *,
    venue: str,
    scope_id: str,
    backoff: Mapping[str, Any],
) -> None:
    log.info(
        "news macro retry deferred venue=%s scope=%s attempt=%s delay_s=%s next_at=%s error=%s",
        venue,
        scope_id,
        backoff.get("attempt"),
        backoff.get("delay_seconds"),
        backoff.get("next_retry_at"),
        backoff.get("error"),
    )


def _failure_error_code(failure: Mapping[str, Any]) -> str:
    error = failure.get("error")
    if isinstance(error, Mapping) and error.get("code"):
        return str(error["code"])
    return str(failure.get("error_code") or "unknown")


def latest_failure_for_venue(*, state_dir: Path, venue: str) -> dict[str, Any] | None:
    """Read one macro failure projection without importing cockpit/UI code."""

    status = _load_status(Path(state_dir) / "news_macro_analysis_status.json")
    return _latest_failure(status.get(str(venue).upper()))


def _load_status(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
