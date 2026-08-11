"""Durable, non-blocking runtime for per-symbol company intelligence."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from trader.application.analyst import CompanyMicroAnalysisRequest, run_company_micro_analysis
from trader.application.queue.contracts import RetryableError
from trader.domain.company import CompanyEvidenceSnapshot
from trader.infrastructure.market_sources.company import (
    CompositeCompanyEvidenceProvider,
    EsefCompanyEvidenceProvider,
    FinMindCompanyEvidenceProvider,
    LocalCompanyNewsEvidenceProvider,
    SecEdgarCompanyEvidenceProvider,
    YFinanceCompanyEvidenceProvider,
)
from trader.infrastructure.queue.decide_pool import DecidePool
from trader.infrastructure.queue.ledger import TaskLedger
from trader.infrastructure.queue.pools import ResourcePools
from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
from trader.infrastructure.state_db.company_analysis_run_store import CompanyAnalysisRunStore
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.fundamental_item_store import FundamentalItemStore
from trader.market.radar_config import load_radar_params
from trader.market.rotation.schedule import load_sessions, preopen_venues
from trader.market.rotation.wiring import venue_of
from trader.runtime.protocols import LoggerLike

COMPANY_TASK_KIND = "company_micro"
COMPANY_RESOURCE = "company-research"
VENUES = ("TW", "EU", "US")
DEFAULT_CONCURRENCY = 2
DEFAULT_WAIT_TIMEOUT_S = 600.0
# A company micro report is advisory and its provider can be slow or
# temporarily unavailable. Keep retries sparse enough not to hammer ACPX or a
# source outage: six total calls spread over 30 min, 1 h, 2 h, 4 h and 6 h.
# This intentionally matches the 30 min -> 6 h bounded regional-report policy.
COMPANY_TASK_MAX_ATTEMPTS = 6
COMPANY_RETRY_BACKOFF_BASE_MS = 30 * 60 * 1000
COMPANY_RETRY_BACKOFF_MAX_MS = 6 * 60 * 60 * 1000
# Fundamental analysis is event-driven: new financials/profile/earnings (which
# change the evidence signature) re-analyze immediately; otherwise a company is
# re-analyzed at most once per this cooldown, to digest accumulated news without
# re-running on every intraday headline.
DEFAULT_REFRESH_COOLDOWN_HOURS = 24.0


def _refresh_cooldown_hours() -> float:
    raw = os.getenv("CASYS_COMPANY_MICRO_REFRESH_COOLDOWN_HOURS")
    if raw is None:
        return DEFAULT_REFRESH_COOLDOWN_HOURS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_COOLDOWN_HOURS
    return value if value >= 0 else DEFAULT_REFRESH_COOLDOWN_HOURS


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Always return UTC (the name promises it): a stored as_of may carry a local
    # offset, and callers must be free to compare .date()/.hour safely.
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _is_successful_company_run(value: Mapping[str, Any]) -> bool:
    """Whether a run projection still points to a usable company brief."""

    return str(value.get("status") or "").strip() in {"success", "unchanged"}


def _latest_company_failure(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read a nested failure without mistaking its retained success for failure."""

    nested = value.get("latest_failure")
    candidate = nested if isinstance(nested, Mapping) else value
    if str(candidate.get("status") or "").strip() != "error":
        return None
    return dict(candidate)


def _company_failure_retry_metadata(task: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Mirror the durable queue schedule in a failure record for read models.

    The ledger remains the source of truth (and writes the exact schedule just
    after the handler returns). This copies the same deterministic policy into
    the append-only report record so a future gallery can show the current
    failure without rediscovering queue internals.
    """

    try:
        attempt = max(1, int(task.get("attempts") or 1))
    except (TypeError, ValueError):
        attempt = 1
    try:
        max_attempts = max(1, int(task.get("max_attempts") or COMPANY_TASK_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        max_attempts = COMPANY_TASK_MAX_ATTEMPTS
    if attempt >= max_attempts:
        return {
            "queue_attempt": attempt,
            "queue_max_attempts": max_attempts,
            "retry_attempt": attempt,
            "retry_delay_seconds": 0,
            "next_retry_at": None,
        }
    delay_ms = min(
        COMPANY_RETRY_BACKOFF_BASE_MS * (2 ** max(0, attempt - 1)),
        COMPANY_RETRY_BACKOFF_MAX_MS,
    )
    return {
        "queue_attempt": attempt,
        "queue_max_attempts": max_attempts,
        "retry_attempt": attempt,
        "retry_delay_seconds": delay_ms // 1000,
        "next_retry_at": datetime.fromtimestamp(
            (int(now_ms) + delay_ms) / 1000,
            tz=timezone.utc,
        ).isoformat(),
    }


def _company_run_id(
    *,
    symbol: str,
    as_of: str,
    input_signature: str,
    task_id: int | None,
    attempt: int | None,
) -> str:
    """Give retries distinct append-only IDs while preserving the old prefix."""

    base = f"company:{symbol}:{as_of}:{input_signature[:12]}"
    if task_id is None:
        return base
    attempt_text = str(attempt) if attempt is not None else "-"
    return f"{base}:task-{task_id}:attempt-{attempt_text}"


def decide_company_refresh(
    *,
    now: datetime,
    last_as_of: str | None,
    venue_in_preopen: bool,
    cooldown: timedelta,
    preopen_window: timedelta,
) -> tuple[bool, str]:
    """Whether an unchanged-fundamentals company must be re-analyzed now.

    Mirrors the global-posture cadence (``decide_global_posture_refresh``):
    refresh once inside the symbol's venue pre-open window — so the brief is
    fresh at the gong — plus a cooldown floor; otherwise reuse. New
    financials/profile/earnings change the evidence signature upstream and
    bypass this entirely (immediate re-analysis). Pure and deterministic.
    """
    if not last_as_of:
        return True, "bootstrap"
    last = _parse_utc(last_as_of)
    if last is None:
        return True, "no_as_of"
    age = now - last
    if venue_in_preopen and age >= preopen_window:
        return True, "preopen"
    if cooldown > timedelta(0) and age >= cooldown:
        return True, "cooldown"
    return False, "fresh"


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def build_company_evidence_provider(
    config_dir: str | Path,
    *,
    issuer_names: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
) -> CompositeCompanyEvidenceProvider:
    """Compose official venue adapters plus the cross-market fallback."""

    config_path = Path(config_dir)
    entities = _load_yaml_mapping(config_path / "company_entities.yaml")
    names = dict(issuer_names or {})
    for symbol, raw in entities.items():
        if isinstance(raw, Mapping) and raw.get("issuer_name"):
            names[str(symbol)] = str(raw["issuer_name"])
    entity_mappings = {
        str(symbol): dict(raw)
        for symbol, raw in entities.items()
        if isinstance(raw, Mapping)
    }
    return CompositeCompanyEvidenceProvider(
        (
            SecEdgarCompanyEvidenceProvider(),
            FinMindCompanyEvidenceProvider(issuer_names=names),
            EsefCompanyEvidenceProvider(entities=entity_mappings, issuer_names=names),
            LocalCompanyNewsEvidenceProvider(Path(state_dir) / "news_items") if state_dir else _NullProvider(),
            YFinanceCompanyEvidenceProvider(),
        ),
        issuer_names=names,
    )


def discover_company_symbols(
    *,
    config_dir: str | Path,
    state_dir: str | Path,
    scope: str = "current",
    symbols: Iterable[str] = (),
    scope_store: CandidateScopeStore | None = None,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Resolve an explicit list or the active candidate/universe research scope."""

    requested = _unique_symbols(symbols)
    if requested:
        return requested, {"scope": "explicit", "candidate_scope_ids": [], "fallback": False}
    normalized_scope = str(scope or "current").strip().lower()
    if normalized_scope not in {"current", "active"}:
        raise ValueError("company intelligence scope must be 'current', 'active' or explicit symbols")

    config_path = Path(config_dir)
    state_path = Path(state_dir)
    scopes = scope_store or CandidateScopeStore(state_path / "candidate_scopes")
    resolved: list[str] = []
    scope_ids: list[str] = []
    for venue in VENUES:
        current = scopes.read_current(venue)
        if current is None:
            continue
        scope_id = str(current.get("candidate_scope_id") or "").strip()
        if scope_id:
            scope_ids.append(scope_id)
        resolved.extend(
            str(item.get("symbol") or "").strip()
            for item in current.get("candidates") or ()
            if isinstance(item, Mapping)
        )
        resolved.extend(str(symbol or "").strip() for symbol in current.get("sticky_context_at_close") or ())

    universe = _load_yaml_mapping(config_path / "universe.yaml")
    active_symbols = _unique_symbols(universe.get("symbols") or ())
    if normalized_scope == "active":
        return active_symbols, {
            "scope": "active",
            "candidate_scope_ids": [],
            "fallback": False,
            "active_universe_count": len(active_symbols),
        }
    # Screen = candidats/sticky HORS univers uniquement. Les symboles d'univers
    # sont analysés (en priorité) par le scope "active"/deep — les inclure ici
    # dupliquerait l'analyse (même evidence, même prompt). Les lecteurs résolvent
    # le bon brief via depth="preferred" (deep pour un membre d'univers).
    active_set = set(active_symbols)
    normalized = tuple(symbol for symbol in _unique_symbols(resolved) if symbol not in active_set)
    return normalized, {
        "scope": "current",
        "candidate_scope_ids": scope_ids,
        "fallback": not bool(scope_ids),
        "candidate_coverage_pending": not bool(scope_ids),
        "active_universe_count": len(active_symbols),
        "screen_excludes_active": True,
    }


class CompanyIntelligenceRuntime:
    """Own the dedicated research ledger, worker pool and coalescing scanner."""

    def __init__(
        self,
        *,
        config_dir: str | Path,
        state_dir: str | Path,
        provider: object | None = None,
        analyst: object | None = None,
        concurrency: int | None = None,
        logger: LoggerLike | None = None,
        on_brief_written: Callable[[dict[str, Any]], None] | None = None,
        now_fn: Callable[[], float] = time.time,
        start_workers: bool = True,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.state_dir = Path(state_dir)
        self.log = logger or _default_logger()
        self.enabled = _enabled(self.config_dir)
        self.concurrency = max(1, int(concurrency or _configured_concurrency(self.config_dir)))
        self.refresh_cooldown_hours = _refresh_cooldown_hours()
        self.provider = provider
        self.analyst = analyst
        self.on_brief_written = on_brief_written
        self.now_fn = now_fn
        self.evidence_store = FundamentalItemStore(self.state_dir / "fundamental_items")
        self.brief_store = CompanyIntelligenceStore(self.state_dir / "company_intelligence")
        self.run_store = CompanyAnalysisRunStore(self.state_dir / "company_analysis_runs")
        self.ledger = TaskLedger(self.state_dir / "company_research_tasks.db")
        self.ledger.recover_on_boot(now_ms=self._now_ms())
        self.pool = DecidePool(
            ledger=self.ledger,
            pools=ResourcePools({COMPANY_RESOURCE: self.concurrency}),
            handlers={COMPANY_TASK_KIND: self._handle_task},
            num_workers=self.concurrency,
            now_fn=self.now_fn,
            lease_ms=600_000,
            backoff_base_ms=COMPANY_RETRY_BACKOFF_BASE_MS,
            backoff_max_ms=COMPANY_RETRY_BACKOFF_MAX_MS,
        )
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._pending_scan: dict[str, Any] | None = None
        self._stopping = False
        self._workers_started = bool(self.enabled and start_workers)
        if self._workers_started:
            self.pool.start()

    def trigger(self, **kwargs: Any) -> dict[str, Any]:
        """Start a source scan in the background; never wait in the daemon cycle."""

        if not self.enabled:
            return {"triggered": False, "reason": "disabled"}
        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            if self._scan_thread is not None:
                self._pending_scan = dict(kwargs)
                return {"triggered": False, "reason": "queued_latest"}
            thread = threading.Thread(
                target=self._scan_loop,
                kwargs={"initial_kwargs": dict(kwargs)},
                daemon=True,
                name="company-intelligence-scan",
            )
            self._scan_thread = thread
            thread.start()
        return {"triggered": True, "_thread": thread}

    def refresh(
        self,
        *,
        symbols: Iterable[str] = (),
        scope: str = "current",
        depth: str = "screen",
        trigger: str = "manual",
        as_of: datetime | None = None,
        wait: bool = False,
        wait_timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "enqueued": [], "skipped": [], "errors": []}
        selected, scope_meta = discover_company_symbols(
            config_dir=self.config_dir,
            state_dir=self.state_dir,
            scope=scope,
            symbols=symbols,
        )
        now = _ensure_utc(as_of or datetime.now(timezone.utc))
        result = self._collect_and_enqueue(
            selected,
            as_of=now,
            depth=depth,
            trigger=trigger,
            candidate_scope_ids=tuple(scope_meta.get("candidate_scope_ids") or ()),
        )
        result["scope"] = scope_meta
        if wait:
            result["wait"] = self.wait_for_tasks(
                [item["task_id"] for item in result["enqueued"]],
                timeout_s=wait_timeout_s,
            )
        return result

    def wait_for_tasks(self, task_ids: Iterable[int], *, timeout_s: float) -> dict[str, Any]:
        pending = {int(task_id) for task_id in task_ids}
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        statuses: dict[int, str] = {}
        while pending and time.monotonic() < deadline:
            for task_id in tuple(pending):
                row = self.ledger.get(task_id)
                status = str((row or {}).get("status") or "missing")
                statuses[task_id] = status
                if status in {"done", "dead", "missing"}:
                    pending.remove(task_id)
            if pending:
                time.sleep(0.05)
        return {
            "completed": not pending,
            "pending_task_ids": sorted(pending),
            "statuses": {str(key): value for key, value in sorted(statuses.items())},
        }

    def status(self) -> dict[str, Any]:
        counts = {
            status: self.ledger.count_by_status(status, kind=COMPANY_TASK_KIND)
            for status in ("pending", "running", "done", "dead")
        }
        latest_runs = []
        if self.run_store.latest_dir.is_dir():
            for path in self.run_store.latest_dir.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    latest_runs.append(payload)
        successes = [row for row in latest_runs if _is_successful_company_run(row)]
        failures = [failure for row in latest_runs if (failure := _latest_company_failure(row)) is not None]
        return {
            "enabled": self.enabled,
            "ledger": str(self.ledger.path),
            "queue": counts,
            "latest_success": max(successes, key=lambda row: str(row.get("as_of") or ""), default=None),
            "latest_failure": max(failures, key=lambda row: str(row.get("as_of") or ""), default=None),
            "symbols_observed": len(latest_runs),
        }

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._pending_scan = None
            self._stop_event.set()
            thread = self._scan_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if self._workers_started:
            self.pool.stop(timeout_s=1.0)

    def _scan_loop(self, *, initial_kwargs: dict[str, Any]) -> None:
        kwargs = initial_kwargs
        while True:
            try:
                self.refresh(**kwargs)
            except Exception as exc:  # noqa: BLE001 - background research is fail-open
                self.log.warning("company intelligence scan failed: %s", exc)
            with self._lock:
                if self._stopping or self._pending_scan is None:
                    if self._scan_thread is threading.current_thread():
                        self._scan_thread = None
                    return
                kwargs = self._pending_scan
                self._pending_scan = None

    def _collect_and_enqueue(
        self,
        symbols: Iterable[str],
        *,
        as_of: datetime,
        depth: str,
        trigger: str,
        candidate_scope_ids: tuple[str, ...],
    ) -> dict[str, list[dict[str, Any]]]:
        provider = self._provider()
        # Cadence: an unchanged-fundamentals company is re-analyzed once in its
        # venue pre-open window (fresh brief at the gong) plus a cooldown floor.
        # config_dir points at <root>/config; load_sessions wants the root.
        radar = load_radar_params(self.config_dir)
        sessions = load_sessions(str(self.config_dir.parent))
        preopen_now = set(
            preopen_venues(as_of.isoformat(), sessions, window_minutes=radar.preopen_window_minutes)
        )
        preopen_window = timedelta(minutes=radar.preopen_window_minutes)
        cooldown = timedelta(hours=self.refresh_cooldown_hours)
        enqueued: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def collect(symbol: str) -> tuple[str, CompanyEvidenceSnapshot | None, Exception | None]:
            try:
                return symbol, provider.collect(symbol=symbol, as_of=as_of.isoformat()), None
            except Exception as exc:  # noqa: BLE001 - recorded per symbol
                return symbol, None, exc

        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="company-source") as executor:
            futures = [executor.submit(collect, symbol) for symbol in _unique_symbols(symbols)]
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                symbol, evidence, error = future.result()
                if error is not None or evidence is None:
                    errors.append(
                        {
                            "symbol": symbol,
                            "code": (error or RuntimeError("no evidence")).__class__.__name__,
                            "message": str(error or "no evidence snapshot")[:300],
                        }
                    )
                    continue
                self.evidence_store.append_many(evidence.items)
                current = self.brief_store.read_current(symbol, depth=depth)
                if current is not None and current.input_signature == evidence.input_signature:
                    # Fundamentals unchanged (news alone no longer move the
                    # signature). Re-analyze only to keep the brief fresh at the
                    # venue gong, or once per cooldown; otherwise reuse.
                    due, reason = decide_company_refresh(
                        now=as_of,
                        last_as_of=current.as_of,
                        venue_in_preopen=venue_of(symbol) in preopen_now,
                        cooldown=cooldown,
                        preopen_window=preopen_window,
                    )
                    if not due:
                        skipped.append(
                            {"symbol": symbol, "reason": f"unchanged:{reason}", "brief_ref": current.ref()}
                        )
                        # This is a scanner observation, not an LLM attempt.
                        # Persisting one row per unchanged symbol and daemon
                        # tick created tens of thousands of no-op run rows and
                        # could replace the projection holding a real failure.
                        # The returned ``skipped`` entry keeps the useful live
                        # status while the durable run history stays attempt-led.
                        continue
                payload = {
                    "symbol": symbol,
                    "as_of": as_of.isoformat(),
                    "depth": depth,
                    "trigger": trigger,
                    "candidate_scope_ids": list(candidate_scope_ids),
                    "evidence": evidence.to_dict(),
                }
                task_id = self.ledger.enqueue(
                    kind=COMPANY_TASK_KIND,
                    priority=_priority(symbol=symbol, evidence=evidence, trigger=trigger, depth=depth),
                    scheduled_at_ms=self._now_ms(),
                    now_ms=self._now_ms(),
                    dedup_key=f"{symbol}:{evidence.input_signature}:{depth}:{as_of.date().isoformat()}",
                    partition_key=symbol,
                    resource=COMPANY_RESOURCE,
                    payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    max_attempts=COMPANY_TASK_MAX_ATTEMPTS,
                )
                if task_id is None:
                    skipped.append({"symbol": symbol, "reason": "already_queued_or_analyzed"})
                else:
                    enqueued.append(
                        {
                            "symbol": symbol,
                            "task_id": task_id,
                            "input_signature": evidence.input_signature,
                            "depth": depth,
                        }
                    )
        return {"enqueued": enqueued, "skipped": skipped, "errors": errors}

    def _handle_task(self, task: dict, *, heartbeat: Callable[[], bool]) -> str:
        started = time.monotonic()
        try:
            raw = json.loads(str(task.get("payload") or "{}"))
            evidence = CompanyEvidenceSnapshot.from_mapping(raw.get("evidence") or {})
            if evidence is None:
                raise ValueError("invalid company evidence task payload")
            request = CompanyMicroAnalysisRequest(
                symbol=evidence.symbol,
                as_of=str(raw.get("as_of") or evidence.as_of),
                evidence=evidence,
                depth=str(raw.get("depth") or "screen"),
                trigger=str(raw.get("trigger") or "scheduled"),
                candidate_scope_ids=tuple(raw.get("candidate_scope_ids") or ()),
                input_refs={"task_id": task.get("id")},
            )
            heartbeat()
            result = run_company_micro_analysis(
                request,
                analyst=self._analyst(),
                repository=self.brief_store,
            )
            latency_ms = max(0, round((time.monotonic() - started) * 1000))
            if result.error_code:
                retry = _company_failure_retry_metadata(task, now_ms=self._now_ms())
                self._record_run(
                    symbol=evidence.symbol,
                    as_of=request.as_of,
                    status="error",
                    trigger=request.trigger,
                    depth=request.depth,
                    input_signature=evidence.input_signature,
                    error={"code": result.error_code, "message": result.error_message},
                    latency_ms=latency_ms,
                    queue_task_id=task.get("id"),
                    written=False,
                    **retry,
                )
                raise RetryableError(f"{result.error_code}: {result.error_message}")
            task_status = "success" if result.written else "unchanged"
            # A completed analyst call is a real recovery even when the
            # repository already owns an equivalent brief. Record it as a
            # successful run with ``written=False`` so it clears a preceding
            # latest_failure without pretending a new brief was emitted.
            self._record_run(
                symbol=evidence.symbol,
                as_of=request.as_of,
                status="success",
                trigger=request.trigger,
                depth=request.depth,
                input_signature=evidence.input_signature,
                brief_ref=result.brief_ref,
                latency_ms=latency_ms,
                queue_task_id=task.get("id"),
                queue_attempt=task.get("attempts"),
                queue_max_attempts=task.get("max_attempts"),
                written=result.written,
            )
            heartbeat()
            if result.written and self.on_brief_written is not None:
                try:
                    self.on_brief_written(
                        {"symbol": evidence.symbol, "brief_ref": result.brief_ref, "as_of": request.as_of}
                    )
                except Exception:  # noqa: BLE001 - downstream retrigger is advisory
                    self.log.warning("company brief downstream retrigger failed for %s", evidence.symbol)
            return json.dumps(
                {"status": task_status, "symbol": evidence.symbol, "brief_ref": result.brief_ref},
                ensure_ascii=False,
                sort_keys=True,
            )
        except RetryableError:
            raise
        except Exception as exc:
            symbol = str(locals().get("raw", {}).get("symbol") or "unknown")
            self._record_run(
                symbol=symbol,
                as_of=datetime.now(timezone.utc).isoformat(),
                status="error",
                trigger="task",
                depth="screen",
                input_signature="unknown",
                error={"code": exc.__class__.__name__, "message": str(exc)[:500]},
                queue_task_id=task.get("id"),
                queue_attempt=task.get("attempts"),
                queue_max_attempts=task.get("max_attempts"),
                written=False,
            )
            raise

    def _record_run(
        self,
        *,
        symbol: str,
        as_of: str,
        status: str,
        trigger: str,
        depth: str,
        input_signature: str,
        brief_ref: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        latency_ms: int | None = None,
        queue_task_id: int | None = None,
        queue_attempt: int | None = None,
        queue_max_attempts: int | None = None,
        retry_attempt: int | None = None,
        retry_delay_seconds: int | None = None,
        next_retry_at: str | None = None,
        written: bool | None = None,
    ) -> None:
        error_payload = dict(error or {})
        error_code = str(error_payload.get("code") or "").strip() or None
        error_message = str(error_payload.get("message") or "").strip() or None
        self.run_store.append(
            {
                "schema_version": 2,
                "run_id": _company_run_id(
                    symbol=symbol,
                    as_of=as_of,
                    input_signature=input_signature,
                    task_id=queue_task_id,
                    attempt=queue_attempt,
                ),
                "symbol": symbol,
                "venue": venue_of(symbol),
                "as_of": as_of,
                "status": status,
                "trigger": trigger,
                "depth": depth,
                "input_signature": input_signature,
                "brief_ref": dict(brief_ref or {}),
                "error": error_payload,
                "error_code": error_code,
                "error_message": error_message,
                "latency_ms": latency_ms,
                "queue_task_id": queue_task_id,
                "queue_attempt": queue_attempt,
                "queue_max_attempts": queue_max_attempts,
                "retry_attempt": retry_attempt,
                "retry_delay_seconds": retry_delay_seconds,
                "next_retry_at": next_retry_at,
                "written": written,
                "agent_provider": getattr(self._analyst_or_none(), "provider", None),
                "agent_model": getattr(self._analyst_or_none(), "model", None),
            }
        )

    def _provider(self) -> object:
        if self.provider is None:
            self.provider = build_company_evidence_provider(self.config_dir, state_dir=self.state_dir)
        return self.provider

    def _analyst_or_none(self) -> object | None:
        return self.analyst

    def _analyst(self) -> object:
        if self.analyst is None:
            from trader.agent.company_micro import LlmCompanyMicroAnalyst

            self.analyst = LlmCompanyMicroAnalyst()
        return self.analyst

    def _now_ms(self) -> int:
        return int(self.now_fn() * 1000)


def _priority(*, symbol: str, evidence: CompanyEvidenceSnapshot, trigger: str, depth: str) -> int:
    del symbol
    if trigger in {"position", "sticky"}:
        return 10
    if any(item.kind in {"filing", "earnings_calendar", "guidance"} for item in evidence.items):
        return 20
    if trigger in {"fresh_news", "challenger"}:
        return 30
    return 60 if depth == "deep" else 40


def _enabled(config_dir: Path) -> bool:
    if os.getenv("CASYS_COMPANY_MICRO_ANALYST_ENABLED", "1") == "0":
        return False
    return bool(_load_yaml_mapping(config_dir / "company_intelligence.yaml").get("enabled", True))


def _configured_concurrency(config_dir: Path) -> int:
    raw = _load_yaml_mapping(config_dir / "company_intelligence.yaml").get(
        "research_concurrency", DEFAULT_CONCURRENCY
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY


def _load_yaml_mapping(path: Path) -> dict[Any, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _unique_symbols(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class _NullProvider:
    def collect(self, *, symbol: str, as_of: str) -> None:
        del symbol, as_of
        return None


__all__ = [
    "COMPANY_RESOURCE",
    "COMPANY_TASK_KIND",
    "CompanyIntelligenceRuntime",
    "build_company_evidence_provider",
    "discover_company_symbols",
]
