"""Fail-open single-flight worker for source-only World macro collection.

Composition root: operator configs, typed adapters, JSONL store, and the
application pipeline. Fetch stays out of ``run_cycle`` and out of the World
Model bar-capture worker. Adapter budgets (24h cooldown, Retry-After, one
retry max) are honored by the ports; this module does not add a second loop.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from trader.domain.world_macro import MacroScope, MacroSourceRegistry


MACRO_LANE_IDENTITY = "context.v2.macro_source.v1"
MACRO_THREAD_NAME = "world-macro-source-only"
GRAPH_V3_FLAG = "CASYS_WORLD_MODEL_GRAPH_V3_ENABLED"
_BRIDGE_KEY = "macro_graph_bridge.v1"


@dataclasses.dataclass(frozen=True)
class _PendingCollect:
    now: datetime
    reason: str


@dataclasses.dataclass(frozen=True)
class WorldMacroRuntimeBundle:
    runner: WorldMacroBackgroundRunner
    store: object
    mapping: object
    lane_identity: str
    budgets: object


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class WorldMacroBackgroundRunner:
    """Coalescing daemon thread. A crash here cannot raise into the trader loop."""

    def __init__(
        self,
        *,
        collect_fn: Callable[..., object],
        logger: object | None = None,
        thread_name: str = MACRO_THREAD_NAME,
    ) -> None:
        self.collect_fn = collect_fn
        self.log = logger or logging.getLogger("casys-trader")
        self.thread_name = str(thread_name).strip() or MACRO_THREAD_NAME
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending: _PendingCollect | None = None
        self._stopping = False
        self._last_report: dict[str, object] = {}

    def trigger(self, *, now: datetime, reason: str = "post_cycle") -> dict[str, object]:
        """Queue latest cutoff and return without waiting for provider I/O."""

        try:
            snapshot = _PendingCollect(now=_utc(now), reason=str(reason))
        except Exception as exc:  # noqa: BLE001 - snapshot freeze is fail-open
            report = {
                "triggered": False,
                "reason": "snapshot_error",
                "error": f"{type(exc).__name__}:{exc}",
            }
            self._record_failure(report)
            return report

        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            if self._thread is not None and self._thread.is_alive():
                self._pending = snapshot
                return {"triggered": False, "reason": "queued_latest"}
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

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                **copy.deepcopy(self._last_report),
                "running": self._thread is not None and self._thread.is_alive(),
                "pending": self._pending is not None,
                "stopping": self._stopping,
            }

    def stop(self) -> None:
        """Request shutdown; a blocking provider is never force-killed."""

        with self._lock:
            self._stopping = True
            self._pending = None
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

    def _run_loop(self, snapshot: _PendingCollect) -> None:
        current = snapshot
        try:
            while True:
                report: dict[str, object] = {
                    "status": "ok",
                    "reason": current.reason,
                    "as_of": current.now.isoformat(),
                    "lane_identity": MACRO_LANE_IDENTITY,
                    "errors": [],
                }
                try:
                    payload = self.collect_fn(now=current.now, reason=current.reason)
                    if isinstance(payload, Mapping):
                        report.update(copy.deepcopy(dict(payload)))
                except Exception as exc:  # noqa: BLE001 - worker crash stays in the shadow lane
                    self._background_error(report, stage="collect", error=exc)
                    report["status"] = "partial"
                if report.get("errors"):
                    report["status"] = "partial"
                try:
                    completed_report = copy.deepcopy(report)
                except Exception as exc:  # noqa: BLE001 - exotic collaborator output stays isolated
                    self._background_error(report, stage="report_copy", error=exc)
                    completed_report = {
                        "status": "partial",
                        "reason": current.reason,
                        "as_of": current.now.isoformat(),
                        "lane_identity": MACRO_LANE_IDENTITY,
                        "errors": list(report.get("errors") or []),
                    }

                with self._lock:
                    self._last_report = completed_report
                    if self._stopping or self._pending is None:
                        return
                    current = self._pending
                    self._pending = None
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _background_error(self, report: dict[str, object], *, stage: str, error: Exception) -> None:
        item = {"stage": stage, "error": f"{type(error).__name__}:{error}"}
        errors = report.get("errors")
        if isinstance(errors, list):
            errors.append(item)
        self._warn(item)

    def _record_failure(self, report: dict[str, object]) -> None:
        self._last_report = copy.deepcopy(report)
        self._warn(report)

    def _warn(self, payload: object) -> None:
        try:
            warning = getattr(self.log, "warning", None)
            if callable(warning):
                warning("[world_macro_source_only] %s", payload)
        except Exception:
            pass


def graph_v3_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(GRAPH_V3_FLAG, "0")).strip() == "1"


def collection_scopes(bundle: object) -> tuple[MacroScope, ...]:
    """Unique canonical scopes from the registry and operator mapping."""

    seen: dict[tuple[str, str], MacroScope] = {}

    def add(kind: object, entity_id: object) -> None:
        scope = MacroScope(kind=str(kind), entity_id=str(entity_id))
        seen.setdefault((scope.kind, scope.entity_id), scope)

    add("world", "market")
    registry = getattr(bundle, "registry", None)
    for entry in getattr(registry, "entries", ()) or ():
        canonical = getattr(entry, "canonical_scope", None)
        if canonical is None:
            continue
        add(getattr(canonical, "kind", None), getattr(canonical, "entity_id", None))
    mapping = getattr(bundle, "scope_mapping", None)
    for mapping_entry in getattr(mapping, "entries", ()) or ():
        for attr in ("venue", "country", "region", "world"):
            ref = getattr(mapping_entry, attr, None)
            if ref is None:
                continue
            add(getattr(ref, "kind", None), getattr(ref, "entity_id", None))
    return tuple(sorted(seen.values(), key=lambda item: (item.kind, item.entity_id)))


def collect_world_macro(
    *,
    now: datetime,
    reason: str,
    pipeline: object,
    registry: MacroSourceRegistry | object,
    sources: Mapping[str, object],
    scopes: Sequence[MacroScope],
    budgets: object | None = None,
) -> dict[str, object]:
    """Run one sequential collect per scope. Provider missingness stays on the pipeline."""

    if budgets is not None and (
        getattr(budgets, "fetch_in_run_cycle", False)
        or getattr(budgets, "fetch_in_world_capture_worker", False)
    ):
        raise ValueError("source-only macro collection cannot fetch in run_cycle or world capture worker")
    cutoff = _utc(now)
    report: dict[str, object] = {
        "status": "ok",
        "reason": reason,
        "as_of": cutoff.isoformat(),
        "lane_identity": MACRO_LANE_IDENTITY,
        "authority": "shadow_only",
        "decision_effect": "none",
        "runs": [],
        "errors": [],
    }
    collect = getattr(pipeline, "collect")
    runs = report["runs"]
    errors = report["errors"]
    assert isinstance(runs, list)
    assert isinstance(errors, list)
    for scope in scopes:
        try:
            run = collect(
                scope=scope,
                cutoff_at=cutoff,
                registry=registry,
                sources=sources,
                observed_at=cutoff,
            )
            runs.append(
                {
                    "scope": scope.to_dict(),
                    "status": getattr(run, "status", "unknown"),
                    "run_id": getattr(run, "run_id", None),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one scope cannot abort the worker
            errors.append({"scope": scope.to_dict(), "error": f"{type(exc).__name__}:{exc}"})
    if errors:
        report["status"] = "partial"
    return report


def wire_world_macro_runtime(
    *,
    config_dir: str | Path,
    state_dir: str | Path,
    logger: object | None = None,
    transport: object | None = None,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> WorldMacroRuntimeBundle:
    """Build store, ports, pipeline, and coalescing worker. No fetch at wire time."""

    from trader.application.world_model.macro_pipeline import MacroWorldPipeline
    from trader.infrastructure.market_sources.world_macro.series import (
        build_macro_source_ports,
        load_world_macro_operator_configs,
        urllib_macro_transport,
    )
    from trader.infrastructure.state_db.world_macro_store import WorldMacroStore

    operator = load_world_macro_operator_configs(config_dir=Path(config_dir))
    if operator.budgets.fetch_in_run_cycle or operator.budgets.fetch_in_world_capture_worker:
        raise ValueError("source-only macro collection cannot fetch in run_cycle or world capture worker")
    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
    ports = build_macro_source_ports(
        operator,
        transport=transport or urllib_macro_transport,
        clock=resolved_clock,
        sleeper=sleeper,
    )
    store = WorldMacroStore(Path(state_dir) / "world_macro", clock=resolved_clock)
    pipeline = MacroWorldPipeline(
        history=store,
        ledger=store,
        reader=store,
        policy=operator.policy,
    )
    scopes = collection_scopes(operator)
    graph_enabled = graph_v3_enabled()
    graph_store = None
    if graph_enabled:
        from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

        graph_store = WorldGraphStore(Path(state_dir) / "world_model.db", clock=resolved_clock)

    def collect_fn(*, now: datetime, reason: str) -> dict[str, object]:
        report = collect_world_macro(
            now=now,
            reason=reason,
            pipeline=pipeline,
            registry=operator.registry,
            sources=ports,
            scopes=scopes,
            budgets=operator.budgets,
        )
        if graph_store is None:
            return report
        try:
            _reconcile_macro_graph_bridge(
                now=now,
                macro_store=store,
                graph_store=graph_store,
                mapping=operator.scope_mapping,
                report=report,
            )
        except Exception as exc:  # noqa: BLE001 - graph bridge stays fail-open
            errors = report.get("errors")
            if isinstance(errors, list):
                errors.append({"stage": "graph_bridge", "error": f"{type(exc).__name__}:{exc}"})
            report["status"] = "partial"
        return report

    runner = WorldMacroBackgroundRunner(
        collect_fn=collect_fn,
        logger=logger,
        thread_name=MACRO_THREAD_NAME,
    )
    return WorldMacroRuntimeBundle(
        runner=runner,
        store=store,
        mapping=operator.scope_mapping,
        lane_identity=MACRO_LANE_IDENTITY,
        budgets=operator.budgets,
    )


def _reconcile_macro_graph_bridge(
    *,
    now: datetime,
    macro_store: object,
    graph_store: object,
    mapping: object,
    report: dict[str, object],
) -> None:
    from trader.application.world_model.graph_observation_bridge import RegisterMacroObservationKnowledge
    from trader.domain.world_episode import canonical_sha256
    from trader.domain.world_graph import WorldOntologyRevision, WorldOntologyRevisionPublished
    from trader.domain.world_scope import WorldScopeMapping

    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("graph bridge requires WorldScopeMapping")
    revision = None
    list_revisions = getattr(graph_store, "list_revision_events_available_through", None)
    if callable(list_revisions):
        for envelope in list_revisions(now):
            event = getattr(envelope, "event", None)
            if isinstance(event, WorldOntologyRevisionPublished):
                revision = event.revision
    if revision is None:
        revision = WorldOntologyRevision(
            revision_id="market_ontology.v1",
            entities=(),
            structural_relation_refs=(),
            identity_link_refs=(),
            scope_mapping_id=mapping.mapping_id,
            scope_mapping_hash=mapping.content_sha256,
        )
    use_case = RegisterMacroObservationKnowledge(
        scan=macro_store,
        graph=graph_store,
        bridge=graph_store,
        scope_mapping=mapping,
        structural_revision=revision,
        bridge_key=_BRIDGE_KEY,
    )
    request_id = "macro_graph_bridge_request:v1:" + canonical_sha256(
        {"bridge_key": _BRIDGE_KEY, "intent": "activate"}
    )
    use_case.activate(request_id)
    registry = use_case.reconcile(limit=32)
    report["graph_bridge"] = {
        "status": None if registry.active_run is None else registry.active_run.status,
        "version": registry.version,
    }


__all__ = [
    "GRAPH_V3_FLAG",
    "MACRO_LANE_IDENTITY",
    "MACRO_THREAD_NAME",
    "WorldMacroBackgroundRunner",
    "WorldMacroRuntimeBundle",
    "collect_world_macro",
    "collection_scopes",
    "graph_v3_enabled",
    "wire_world_macro_runtime",
]
