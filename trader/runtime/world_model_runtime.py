"""Fail-open background adapter around the World Model application service.

Deterministic capture/predict/mature orchestration lives in
:mod:`trader.application.world_model.service`.  This module keeps the daemon
thread, snapshot freeze, and fail-open wrapping so a shadow worker cannot
raise into the trading cycle.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from trader.application.world_model.scope_mapping_ports import WorldScopeMappingGenerationQuery
from trader.application.world_model.service import (
    DEFAULT_HORIZONS,
    WorldModelService,
    _clone,
    _utc,
)
from trader.runtime.world_macro_runtime import graph_enabled


@dataclasses.dataclass(frozen=True)
class _BackgroundSnapshot:
    episodes: tuple[object, ...]
    bars_by_symbol: object
    now: datetime
    reason: str


class WorldModelBackgroundRunner:
    """Coalescing background wrapper around :class:`WorldModelService`.

    ``trigger`` freezes caller-owned observations and bars, then returns as
    soon as a daemon worker is started or a latest snapshot is queued.  The
    worker always matures old episodes before it captures/predicts the new
    snapshot.  It is strictly shadow maintenance: no scheduler, broker,
    RiskGate, Brain, or decision callback is accepted by this class.
    """

    def __init__(
        self,
        *,
        runtime: WorldModelService,
        logger: object | None = None,
        thread_name: str = "world-model-shadow",
        context_enricher: WorldContextEpisodeEnricher | None = None,
        graph_enricher: WorldGraphEpisodeEnricher | None = None,
        pattern_workflow: object | None = None,
        resource_guard: object | None = None,
    ) -> None:
        self.runtime = runtime
        self.log = logger or logging.getLogger("casys-trader")
        self.thread_name = str(thread_name).strip() or "world-model-shadow"
        self.context_enricher = context_enricher
        self.graph_enricher = graph_enricher
        self.pattern_workflow = pattern_workflow
        self.resource_guard = resource_guard
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending: _BackgroundSnapshot | None = None
        self._stopping = False
        self._last_report: dict[str, object] = {}

    def trigger(
        self,
        *,
        episodes: Iterable[object],
        now: datetime,
        bars_by_symbol: Mapping[str, object] | None = None,
        reason: str = "shadow",
    ) -> dict[str, object]:
        """Freeze input and schedule best-effort maintenance without waiting."""

        try:
            snapshot = _BackgroundSnapshot(
                episodes=tuple(_clone(episode) for episode in episodes),
                bars_by_symbol={} if bars_by_symbol is None else _clone(bars_by_symbol),
                now=_utc(now),
                reason=str(reason),
            )
        except Exception as exc:  # noqa: BLE001 - background capture is fail-open
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
            if self._thread is not None:
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
            except Exception as exc:  # noqa: BLE001 - no worker startup can affect trading
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
            payload: dict[str, object] = {
                **copy.deepcopy(self._last_report),
                "running": self._thread is not None,
                "pending": self._pending is not None,
                "stopping": self._stopping,
            }
        if self.graph_enricher is not None:
            payload["graph"] = graph_status_overlay(wired=True)
        return payload

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
                self._record_failure({"status": "partial", "stage": "stop", "error": f"{type(exc).__name__}:{exc}"})

    def _run_loop(self, snapshot: _BackgroundSnapshot) -> None:
        current = snapshot
        while True:
            report: dict[str, object] = {
                "status": "ok",
                "reason": current.reason,
                "as_of": current.now.isoformat(),
                "errors": [],
            }
            skip_writes = self._apply_resource_budget(report, now=current.now)
            if not skip_writes:
                try:
                    report["mature"] = self.runtime.mature_pending(
                        current.now,
                        bars_by_symbol=_clone(current.bars_by_symbol),
                    )
                except Exception as exc:  # noqa: BLE001 - runtime is shadow-only even if its contract breaks
                    self._background_error(report, stage="mature", error=exc)
                capture_snapshot = current
                if self.context_enricher is not None:
                    try:
                        enriched = tuple(
                            self.context_enricher.enrich(tuple(_clone(episode) for episode in current.episodes))
                        )
                        capture_snapshot = dataclasses.replace(current, episodes=enriched)
                    except Exception as exc:  # noqa: BLE001 - context enrichment is fail-open for market
                        self._background_error(report, stage="context_enrich", error=exc)
                if self.graph_enricher is not None:
                    try:
                        enriched = tuple(
                            self.graph_enricher.enrich(tuple(_clone(episode) for episode in capture_snapshot.episodes))
                        )
                        capture_snapshot = dataclasses.replace(capture_snapshot, episodes=enriched)
                    except Exception as exc:  # noqa: BLE001 - graph enrichment is fail-open for market/context
                        self._background_error(report, stage="graph_enrich", error=exc)
                try:
                    report["capture"] = self._capture_and_predict(capture_snapshot)
                except Exception as exc:  # noqa: BLE001 - still attempt capture after a maturity failure
                    self._background_error(report, stage="capture", error=exc)
                if self.pattern_workflow is not None:
                    try:
                        lifecycle = self.pattern_workflow.run(current.now)
                        to_dict = getattr(lifecycle, "to_dict", None)
                        report["pattern_lifecycle"] = to_dict() if callable(to_dict) else lifecycle
                        lifecycle_status = getattr(lifecycle, "status", None)
                        if lifecycle_status is None and isinstance(report["pattern_lifecycle"], Mapping):
                            lifecycle_status = report["pattern_lifecycle"].get("status")
                        if lifecycle_status == "partial":
                            report["status"] = "partial"
                    except Exception as exc:  # noqa: BLE001 - pattern shadow cannot affect Trader
                        self._background_error(report, stage="pattern_lifecycle", error=exc)
                if report["errors"]:
                    report["status"] = "partial"
            try:
                completed_report = copy.deepcopy(report)
            except Exception as exc:  # noqa: BLE001 - exotic collaborator output stays isolated
                self._background_error(report, stage="report_copy", error=exc)
                completed_report = {
                    "status": "partial",
                    "reason": current.reason,
                    "as_of": current.now.isoformat(),
                    "errors": list(report["errors"]),
                }

            with self._lock:
                self._last_report = completed_report
                if self._stopping or self._pending is None:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return
                current = self._pending
                self._pending = None

    def _apply_resource_budget(self, report: dict[str, object], *, now: datetime) -> bool:
        """Skip capture/training when the last-resort budget is breached. Never raises."""

        if self.resource_guard is None:
            return False
        evaluate = getattr(self.resource_guard, "evaluate", None)
        if not callable(evaluate):
            return False
        try:
            evaluation = evaluate(now=now)
            to_status = getattr(evaluation, "to_status", None)
            report["resource_budget"] = to_status() if callable(to_status) else evaluation
            decision = getattr(evaluation, "decision", evaluation)
            emit_warning = bool(getattr(evaluation, "emit_warning", False))
            allowed = bool(getattr(decision, "allowed", False))
            reason = str(getattr(decision, "reason", "probe_error") or "probe_error")
            if emit_warning:
                payload = {"stage": "resource_budget"}
                if isinstance(report.get("resource_budget"), Mapping):
                    payload.update(dict(report["resource_budget"]))
                else:
                    payload["reason"] = reason
                    payload["status"] = "skipped" if not allowed else "allowed"
                payload["stage"] = "resource_budget"
                self._warn(payload)
            if allowed:
                return False
            report["status"] = "skipped"
            report["reason"] = reason
            return True
        except Exception as exc:  # noqa: BLE001 - unreadable budget is fail-safe skip for shadow writes
            report["status"] = "skipped"
            report["reason"] = "probe_error"
            report["resource_budget"] = {
                "status": "skipped",
                "reason": "probe_error",
                "authority": "shadow_only",
                "decision_effect": "none",
                "error": f"{type(exc).__name__}:{exc}",
            }
            self._warn(
                {
                    "stage": "resource_budget",
                    "status": "skipped",
                    "reason": "probe_error",
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            return True

    def _capture_and_predict(self, snapshot: _BackgroundSnapshot) -> object:
        """Call the canonical service with now; keep legacy doubles without now.

        Signature inspection stays in this runtime adapter.  The application
        service always receives an explicit snapshot clock when it accepts one.
        """

        fn = self.runtime.capture_and_predict
        episodes = tuple(_clone(episode) for episode in snapshot.episodes)
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(episodes, now=snapshot.now)
        for args, kwargs in (
            ((episodes,), {"now": snapshot.now}),
            ((episodes,), {}),
        ):
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
            return fn(*args, **kwargs)
        return fn(episodes, now=snapshot.now)

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
                warning("[world_model_shadow] %s", payload)
        except Exception:
            pass


class WorldContextEpisodeEnricher:
    """Background context companion attachment. Local files only; no network or LLM I/O."""

    def __init__(self, source: object) -> None:
        self.source = source

    def enrich(self, episodes: Sequence[object]) -> tuple[object, ...]:
        from trader.application.world_model.context_capture import attach_world_context
        from trader.domain.world_episode import WorldEpisode

        canonical: list[WorldEpisode] = []
        for episode in episodes:
            if isinstance(episode, WorldEpisode):
                canonical.append(episode)
                continue
            if isinstance(episode, Mapping):
                canonical.append(WorldEpisode.from_dict(episode))
                continue
            to_dict = getattr(episode, "to_dict", None)
            if not callable(to_dict):
                raise TypeError("context enricher requires WorldEpisode records")
            payload = to_dict()
            if not isinstance(payload, Mapping):
                raise TypeError("context enricher requires WorldEpisode records")
            canonical.append(WorldEpisode.from_dict(payload))
        market = tuple(canonical)
        context = attach_world_context(market, self.source)
        return market + tuple(context)


class WorldGraphEpisodeEnricher:
    """Background graph companion attachment. Local files only; no network or LLM I/O."""

    def __init__(self, config: object) -> None:
        self.config = config

    def enrich(self, episodes: Sequence[object]) -> tuple[object, ...]:
        from trader.application.world_model.graph_capture import attach_world_graph
        from trader.domain.world_episode import WorldEpisode

        canonical: list[WorldEpisode] = []
        for episode in episodes:
            if isinstance(episode, WorldEpisode):
                canonical.append(episode)
                continue
            if isinstance(episode, Mapping):
                canonical.append(WorldEpisode.from_dict(episode))
                continue
            to_dict = getattr(episode, "to_dict", None)
            if not callable(to_dict):
                raise TypeError("graph enricher requires WorldEpisode records")
            payload = to_dict()
            if not isinstance(payload, Mapping):
                raise TypeError("graph enricher requires WorldEpisode records")
            canonical.append(WorldEpisode.from_dict(payload))
        attached = tuple(canonical)
        graph = attach_world_graph(attached, self.config)
        return attached + tuple(graph)


class WorldTemporalTraversalAdapter:
    """Runtime adapter: GRAPH-4 projector behind the application traversal port."""

    def enumerate_paths(self, structural, knowledge, root, *, max_depth=None, max_paths=None):
        from trader.application.world_model.graph_ports import WorldGraphPath, WorldGraphPathSet, WorldGraphPathStep
        from trader.infrastructure.graph.world_temporal_networkx import WorldTemporalGraph

        enumerated = WorldTemporalGraph.from_resolved_views(structural, knowledge).enumerate_paths(
            root,
            max_depth=max_depth,
            max_paths=max_paths,
        )
        return WorldGraphPathSet(
            paths=tuple(
                WorldGraphPath(
                    steps=tuple(
                        WorldGraphPathStep(
                            relation_id=step.relation_id,
                            kind=step.kind,
                            family=step.family,
                            direction=step.direction,
                            source_kind=step.source_kind,
                            target_kind=step.target_kind,
                            source_node_id=step.source_node_id,
                            target_node_id=step.target_node_id,
                            freshness_bucket=step.freshness_bucket,
                        )
                        for step in path.steps
                    )
                )
                for path in enumerated.paths
            ),
            status=enumerated.status,
            policy_version=enumerated.policy_version,
        )


def graph_budget_view() -> dict[str, object]:
    from trader.domain.world_feature_contract import (
        GRAPH_PATH_RULE_VERSION,
        GRAPH_WINDOWS_AND_DECAY,
    )
    from trader.domain.world_graph import GRAPH_TRAVERSAL_POLICY_VERSION

    return {
        "max_depth": 4,
        "max_paths_per_root": 32,
        "policy_version": GRAPH_TRAVERSAL_POLICY_VERSION,
        "path_rule_version": GRAPH_PATH_RULE_VERSION,
        "windows_and_decay": dict(GRAPH_WINDOWS_AND_DECAY),
    }


def graph_status_overlay(*, wired: bool, writes: str = "none_until_due_cycle") -> dict[str, object]:
    return {
        "wired": wired,
        "flag": "CASYS_WORLD_MODEL_GRAPH_ENABLED",
        "flag_default": 0,
        "budgets": graph_budget_view(),
        "gaps": {"writes": writes, "cohort_activation": "not_read_from_ledger"},
        "authority": "shadow_only",
        "decision_effect": "none",
        "causal_claim": False,
        "pnl_claim": False,
        "recommendation": "NO_GO",
    }


def _compose_graph_predictors() -> tuple[object, ...]:
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.encoding import world_lane_encoder_profile
    from trader.application.world_model.gru import OnlineGRUWorldChallenger
    from trader.domain.world_feature_contract import (
        GRAPH_FEATURE_CONTRACT_ID,
        GRAPH_GRU_MODEL_IDENTITY,
        GRAPH_MARKOV_MODEL_IDENTITY,
        GRAPH_MODEL_VERSION,
    )

    status = world_lane_encoder_profile("topology_status_only")
    content = world_lane_encoder_profile("graph_content")
    return (
        HierarchicalDirichletWorldBaseline(
            model_id=GRAPH_MARKOV_MODEL_IDENTITY,
            model_version=GRAPH_MODEL_VERSION,
            feature_contract=status.contract,
            feature_mask=status.mask,
            accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_ID}),
        ),
        OnlineGRUWorldChallenger(
            model_id=GRAPH_GRU_MODEL_IDENTITY,
            model_version=GRAPH_MODEL_VERSION,
            feature_contract=content.contract,
            feature_mask=content.mask,
            accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_ID}),
        ),
    )


def compose_world_ontology_attestation(
    *,
    store: object | None,
    config_dir: str | Path | None,
    clock: object | None = None,
) -> object | None:
    """Compose the single committed ontology attestation. Fail-open, no fabricated heads."""

    if store is None or config_dir is None:
        return None
    try:
        from trader.application.world_model.ontology_bootstrap import WorldOntologyAttestation
        from trader.application.world_model.world_scope_resolver import WorldScopeResolver
        from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

        path = getattr(store, "path", None)
        db = getattr(store, "_db", None)
        if db is None and path is None:
            return None
        kwargs: dict[str, object] = {}
        if clock is not None:
            kwargs["clock"] = clock
        graph_store = WorldGraphStore(db if db is not None else path, **kwargs)
        mapping = WorldScopeResolver.load(Path(config_dir)).mapping
        return WorldOntologyAttestation(graph_store, mapping)
    except Exception:  # noqa: BLE001 - missing ontology cannot block market/Trader
        return None


def _compose_graph_capture(
    *,
    store: object | None,
    config_dir: str | Path | None,
    study_cohort_id: str | None = None,
    ontology_attestation: object | None = None,
) -> object | None:
    if store is None or config_dir is None:
        return None
    path = getattr(store, "path", None)
    if path is None and ontology_attestation is None:
        return None
    from trader.application.world_model.graph_capture import WorldGraphCaptureConfig
    from trader.application.world_model.graph_snapshot import WorldGraphSnapshotService
    from trader.application.world_model.world_scope_resolver import WorldScopeResolver
    from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

    resolver = WorldScopeResolver.load(Path(config_dir))
    from trader.application.world_model.ontology_bootstrap import WorldOntologyAttestation

    if isinstance(ontology_attestation, WorldOntologyAttestation):
        graph_store = ontology_attestation.ledger
        attestation = ontology_attestation
    else:
        if path is None:
            return None
        db = getattr(store, "_db", None)
        graph_store = WorldGraphStore(db if db is not None else path)
        attestation = WorldOntologyAttestation(graph_store, resolver.mapping)
    attestation.ensure_published()
    service = WorldGraphSnapshotService(
        ledger=graph_store,
        traversal=WorldTemporalTraversalAdapter(),
        snapshot_ledger=graph_store,
    )
    return WorldGraphCaptureConfig(
        scope_mapping=attestation.mapping,
        snapshot_service=service,
        study_cohort_id=study_cohort_id,
        max_depth=4,
        max_paths=32,
    )


def compose_world_resource_guard(
    *,
    db_path: str | Path,
    config_dir: str | Path,
    clock: object | None = None,
) -> object:
    """Compose the last-resort shadow budget guard. Fail-open to conservative defaults."""

    from trader.application.world_model.resource_budget import (
        WorldResourceBudgetGuard,
        load_world_shadow_resource_budget,
    )
    from trader.domain.world_resource import WorldResourceBudget
    from trader.infrastructure.state_db.world_resource_probe import FilesystemWorldResourceProbe

    try:
        budget = load_world_shadow_resource_budget(config_dir)
    except Exception:  # noqa: BLE001 - missing config cannot disable the last-resort guard
        budget = WorldResourceBudget.conservative_defaults()
    kwargs: dict[str, object] = {
        "budget": budget,
        "probe": FilesystemWorldResourceProbe(db_path),
    }
    if clock is not None:
        kwargs["clock"] = clock
    return WorldResourceBudgetGuard(**kwargs)  # type: ignore[arg-type]


def compose_pattern_shadow_workflow(
    *,
    enabled: bool,
    store: object | None,
    macro_root: str | Path,
) -> object | None:
    """Compose the automatic graph-pattern lifecycle on the world-model ledger.

    The application workflow owns orchestration and invariants. This runtime
    composition root only binds its SQLite read adapters and append-only
    writers. The supplied ``WorldModelStore`` remains the sole owner of the
    shared daemon connection.
    """

    if not enabled or store is None:
        return None
    db = getattr(store, "_db", None)
    db_path = getattr(store, "path", None)
    if db is None or db_path is None:
        raise TypeError("pattern shadow requires a daemon-owned WorldModelStore")

    from trader.application.world_model.pattern_discovery import PatternDiscoveryService
    from trader.application.world_model.pattern_evaluation import PatternEvaluationService
    from trader.application.world_model.pattern_outcome_link import PatternOutcomeLinkService
    from trader.application.world_model.pattern_service import WorldPatternService
    from trader.application.world_model.pattern_shadow_workflow import PatternShadowWorkflow
    from trader.infrastructure.state_db.world_pattern_catalog_query import SqlitePatternCatalogQuery
    from trader.infrastructure.state_db.world_pattern_evaluation_query import SqlitePatternEvaluationSource
    from trader.infrastructure.state_db.world_pattern_formation_query import SqlitePatternFormationSource
    from trader.infrastructure.state_db.world_pattern_outcome_query import SqlitePatternOutcomeLeafQuery
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    pattern_store = WorldPatternStore(db)
    patterns = WorldPatternService(
        hypotheses=pattern_store,
        occurrences=pattern_store,
        availability=pattern_store,
    )
    catalog = SqlitePatternCatalogQuery(db_path)
    return PatternShadowWorkflow(
        cohorts=store,  # type: ignore[arg-type]
        lifecycle=pattern_store,
        discovery=PatternDiscoveryService(SqlitePatternFormationSource(db_path, macro_root=macro_root)),
        patterns=patterns,
        evaluation=PatternEvaluationService(
            catalog=catalog,
            source=SqlitePatternEvaluationSource(db_path, macro_root=macro_root),
        ),
        predictions=store,  # type: ignore[arg-type]
        outcomes=PatternOutcomeLinkService(
            catalog=catalog,
            outcomes=SqlitePatternOutcomeLeafQuery(db_path),
        ),
    )


def compose_local_graph_lanes(
    *,
    enabled: bool | None = None,
    capture: object | None = None,
    predictors: Iterable[object] | None = None,
    store: object | None = None,
    config_dir: str | Path | None = None,
    study_cohort_id: str | None = None,
    ontology_attestation: object | None = None,
) -> tuple[tuple[object, ...], WorldGraphEpisodeEnricher | None]:
    """Compose local graph lanes only when the graph flag is on and capture+predictors exist."""

    try:
        resolved_enabled = graph_enabled() if enabled is None else bool(enabled)
        if not resolved_enabled:
            return (), None
        resolved_capture = (
            capture
            if capture is not None
            else _compose_graph_capture(
                store=store,
                config_dir=config_dir,
                study_cohort_id=study_cohort_id,
                ontology_attestation=ontology_attestation,
            )
        )
        if predictors is None:
            resolved_predictors: tuple[object, ...] = _compose_graph_predictors()
        else:
            resolved_predictors = tuple(predictors)
        if resolved_capture is None or not resolved_predictors:
            return (), None
        return resolved_predictors, WorldGraphEpisodeEnricher(resolved_capture)
    except Exception:  # noqa: BLE001 - graph composition never blocks market/context/Trader
        return (), None


class CallableWorldBarProvider:
    """Adapt a ``get_bars(symbol, lookback, interval)`` callable to the port."""

    def __init__(self, get_bars: object) -> None:
        if not callable(get_bars):
            raise TypeError("bar_provider must be callable")
        self._get_bars = get_bars

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "1h",
    ) -> object:
        return self._get_bars(symbol, lookback=lookback, interval=interval)


class CallableWorldLabeler:
    """Adapt a ``label_horizon`` function or module/object to the application port."""

    def __init__(self, labeler: object) -> None:
        method = getattr(labeler, "label_horizon", None)
        if callable(method):
            self._label_horizon = method
        elif callable(labeler):
            self._label_horizon = labeler
        else:
            raise TypeError("labeler must expose label_horizon or be callable")

    def label_horizon(
        self,
        episode: object,
        bars: object,
        horizon: str,
        *,
        now: datetime,
    ) -> object:
        return self._label_horizon(episode, bars, horizon, now=now)


def _adapt_world_labeler(labeler: object | None) -> object | None:
    if labeler is None:
        return None
    method = getattr(labeler, "label_horizon", None)
    if callable(method):
        return labeler
    return CallableWorldLabeler(labeler)


def _adapt_world_bar_provider(provider: object | None) -> object | None:
    if provider is None:
        return None
    get_bars = getattr(provider, "get_bars", None)
    if callable(get_bars):
        return provider
    return CallableWorldBarProvider(provider)


def _default_scope_resolver() -> object | None:
    try:
        from trader.application.world_model.world_scope_resolver import WorldScopeResolver

        return WorldScopeResolver.load(Path(__file__).resolve().parents[2] / "config")
    except Exception:  # noqa: BLE001 - missing mapping never blocks market shadow
        return None


def compose_world_scope_mapping_reconcile(config_dir: str | Path) -> object:
    """Composition root for universe → WorldScopeMapping reconciliation."""

    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService
    from trader.infrastructure.files.universe_anchors import YamlUniverseAnchorSource
    from trader.infrastructure.files.world_scope_mapping_config import YamlWorldScopeMappingStore
    from trader.infrastructure.market_sources.world_scope_listing import (
        InstrumentListingMetadataAdapter,
        YFinanceListingExchangeLookup,
    )

    return WorldScopeMappingReconcileService(
        universe=YamlUniverseAnchorSource(config_dir),
        listings=InstrumentListingMetadataAdapter(exchange_lookup=YFinanceListingExchangeLookup()),
        store=YamlWorldScopeMappingStore(config_dir),
    )


def build_universe_written_scope_observer(config_dir: str | Path):
    """Fail-open observer. Rotation writes stay authoritative even if mapping fails."""

    def _observe(_symbols=None) -> None:
        try:
            compose_world_scope_mapping_reconcile(config_dir).reconcile(persist=True)
        except Exception:  # noqa: BLE001 - shadow mapping cannot affect trading
            return

    return _observe


def _wire_mapping_generations(
    store: object,
    mapping_generations: WorldScopeMappingGenerationQuery | None,
) -> WorldScopeMappingGenerationQuery | None:
    if mapping_generations is not None:
        return mapping_generations
    load = getattr(store, "load_mapping_generation", None)
    persist = getattr(store, "persist_mapping_generation", None)
    if callable(load) and callable(persist):
        return store
    return None


def _wire_cohort_service(store: object, cohort_service: object | None) -> object | None:
    if cohort_service is not None:
        return cohort_service
    required = ("register", "append_event", "load", "list_slots", "envelope_for")
    if not all(callable(getattr(store, name, None)) for name in required):
        return None
    from trader.application.world_model.cohort_service import WorldCohortService

    return WorldCohortService(repository=store, query=store)  # type: ignore[arg-type]


def _predictor_key(predictor: object) -> tuple[str, str]:
    model_id = str(getattr(predictor, "model_id", "") or "").strip()
    model_version = str(getattr(predictor, "model_version", "") or "").strip()
    if not model_id:
        predictor_type = type(predictor)
        model_id = f"{predictor_type.__module__}.{predictor_type.__qualname__}"
    return model_id, model_version or "unversioned"


def _mint_collecting_cohort_predictors(store: object, cohort_service: object | None) -> tuple[object, ...]:
    """Mint cold lane models only after a proven WorldCohortStarted. Fail-open otherwise."""

    if cohort_service is None:
        return ()
    list_ids = getattr(store, "list_collecting_cohort_ids", None)
    load = getattr(store, "load", None)
    cold_lanes = getattr(cohort_service, "cold_lanes", None)
    if not callable(list_ids) or not callable(load) or not callable(cold_lanes):
        return ()
    from trader.application.world_model.baseline import cold_markov_challenger
    from trader.application.world_model.encoding import world_lane_encoder_profile
    from trader.application.world_model.gru import cold_gru_challenger
    from trader.domain.world_cohort import ModelFamily

    minted: list[object] = []
    for cohort_id in list_ids():
        try:
            cold = cold_lanes(cohort_id)
            cohort = load(cohort_id)
        except Exception:  # noqa: BLE001 - unproven start never blocks market shadow
            continue
        by_id = {lane.lane_id: lane for lane in cohort.manifest.lanes}
        for item in cold:
            lane = by_id.get(item.lane_id)
            if lane is None:
                continue
            try:
                kind = lane.feature_mask_id.rsplit(".v", 1)[0]
                profile = world_lane_encoder_profile(kind)
                kwargs = {
                    "lane": lane,
                    "contract": profile.contract,
                    "mask": profile.mask,
                    "study_cohort_id": item.study_cohort_id,
                    "manifest_sha256": item.manifest_sha256,
                    "started_event_id": item.started_event_id,
                }
                if lane.model_family is ModelFamily.MARKOV:
                    minted.append(cold_markov_challenger(**kwargs))
                elif lane.model_family is ModelFamily.GRU:
                    minted.append(cold_gru_challenger(**kwargs))
            except Exception:  # noqa: BLE001 - a broken lane stays isolated
                continue
    return tuple(minted)


class WorldModelRuntime(WorldModelService):
    """Composition adapter: wrap legacy labeler/bar shapes onto explicit ports."""

    def __init__(
        self,
        *,
        store: object,
        predictor: object | None = None,
        predictors: Iterable[object] | None = None,
        labeler: object | None = None,
        bar_provider: object | None = None,
        horizons: Iterable[str] = DEFAULT_HORIZONS,
        logger: object | None = None,
        lookback: str = "5d",
        run_id: str = "world_shadow.v1",
        cohort_service: object | None = None,
        scope_resolver: object | None = None,
        mapping_generations: WorldScopeMappingGenerationQuery | None = None,
    ) -> None:
        resolved = _wire_cohort_service(store, cohort_service)
        configured = ([predictor] if predictor is not None else []) + list(predictors or ())
        taken = {_predictor_key(item) for item in configured}
        extras: list[object] = []
        for item in _mint_collecting_cohort_predictors(store, resolved):
            key = _predictor_key(item)
            if key in taken:
                continue
            taken.add(key)
            extras.append(item)
        super().__init__(
            store=store,
            predictor=predictor,
            predictors=tuple(predictors or ()) + tuple(extras),
            labeler=_adapt_world_labeler(labeler),
            bar_provider=_adapt_world_bar_provider(bar_provider),
            horizons=horizons,
            logger=logger,
            lookback=lookback,
            run_id=run_id,
            cohort_service=resolved,
            scope_resolver=scope_resolver if scope_resolver is not None else _default_scope_resolver(),
            mapping_generations=_wire_mapping_generations(store, mapping_generations),
        )


WorldModelShadowRuntime = WorldModelRuntime
WorldModelRunner = WorldModelRuntime


__all__ = [
    "DEFAULT_HORIZONS",
    "CallableWorldBarProvider",
    "CallableWorldLabeler",
    "WorldContextEpisodeEnricher",
    "WorldGraphEpisodeEnricher",
    "WorldModelBackgroundRunner",
    "WorldModelRunner",
    "WorldModelRuntime",
    "WorldModelShadowRuntime",
    "WorldTemporalTraversalAdapter",
    "build_universe_written_scope_observer",
    "compose_local_graph_lanes",
    "compose_pattern_shadow_workflow",
    "compose_world_ontology_attestation",
    "compose_world_resource_guard",
    "compose_world_scope_mapping_reconcile",
    "graph_budget_view",
    "graph_status_overlay",
]
