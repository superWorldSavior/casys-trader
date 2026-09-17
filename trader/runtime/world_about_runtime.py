"""Event-driven forward ABOUT writer: fresh briefs/notes → artifacts + relations.

Chained on the LLM runners' write callbacks (news macro briefs, company
briefs): named items take an unthrottled fast path (see
``AboutWriterService``), while throttled full sweeps catch up everything the
callbacks missed (boot state, failures, graph off→on). Every sweep reloads its
inputs fresh and constructs its own stores — including fast paths, whose
ontology attestation still derives from FULL inputs — so concurrent publishers
converge on the same extended ontology tip and no store instance is shared
across threads.

Shadow lane, fail-open everywhere: a sweep that raises records an error report
and never propagates into the LLM runner threads.

A supersede observed at batch time (or caused by a fast sweep) forces an
immediate full sweep past the throttle, so new structural heads are picked
up instead of waiting for the cooldown. ABOUT relations pin the stable
family id and are never re-stamped.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ABOUT_THREAD_NAME = "world-about-forward"
FORWARD_FLAG = "CASYS_WORLD_ABOUT_FORWARD_ENABLED"
MIN_INTERVAL_FLAG = "CASYS_WORLD_ABOUT_FORWARD_MIN_INTERVAL_S"
FULL_SWEEP_FLAG = "CASYS_WORLD_ABOUT_FULL_SWEEP_INTERVAL_S"
DEFAULT_MIN_INTERVAL_S = 300.0
DEFAULT_FULL_SWEEP_INTERVAL_S = 21600.0


@dataclass(frozen=True)
class _PendingSweep:
    reason: str
    symbols: tuple[str, ...] = ()
    brief_ids: tuple[str, ...] = ()


def _seconds_flag(override: float | None, flag: str, default: float, log: object = None) -> float:
    if override is not None:
        return max(0.0, float(override))
    raw = os.getenv(flag, str(default))
    try:
        return max(0.0, float(raw))
    except ValueError:
        _log_warning(log, "[world_about] invalid %s=%r, using default %s", flag, raw, default)
        return default


def _merge_pending(items: Sequence[_PendingSweep]) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """Union queued items. An item without identity requests a full sweep."""

    symbols: dict[str, None] = {}
    brief_ids: dict[str, None] = {}
    full_requested = False
    for item in items:
        if not item.symbols and not item.brief_ids:
            full_requested = True
        for symbol in item.symbols:
            cleaned = str(symbol).strip()
            if cleaned:
                symbols.setdefault(cleaned, None)
        for brief_id in item.brief_ids:
            cleaned = str(brief_id).strip()
            if cleaned:
                brief_ids.setdefault(cleaned, None)
    return tuple(symbols), tuple(brief_ids), full_requested


def sweep_about_forward(
    *,
    config_dir: str | Path,
    state_dir: str | Path,
    briefs_root: str | Path | None = None,
    now: datetime | None = None,
    only_symbols: Collection[str] | None = None,
    only_brief_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    """One forward sweep: bridge + append + link news notes and company briefs.

    Same recipe as the backfill ``--apply`` path, on fresh inputs. Raises on
    hard errors; a missing notes db only skips the news side (explicit flag).

    With ``only_symbols``/``only_brief_ids`` the sweep is a fast path: the
    ontology attestation still derives from FULL inputs (convergent tip, never
    a divergent revision), but only the named items are bridged. Anything named
    but unwritable lands in ``skipped_symbols``/``skipped_brief_ids`` — never
    silent.
    """

    from trader.application.world_model.about_writer import AboutWriterService
    from trader.application.world_model.issuer_registry import build_issuer_registry
    from trader.application.world_model.ontology_bootstrap import WorldOntologyAttestation
    from trader.application.world_model.world_scope_resolver import WorldScopeResolver
    from trader.infrastructure.files.company_briefs import load_company_brief_file, load_company_briefs
    from trader.infrastructure.files.family_catalog_config import load_family_catalog
    from trader.infrastructure.state_db.fundamental_item_store import symbol_storage_key
    from trader.infrastructure.state_db.situation_memory_store import (
        ensure_notes_schema,
        read_situation_notes,
        read_situation_notes_by_brief_ids,
    )
    from trader.infrastructure.state_db.world_graph_store import WorldGraphStore
    from trader.infrastructure.state_db.world_knowledge_store import WorldKnowledgeStore

    started = time.monotonic()
    fast = only_symbols is not None or only_brief_ids is not None
    wanted_symbols = sorted({str(s).strip() for s in (only_symbols or ()) if str(s).strip()})
    wanted_briefs = sorted({str(b).strip() for b in (only_brief_ids or ()) if str(b).strip()})
    skipped_symbols: dict[str, str] = {}
    skipped_briefs: dict[str, str] = {}
    state_path = Path(state_dir)
    config_path = Path(config_dir)
    resolved_briefs = Path(briefs_root) if briefs_root is not None else state_path / "company_intelligence"
    mapping = WorldScopeResolver.load(config_path).mapping
    catalog = load_family_catalog(config_path)
    corpus = load_company_briefs(resolved_briefs)
    registry = build_issuer_registry(corpus.briefs, mapping).registry
    notes_db = state_path / "situation_memory.db"
    # Boot ordering: the sweep may run before the first news tick, so it
    # asserts the schema it reads instead of assuming the tick migrated it.
    ensure_notes_schema(notes_db)

    if fast:
        if only_brief_ids is not None:
            try:
                notes = read_situation_notes_by_brief_ids(notes_db, wanted_briefs)
            except FileNotFoundError:
                notes = []
                notes_source = "missing"
            else:
                notes_source = "read"
            seen_briefs = {str(note.get("brief_id")) for note in notes}
            for brief_id in wanted_briefs:
                if brief_id not in seen_briefs:
                    skipped_briefs[brief_id] = (
                        "no_notes" if notes_source == "read" else "notes_source_missing"
                    )
        else:
            notes = []
            notes_source = "not_requested"
        company_briefs: dict[str, Any] = {}
        if only_symbols is not None:
            current = resolved_briefs if resolved_briefs.name == "current" else resolved_briefs / "current"
            for symbol in wanted_symbols:
                try:
                    path = current / f"{symbol_storage_key(symbol)}.json"
                except ValueError:
                    skipped_symbols[symbol] = "invalid_symbol"
                    continue
                brief = load_company_brief_file(path)
                if brief is None:
                    skipped_symbols[symbol] = "brief_file_missing"
                    continue
                company_briefs[brief.symbol] = brief
        bridge_registry = build_issuer_registry(company_briefs, mapping).registry
        bound = {entry.symbol.strip().upper() for entry in bridge_registry.entries.values()}
        for symbol in wanted_symbols:
            if symbol not in skipped_symbols and symbol.strip().upper() not in bound:
                skipped_symbols[symbol] = "unbound"
    else:
        try:
            notes = read_situation_notes(notes_db)
        except FileNotFoundError:
            notes = []
            notes_source = "missing"
        else:
            notes_source = "read"
        company_briefs = dict(corpus.briefs)
        bridge_registry = registry

    graph_store = WorldGraphStore(state_path / "world_model.db")
    try:
        attestation = WorldOntologyAttestation(
            graph_store, mapping, issuer_registry=registry, family_catalog=catalog
        )
        writer = AboutWriterService(
            graph=graph_store,
            knowledge=WorldKnowledgeStore(state_path / "world_knowledge"),
            ontology=attestation,
        )
        news = writer.write_news(notes, mapping=mapping, now=now)
        company = writer.write_company(company_briefs, registry=bridge_registry, catalog=catalog)
    finally:
        graph_store.close()

    return {
        "status": "ok",
        "mode": "fast" if fast else "full",
        "revision_id": news.revision_id,
        "company_revision_id": company.revision_id,
        "notes_source": notes_source,
        "notes": len(notes),
        "company_briefs": len(company_briefs),
        "skipped_brief_files": len(corpus.skipped_files),
        "skipped_symbols": skipped_symbols,
        "skipped_brief_ids": skipped_briefs,
        "news": news.to_dict(),
        "company": company.to_dict(),
        "elapsed_s": time.monotonic() - started,
    }


class AboutForwardRunner:
    """Single-flight coalescing sweeper. A crash here cannot raise into LLM threads."""

    def __init__(
        self,
        *,
        config_dir: str | Path,
        state_dir: str | Path,
        briefs_root: str | Path | None = None,
        min_interval_s: float | None = None,
        full_sweep_interval_s: float | None = None,
        clock: Callable[[], datetime] | None = None,
        now_fn: Callable[[], float] | None = None,
        logger: object | None = None,
        thread_name: str = ABOUT_THREAD_NAME,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.state_dir = Path(state_dir)
        self.briefs_root = None if briefs_root is None else Path(briefs_root)
        self.log = logger or logging.getLogger("casys-trader")
        self.min_interval_s = _seconds_flag(
            min_interval_s, MIN_INTERVAL_FLAG, DEFAULT_MIN_INTERVAL_S, self.log
        )
        self.full_sweep_interval_s = _seconds_flag(
            full_sweep_interval_s, FULL_SWEEP_FLAG, DEFAULT_FULL_SWEEP_INTERVAL_S, self.log
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._now_fn = now_fn or time.monotonic
        self.thread_name = str(thread_name).strip() or ABOUT_THREAD_NAME
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending: list[_PendingSweep] = []
        self._stopping = False
        self._last_report: dict[str, Any] = {}
        self._last_full_sweep_at: float | None = None
        self._last_full_revision_id: str | None = None
        self._status_path = self.state_dir / "world_about_runner_status.json"
        self._started_at = datetime.now(timezone.utc).isoformat()

    def trigger(
        self,
        *,
        reason: str = "briefs_written",
        symbols: Collection[str] = (),
        brief_ids: Collection[str] = (),
    ) -> dict[str, Any]:
        """Queue named items (fast path) or a full sweep (no items) and return.

        Item triggers append to a queue — every named item is eventually
        written, never coalesced away. A trigger without identity requests a
        full catch-up sweep.
        """

        if os.getenv(FORWARD_FLAG, "1") == "0":
            return {"triggered": False, "reason": "disabled"}
        snapshot = _PendingSweep(
            reason=str(reason),
            symbols=tuple(symbols),
            brief_ids=tuple(brief_ids),
        )
        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            if self._thread is not None and self._thread.is_alive():
                self._pending.append(snapshot)
                return {"triggered": False, "reason": "queued"}
            thread = threading.Thread(
                target=self._run_loop,
                args=(snapshot,),
                daemon=True,
                name=self.thread_name,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:  # noqa: BLE001 - startup cannot affect trading
                self._thread = None
                report = {
                    "triggered": False,
                    "reason": "thread_start_error",
                    "error": f"{type(exc).__name__}:{exc}",
                }
                self._record_failure(report)
                return report
        return {"triggered": True, "reason": snapshot.reason, "_thread": thread}

    def status(self) -> dict[str, Any]:
        with self._lock:
            symbols, brief_ids, _ = _merge_pending(self._pending)
            return {
                **copy.deepcopy(self._last_report),
                "running": self._thread is not None and self._thread.is_alive(),
                "pending": bool(self._pending),
                "pending_symbols": len(symbols),
                "pending_brief_ids": len(brief_ids),
                "stopping": self._stopping,
                "last_full_revision_id": self._last_full_revision_id,
            }

    def stop(self) -> None:
        """Request shutdown; a blocking sweep is never force-killed."""

        with self._lock:
            self._stopping = True
            self._pending = []
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=1.0)
            except Exception as exc:  # noqa: BLE001 - shutdown remains best effort
                self._record_failure(
                    {
                        "status": "partial",
                        "stage": "stop",
                        "error": f"{type(exc).__name__}:{exc}",
                    }
                )

    def _record_failure(self, report: dict[str, Any]) -> None:
        with self._lock:
            self._last_report = dict(report)
        self._write_status_file()

    def _record_report(self, report: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._last_report = dict(report)
        self._write_status_file()
        return report

    def _write_status_file(self) -> None:
        """Best-effort runner status for operators. Never raises."""

        try:
            with self._lock:
                snapshot = copy.deepcopy(self._last_report)
            payload = {
                "pid": os.getpid(),
                "status": snapshot.get("status", "unknown"),
                "started_at": self._started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "last": snapshot,
            }
            self._status_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001 - observability never breaks sweeps
            pass

    def _run_sweep(
        self,
        *,
        reason: str,
        symbols: tuple[str, ...],
        brief_ids: tuple[str, ...],
        full: bool,
    ) -> dict[str, Any]:
        try:
            if full:
                report = sweep_about_forward(
                    config_dir=self.config_dir,
                    state_dir=self.state_dir,
                    briefs_root=self.briefs_root,
                    now=self.clock(),
                )
            else:
                report = sweep_about_forward(
                    config_dir=self.config_dir,
                    state_dir=self.state_dir,
                    briefs_root=self.briefs_root,
                    now=self.clock(),
                    only_symbols=symbols if symbols else None,
                    only_brief_ids=brief_ids if brief_ids else None,
                )
        except Exception as exc:  # noqa: BLE001 - forward sweep stays fail-open
            report = {
                "status": "error",
                "mode": "full" if full else "fast",
                "trigger": reason,
                "error": f"{type(exc).__name__}:{exc}",
            }
            _log_warning(self.log, "[world_about_forward] sweep failed: %s", report["error"])
        else:
            report["trigger"] = reason
            if full and report.get("status") == "ok":
                revision = report.get("revision_id") or report.get("company_revision_id")
                if revision:
                    self._last_full_revision_id = str(revision)
        return self._record_report(report)

    def _live_tip_changed(self) -> bool:
        """True when the published head moved past our last full sweep. Fail-open."""

        if self._last_full_revision_id is None:
            return False
        db_path = self.state_dir / "world_model.db"
        if not db_path.is_file():
            return False
        try:
            from trader.application.world_model.ontology_service import WorldOntologyResolver
            from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

            store = WorldGraphStore(db_path)
            try:
                # Liveness is wall-clock by design: the sweep clock may be
                # frozen (tests) or stale, but the probe must see the live head.
                head_id = WorldOntologyResolver(store).live_head_revision_id(
                    at=datetime.now(timezone.utc)
                )
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - liveness probe never breaks sweeps
            _log_warning(self.log, "[world_about_forward] tip probe failed: %s", exc)
            return False
        return head_id is not None and head_id != self._last_full_revision_id

    def _report_advanced_tip(self, report: dict[str, Any]) -> bool:
        if self._last_full_revision_id is None or report.get("status") != "ok":
            return False
        written = {report.get("revision_id"), report.get("company_revision_id")} - {None}
        return bool(written - {self._last_full_revision_id})

    def _sweep_batch(self, batch: list[_PendingSweep]) -> dict[str, Any]:
        symbols, brief_ids, full_requested = _merge_pending(batch)
        reason = batch[-1].reason if batch else "briefs_written"
        now = self._now_fn()
        tip_changed = self._live_tip_changed()
        full_due = (
            full_requested
            or self._last_full_sweep_at is None
            or now - self._last_full_sweep_at >= self.full_sweep_interval_s
            or tip_changed
        )
        full_throttled = (
            self._last_full_sweep_at is not None
            and now - self._last_full_sweep_at < self.min_interval_s
            and not tip_changed
        )
        if full_due and not full_throttled:
            self._last_full_sweep_at = now
            return self._run_sweep(reason=reason, symbols=(), brief_ids=(), full=True)
        if symbols or brief_ids:
            report = self._run_sweep(reason=reason, symbols=symbols, brief_ids=brief_ids, full=False)
            if full_due:
                report["full_deferred"] = "throttled"
                self._record_report(report)
            if self._report_advanced_tip(report):
                self._last_full_sweep_at = self._now_fn()
                return self._run_sweep(
                    reason=f"{reason}+tip_advanced", symbols=(), brief_ids=(), full=True
                )
            return report
        skipped = {
            "status": "skipped",
            "reason": "throttled",
            "trigger": reason,
            "min_interval_s": self.min_interval_s,
        }
        return self._record_report(skipped)

    def _run_loop(self, snapshot: _PendingSweep) -> None:
        batch = [snapshot]
        try:
            while True:
                try:
                    self._sweep_batch(batch)
                except Exception as exc:  # noqa: BLE001 - one sweep never kills the loop
                    self._record_failure(
                        {
                            "status": "error",
                            "trigger": batch[-1].reason if batch else "briefs_written",
                            "error": f"{type(exc).__name__}:{exc}",
                        }
                    )
                with self._lock:
                    if self._stopping:
                        return
                    batch = list(self._pending)
                    self._pending = []
                    if not batch:
                        return
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None


def _log_warning(logger: object, message: str, *args: object) -> None:
    warning = getattr(logger, "warning", None)
    if callable(warning):
        warning(message, *args)


__all__ = [
    "ABOUT_THREAD_NAME",
    "DEFAULT_FULL_SWEEP_INTERVAL_S",
    "DEFAULT_MIN_INTERVAL_S",
    "FORWARD_FLAG",
    "FULL_SWEEP_FLAG",
    "MIN_INTERVAL_FLAG",
    "AboutForwardRunner",
    "sweep_about_forward",
]
