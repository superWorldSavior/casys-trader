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

from trader.application.world_model.service import (
    DEFAULT_HORIZONS,
    WorldModelService,
    _clone,
    _utc,
)
from trader.runtime.world_macro_runtime import graph_v3_enabled


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
    ) -> None:
        self.runtime = runtime
        self.log = logger or logging.getLogger("casys-trader")
        self.thread_name = str(thread_name).strip() or "world-model-shadow"
        self.context_enricher = context_enricher
        self.graph_enricher = graph_enricher
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
            payload["graph"] = graph_v3_status_overlay(wired=True)
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
                except Exception as exc:  # noqa: BLE001 - V2 enrichment is fail-open for V1
                    self._background_error(report, stage="context_enrich", error=exc)
            if self.graph_enricher is not None:
                try:
                    enriched = tuple(
                        self.graph_enricher.enrich(tuple(_clone(episode) for episode in capture_snapshot.episodes))
                    )
                    capture_snapshot = dataclasses.replace(capture_snapshot, episodes=enriched)
                except Exception as exc:  # noqa: BLE001 - V3 enrichment is fail-open for V1/V2
                    self._background_error(report, stage="graph_enrich", error=exc)
            try:
                report["capture"] = self._capture_and_predict(capture_snapshot)
            except Exception as exc:  # noqa: BLE001 - still attempt capture after a maturity failure
                self._background_error(report, stage="capture", error=exc)
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
    """Background V2 companion attachment. Local files only; no network or LLM I/O."""

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
        v1 = tuple(canonical)
        v2 = attach_world_context(v1, self.source)
        return v1 + tuple(v2)


class WorldGraphEpisodeEnricher:
    """Background V3 companion attachment. Local files only; no network or LLM I/O."""

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
        v3 = attach_world_graph(attached, self.config)
        return attached + tuple(v3)


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


def graph_v3_budget_view() -> dict[str, object]:
    from trader.domain.world_feature_contract import (
        WORLD_GRAPH_V3_PATH_RULE_VERSION,
        WORLD_GRAPH_V3_WINDOWS_AND_DECAY,
    )
    from trader.domain.world_graph import GRAPH_TRAVERSAL_POLICY_VERSION

    return {
        "max_depth": 4,
        "max_paths_per_root": 32,
        "policy_version": GRAPH_TRAVERSAL_POLICY_VERSION,
        "path_rule_version": WORLD_GRAPH_V3_PATH_RULE_VERSION,
        "windows_and_decay": dict(WORLD_GRAPH_V3_WINDOWS_AND_DECAY),
    }


def graph_v3_status_overlay(*, wired: bool, writes: str = "none_until_due_cycle") -> dict[str, object]:
    return {
        "wired": wired,
        "flag": "CASYS_WORLD_MODEL_GRAPH_V3_ENABLED",
        "flag_default": 0,
        "budgets": graph_v3_budget_view(),
        "gaps": {"writes": writes, "cohort_activation": "not_started"},
        "authority": "shadow_only",
        "decision_effect": "none",
        "causal_claim": False,
        "pnl_claim": False,
        "recommendation": "NO_GO",
    }


def _compose_graph_v3_predictors() -> tuple[object, ...]:
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.encoding import world_lane_encoder_profile
    from trader.application.world_model.gru import OnlineGRUWorldChallenger
    from trader.domain.world_feature_contract import (
        GRAPH_FEATURE_CONTRACT_VERSION,
        WORLD_V3_GRU_MODEL_IDENTITY,
        WORLD_V3_MARKOV_MODEL_IDENTITY,
        WORLD_V3_MODEL_VERSION,
    )

    status = world_lane_encoder_profile("topology_status_only")
    content = world_lane_encoder_profile("graph_content")
    return (
        HierarchicalDirichletWorldBaseline(
            model_id=WORLD_V3_MARKOV_MODEL_IDENTITY,
            model_version=WORLD_V3_MODEL_VERSION,
            feature_contract=status.contract,
            feature_mask=status.mask,
            accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_VERSION}),
        ),
        OnlineGRUWorldChallenger(
            model_id=WORLD_V3_GRU_MODEL_IDENTITY,
            model_version=WORLD_V3_MODEL_VERSION,
            feature_contract=content.contract,
            feature_mask=content.mask,
            accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_VERSION}),
        ),
    )


def _compose_graph_v3_capture(
    *,
    store: object | None,
    config_dir: str | Path | None,
    study_cohort_id: str | None = None,
) -> object | None:
    if store is None or config_dir is None:
        return None
    path = getattr(store, "path", None)
    if path is None:
        return None
    from trader.application.world_model.graph_capture import WorldGraphCaptureConfig
    from trader.application.world_model.graph_snapshot import WorldGraphSnapshotService
    from trader.application.world_model.world_scope_resolver import WorldScopeResolver
    from trader.infrastructure.state_db.world_graph_store import WorldGraphStore

    resolver = WorldScopeResolver.load(Path(config_dir))
    db = getattr(store, "_db", None)
    graph_store = WorldGraphStore(db if db is not None else path)
    service = WorldGraphSnapshotService(
        ledger=graph_store,
        traversal=WorldTemporalTraversalAdapter(),
        snapshot_ledger=graph_store,
    )
    return WorldGraphCaptureConfig(
        scope_mapping=resolver.mapping,
        snapshot_service=service,
        study_cohort_id=study_cohort_id,
        max_depth=4,
        max_paths=32,
    )


def compose_local_graph_v3_lanes(
    *,
    enabled: bool | None = None,
    capture: object | None = None,
    predictors: Iterable[object] | None = None,
    store: object | None = None,
    config_dir: str | Path | None = None,
    study_cohort_id: str | None = None,
) -> tuple[tuple[object, ...], WorldGraphEpisodeEnricher | None]:
    """Compose local V3 lanes only when the reused GRAPH_V3 flag is on and capture+predictors exist."""

    try:
        resolved_enabled = graph_v3_enabled() if enabled is None else bool(enabled)
        if not resolved_enabled:
            return (), None
        resolved_capture = (
            capture
            if capture is not None
            else _compose_graph_v3_capture(
                store=store,
                config_dir=config_dir,
                study_cohort_id=study_cohort_id,
            )
        )
        if predictors is None:
            resolved_predictors: tuple[object, ...] = _compose_graph_v3_predictors()
        else:
            resolved_predictors = tuple(predictors)
        if resolved_capture is None or not resolved_predictors:
            return (), None
        return resolved_predictors, WorldGraphEpisodeEnricher(resolved_capture)
    except Exception:  # noqa: BLE001 - V3 composition never blocks V1/V2/Trader
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
        except Exception:  # noqa: BLE001 - unproven start never blocks V1 shadow
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
    "compose_local_graph_v3_lanes",
    "graph_v3_budget_view",
    "graph_v3_status_overlay",
]
