"""Deterministic World Model use-case: capture, predict, mature, and replay.

This service has no dependency on the trading daemon, scheduler, broker,
RiskGate, or Brain.  Collaborators are injected through the local Protocol
ports in :mod:`trader.application.world_model.protocols`.  Fail-open
protection for a live daemon lives in the runtime background adapter.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone

from trader.application.world_model.encoding import (
    common_training_replay_key,
    feature_contract_version_of,
    observation_market_anchor,
)
from trader.application.world_model.protocols import (
    WorldBarProvider,
    WorldLabeler,
    WorldModelLedger,
    WorldPredictor,
)
from trader.domain.world_cohort import (
    AdmitWorldCohortSlot,
    LaneOperationalStatus,
    WorldCohortSlot,
)
from trader.domain.world_scope import WorldMarketAnchorRef, WorldScopeResolution
from trader.domain.world_episode import (
    DEFAULT_WORLD_HORIZONS,
    WorldEpisode,
    WorldOutcome,
    WorldPrediction,
    canonical_prediction_class,
)


_V2_FEATURE_CONTRACT = "market_ohlcv_context.v2"
_V3_FEATURE_CONTRACT = "market_ohlcv_graph.v3"


DEFAULT_HORIZONS: tuple[str, ...] = tuple(item.horizon_id for item in DEFAULT_WORLD_HORIZONS)

_EPISODE_ID_FIELDS = ("episode_id", "world_episode_id", "id")
_HORIZON_FIELDS = ("horizon_id", "horizon_code", "horizon")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _field(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, _MISSING)
        if candidate is not _MISSING:
            return candidate
    return default


_MISSING = object()


@dataclasses.dataclass(frozen=True)
class _BarFetchFailure:
    """Cached provider failure, so one broken series is fetched at most once/run."""

    error: Exception


def _episode_id(episode: object) -> str | None:
    raw = _field(episode, *_EPISODE_ID_FIELDS)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _horizon_id(value: object) -> str | None:
    raw = _field(value, *_HORIZON_FIELDS)
    if raw is None:
        nested = _field(value, "prediction", "prediction_record", "outcome", "label")
        if nested is not None and nested is not value:
            return _horizon_id(nested)
    if isinstance(raw, Mapping):
        raw = _field(raw, "horizon_id", "horizon_code")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _symbol(episode: object) -> str | None:
    raw = _field(episode, "symbol")
    if raw is None:
        observation = _field(episode, "observation")
        raw = _field(observation, "symbol") if observation is not None else None
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _bar_interval(episode: object) -> str:
    observation = _field(episode, "observation")
    raw = _field(observation, "bar_interval", "interval") if observation is not None else None
    raw = raw or _field(episode, "bar_interval", "interval") or "1h"
    return str(raw)


def _mapping_copy(value: object) -> dict[str, object]:
    """Return a detached, JSON-ish mapping without requiring a domain import."""

    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return copy.deepcopy(dict(payload))
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        if isinstance(payload, Mapping):
            return copy.deepcopy(dict(payload))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump()
        if isinstance(payload, Mapping):
            return copy.deepcopy(dict(payload))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        payload = dataclasses.asdict(value)
        return copy.deepcopy(payload)
    raise TypeError(f"unsupported_shadow_payload:{type(value).__name__}")


def _clone(value: object) -> object:
    """Detach collaborator inputs so a shadow dependency cannot mutate trading data."""

    try:
        return copy.deepcopy(value)
    except Exception:
        # Frozen domain records use MappingProxyType internally, which the
        # stdlib deepcopy protocol cannot pickle.  Their canonical projection
        # is immutable by contract and is a safe detached boundary for the
        # duck-typed collaborators used here.
        return _mapping_copy(value)


def _episode_payload(value: object) -> object:
    """Unwrap indexed store rows without teaching the runtime a store schema."""

    nested = _field(value, "episode")
    if nested is not None:
        return nested
    return value


def _canonical_v2_episode(value: object) -> WorldEpisode | None:
    """Rebuild a V2 episode through the domain contract, or return None for non-V2."""

    payload = _mapping_copy(value)
    observation = payload.get("observation")
    observation_map = dict(observation) if isinstance(observation, Mapping) else {}
    top = str(payload.get("feature_contract_version") or "").strip()
    nested = str(observation_map.get("feature_contract_version") or "").strip()
    has_context = observation_map.get("context") is not None
    is_v3 = top == _V3_FEATURE_CONTRACT or nested == _V3_FEATURE_CONTRACT
    is_v2 = (top == _V2_FEATURE_CONTRACT or nested == _V2_FEATURE_CONTRACT or has_context) and not is_v3
    if top and nested and top != nested:
        if is_v2:
            raise ValueError("feature_contract_version envelope contradicts nested observation")
        return None
    if not is_v2:
        return None
    return WorldEpisode.from_dict(payload)


def _canonical_v3_episode(value: object) -> WorldEpisode | None:
    """Rebuild a V3 episode through the domain contract, or return None for non-V3."""

    payload = _mapping_copy(value)
    observation = payload.get("observation")
    observation_map = dict(observation) if isinstance(observation, Mapping) else {}
    top = str(payload.get("feature_contract_version") or "").strip()
    nested = str(observation_map.get("feature_contract_version") or "").strip()
    has_graph = observation_map.get("graph_features") is not None or observation_map.get("graph") is not None
    is_v3 = top == _V3_FEATURE_CONTRACT or nested == _V3_FEATURE_CONTRACT or has_graph
    if top and nested and top != nested:
        if is_v3:
            raise ValueError("feature_contract_version envelope contradicts nested observation")
        return None
    if not is_v3:
        return None
    return WorldEpisode.from_dict(payload)


def _episode_market_signature(value: object) -> str:
    """Fingerprint stable market evidence while ignoring context and fetch clocks."""

    canonical_v3 = _canonical_v3_episode(value)
    canonical_v2 = None if canonical_v3 is not None else _canonical_v2_episode(value)
    source = canonical_v3 if canonical_v3 is not None else canonical_v2
    payload = _mapping_copy(source if source is not None else value)
    payload.pop("episode_id", None)
    payload.pop("context", None)
    payload.pop("context_id", None)
    payload.pop("graph_features", None)
    payload.pop("graph", None)
    containers: list[dict[str, object]] = [payload]
    observation = payload.get("observation")
    if isinstance(observation, Mapping):
        detached_observation = copy.deepcopy(dict(observation))
        detached_observation.pop("context", None)
        detached_observation.pop("context_id", None)
        detached_observation.pop("graph_features", None)
        detached_observation.pop("graph", None)
        payload["observation"] = detached_observation
        containers.append(detached_observation)
    for container in containers:
        container.pop("available_at", None)
        container.pop("captured_at", None)
        freshness = container.get("freshness")
        if isinstance(freshness, Mapping):
            stable_freshness = copy.deepcopy(dict(freshness))
            stable_freshness.pop("data_age_minutes", None)
            container["freshness"] = stable_freshness
    return _stable_id("world-episode-market", payload)


def _iso_slot_text(value: object) -> str | None:
    parsed = _parse_timestamp(value)
    if parsed is not None:
        return parsed.isoformat()
    text = str(value or "").strip()
    return text or None


def _v2_market_slot(episode: object) -> dict[str, str] | None:
    return _market_slot_for_contract(episode, _V2_FEATURE_CONTRACT)


def _v3_market_slot(episode: object) -> dict[str, str] | None:
    return _market_slot_for_contract(episode, _V3_FEATURE_CONTRACT)


def _market_slot_for_contract(episode: object, contract: str) -> dict[str, str] | None:
    payload = _mapping_copy(episode)
    observation = payload.get("observation")
    source = dict(observation) if isinstance(observation, Mapping) else payload
    version = str(source.get("feature_contract_version") or payload.get("feature_contract_version") or "").strip()
    if version != contract:
        return None
    venue = str(source.get("venue") or payload.get("venue") or "").strip()
    symbol = str(source.get("symbol") or payload.get("symbol") or "").strip()
    interval = str(source.get("bar_interval") or payload.get("bar_interval") or "").strip()
    as_of = _iso_slot_text(source.get("as_of_bar_ts") or payload.get("as_of_bar_ts"))
    sampling = str(source.get("sampling_policy_version") or payload.get("sampling_policy_version") or "").strip()
    if not venue or not symbol or not interval or not as_of or not sampling:
        return None
    return {
        "venue": venue,
        "symbol": symbol,
        "bar_interval": interval,
        "as_of_bar_ts": as_of,
        "feature_contract_version": version,
        "sampling_policy_version": sampling,
    }


def _observation_payload(episode: object) -> dict[str, object]:
    observation = _field(episode, "observation")
    if observation is None:
        return {}
    return _mapping_copy(observation)


def _stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _canonical_outcome(value: object) -> object:
    """Project a labeler payload onto WorldOutcome when the live fields are present."""

    if isinstance(value, WorldOutcome):
        return value
    if not isinstance(value, Mapping):
        return value
    try:
        return WorldOutcome.from_dict(value)
    except (TypeError, ValueError):
        return value


def _normalise_horizons(values: Iterable[object]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        raw = value if isinstance(value, str) else _field(value, "horizon_id", "horizon_code")
        text = str(raw or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _predictor_accepts(predictor: object, episode: object) -> bool:
    """Honor optional accepts_episode(); legacy doubles without it accept all."""

    accepts = getattr(predictor, "accepts_episode", None)
    if not callable(accepts):
        return True
    try:
        return bool(accepts(episode))
    except Exception:
        return False


def _predictor_identity(predictor: object) -> tuple[str, str]:
    """Return a stable runtime identity for one shadow predictor.

    Real multi-model predictors are expected to expose ``model_id`` and
    ``model_version``.  The class-based fallback keeps older injected test
    doubles and single-model adapters backward compatible without conflating
    two real challengers.
    """

    model_id = str(getattr(predictor, "model_id", "") or "").strip()
    model_version = str(getattr(predictor, "model_version", "") or "").strip()
    if not model_id:
        predictor_type = type(predictor)
        model_id = f"{predictor_type.__module__}.{predictor_type.__qualname__}"
    return model_id, model_version or "unversioned"


def _prediction_model_identity(value: object) -> tuple[str | None, str | None]:
    nested = _field(value, "prediction", "prediction_record")
    model_id = _field(value, "model_kind", "model_id", "kind")
    model_version = _field(value, "model_version", "version")
    if model_id is None and nested is not None:
        model_id = _field(nested, "model_kind", "model_id", "kind")
    if model_version is None and nested is not None:
        model_version = _field(nested, "model_version", "version")
    normalized_id = str(model_id or "").strip() or None
    normalized_version = str(model_version or "").strip() or None
    return normalized_id, normalized_version


def _outcome_replay_key(value: object) -> tuple[datetime, str, str, str, str, str, str]:
    """Canonical order shared by live reconciliation and restart replay."""

    episode = _field(value, "episode")
    return common_training_replay_key(value, None if episode is _MISSING else episode)


def _episode_replay_key(value: object) -> tuple[datetime, datetime, str]:
    """Canonical causal order for sequence-model episode reconstruction."""

    observation = _field(value, "observation")
    available_at = _parse_timestamp(_field(value, "available_at"))
    if available_at is None and observation is not None:
        available_at = _parse_timestamp(_field(observation, "available_at"))
    observed_at = _parse_timestamp(_field(value, "observed_at", "as_of_bar_ts"))
    if observed_at is None and observation is not None:
        observed_at = _parse_timestamp(_field(observation, "as_of_bar_ts"))
    latest = datetime.max.replace(tzinfo=timezone.utc)
    return (available_at or observed_at or latest, observed_at or latest, str(_episode_id(value) or ""))


def _episode_series_identity(value: object) -> tuple[str, str, str, str, str]:
    """Mirror the sequence boundary without importing a concrete predictor."""

    episode = _episode_payload(value)
    observation = _field(episode, "observation")

    def text_field(name: str, *aliases: str) -> str:
        raw = _field(observation, name, *aliases) if observation is not None else None
        if raw is None:
            raw = _field(episode, name, *aliases)
        return str(raw or "").strip()

    return (
        text_field("venue"),
        text_field("symbol"),
        text_field("bar_interval", "interval"),
        text_field("feature_contract_version"),
        text_field("sampling_policy_version"),
    )


class WorldModelService:
    """A synchronous, single-flight, non-authoritative WorldEpisode worker.

    Collaborators implement the local Protocol ports.  Failures are recorded
    in the returned report and logger only; they never propagate to the caller.
    """

    def __init__(
        self,
        *,
        store: WorldModelLedger,
        predictor: WorldPredictor | None,
        predictors: Iterable[WorldPredictor] | None = None,
        labeler: WorldLabeler | None,
        bar_provider: WorldBarProvider | None,
        horizons: Iterable[str] = DEFAULT_HORIZONS,
        logger: object | None = None,
        lookback: str = "5d",
        run_id: str = "world_shadow.v1",
        cohort_service: object | None = None,
        scope_resolver: object | None = None,
    ) -> None:
        normalized = _normalise_horizons(horizons)
        configured = ([predictor] if predictor is not None else []) + list(predictors or ())
        identities = [_predictor_identity(item) for item in configured]
        if len(set(identities)) != len(identities):
            raise ValueError("shadow predictors must have unique model_id/model_version identities")
        self.store = store
        self.predictors = tuple(configured)
        # Retain the original attribute for compatibility with older callers;
        # new code iterates ``predictors`` and isolates each challenger.
        self.predictor = self.predictors[0] if self.predictors else None
        self._predictor_identities = {
            id(item): identity for item, identity in zip(self.predictors, identities, strict=True)
        }
        self.labeler = labeler
        self.bar_provider = bar_provider
        self.horizons = normalized or DEFAULT_HORIZONS
        self.lookback = str(lookback)
        self.run_id = str(run_id).strip() or "world_shadow.v1"
        self.cohort_service = cohort_service
        self.scope_resolver = scope_resolver
        self.log = logger or logging.getLogger("casys-trader")
        self._lock = threading.Lock()
        self._running: str | None = None
        self._last_report: dict[str, object] = {}
        self._prediction_keys: set[tuple[str, str, str, str]] = set()
        self._baseline_hydrated = False
        self._active_outcome_fingerprint: str | None = None
        self._eligible_episode_fingerprint: str | None = None
        self._eligible_episode_signatures: dict[str, str] | None = None
        self._model_hydrated_through: datetime | None = None
        self._started_cutoffs: dict[str, datetime] | None = None
        self._admitted_ids_by_cohort: dict[str, set[str]] | None = None
        self._blocked_lanes_by_cohort: dict[str, set[str]] | None = None

    def capture_and_predict(
        self,
        episodes: Iterable[object],
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Persist detached observations then persist at most one prediction/horizon.

        This method deliberately does not fetch bars or label anything.  An
        observation/prediction is therefore durable before later maturation can
        emit an outcome event.  A background caller must inject the snapshot
        clock so a delayed worker cannot train or predict from evidence that
        became available after that snapshot.
        """

        try:
            current_now = datetime.now(timezone.utc) if now is None else _utc(now)
        except Exception as exc:  # noqa: BLE001 - malformed shadow input stays isolated
            report = self._begin_report(
                "capture_and_predict",
                now=datetime.now(timezone.utc),
            )
            if report is None:
                return self._busy_report("capture_and_predict")
            try:
                self._error(report, stage="capture_now", error=exc)
            finally:
                self._finish_report(report)
            return report
        report = self._begin_report("capture_and_predict", now=current_now)
        if report is None:
            return self._busy_report("capture_and_predict")
        try:
            self._hydrate_baseline(report, _parse_timestamp(report["as_of"]))
            self._hydrate_prediction_keys(report)
            canonical_episodes = self._capture_episodes(episodes, report)
            self._admit_collecting_slots(canonical_episodes, report)
            # Persist the entire cohort before prediction.  A late eligible
            # episode can change an older target sequence, so reconcile the
            # canonical episode ledger before emitting any forecast.
            observed_during_hydration = self._hydrate_baseline(
                report,
                _parse_timestamp(report["as_of"]),
            )
            for episode in canonical_episodes:
                identifier = _episode_id(episode)
                already_observed = observed_during_hydration.get(identifier, set()) if identifier is not None else set()
                remaining_predictors = tuple(
                    predictor for predictor in self.predictors if id(predictor) not in already_observed
                )
                if remaining_predictors:
                    self._observe_predictors(
                        episode,
                        report,
                        predictors=remaining_predictors,
                    )
                for horizon in self.horizons:
                    self._predict_one(episode, horizon, report)
        except Exception as exc:  # noqa: BLE001 - a shadow worker never raises
            self._error(report, stage="capture_run", error=exc)
        finally:
            self._finish_report(report)
        return report

    def mature_pending(
        self,
        now: datetime,
        *,
        bars_by_symbol: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Mature each pending horizon independently and update only sealed labels.

        ``bars_by_symbol`` is an optional detached cycle snapshot.  Supplying
        it lets a future daemon integration reuse already-fetched market bars
        rather than making shadow maintenance add market I/O to a cycle.
        """

        try:
            current_now = _utc(now)
        except Exception as exc:  # noqa: BLE001 - malformed shadow input never reaches trading
            report = self._begin_report("mature_pending", now=datetime.now(timezone.utc))
            if report is None:
                return self._busy_report("mature_pending")
            try:
                self._error(report, stage="mature_now", error=exc)
            finally:
                self._finish_report(report)
            return report
        report = self._begin_report("mature_pending", now=current_now)
        if report is None:
            return self._busy_report("mature_pending")
        try:
            try:
                # ``None`` means an explicit maintenance run may fetch through
                # the injected provider.  A supplied mapping is a frozen cohort:
                # an absent symbol means unavailable at T0, never permission for
                # post-snapshot background I/O.
                supplied_bars = _MISSING if bars_by_symbol is None else _clone(bars_by_symbol)
            except Exception as exc:  # noqa: BLE001 - caller data remains untouched
                self._error(report, stage="bars_snapshot_clone", error=exc)
                supplied_bars = {}
            self._hydrate_baseline(report, current_now)
            self._mature_horizons(current_now, report, supplied_bars)
            # New labels, delayed evidence and superseding corrections all
            # reconcile against the same active-leaf order used on restart.
            self._hydrate_baseline(report, current_now)
        except Exception as exc:  # noqa: BLE001 - a shadow worker never raises
            self._error(report, stage="mature_run", error=exc)
        finally:
            self._finish_report(report)
        return report

    def status(self) -> dict[str, object]:
        """Return shadow-worker state without consulting any trading runtime state."""

        return {**copy.deepcopy(self._last_report), "running": self._running is not None}

    def _begin_report(self, operation: str, *, now: datetime) -> dict[str, object] | None:
        if not self._lock.acquire(blocking=False):
            return None
        self._running = operation
        return {
            "operation": operation,
            "as_of": _utc(now).isoformat(),
            "status": "ok",
            "episodes_received": 0,
            "episodes_appended": 0,
            "episodes_existing": 0,
            "predictions_appended": 0,
            "predictions_existing": 0,
            "outcomes_appended": 0,
            "outcomes_existing": 0,
            "outcomes_pending": 0,
            "outcomes_terminal": 0,
            "bar_provider_fetches": 0,
            "bars_snapshot_reused": 0,
            "baseline_updates": 0,
            "baseline_replayed": 0,
            "model_updates": 0,
            "model_replayed": 0,
            "model_observations_replayed": 0,
            "model_reconcile_skipped_unresettable": 0,
            "model_updates_by_model": {},
            "model_replayed_by_model": {},
            "errors": [],
        }

    def _busy_report(self, operation: str) -> dict[str, object]:
        return {
            "operation": operation,
            "status": "skipped",
            "reason": "single_flight_busy",
            "running": self._running,
            "errors": [],
        }

    def _finish_report(self, report: dict[str, object]) -> None:
        if report["errors"]:
            report["status"] = "partial"
        self._last_report = copy.deepcopy(report)
        self._running = None
        self._lock.release()

    def _error(
        self,
        report: dict[str, object],
        *,
        stage: str,
        error: Exception,
        episode: object | None = None,
        horizon: str | None = None,
    ) -> None:
        item: dict[str, object] = {"stage": stage, "error": f"{type(error).__name__}:{error}"}
        if episode is not None:
            if (episode_id := _episode_id(episode)) is not None:
                item["episode_id"] = episode_id
            if (symbol := _symbol(episode)) is not None:
                item["symbol"] = symbol
        if horizon is not None:
            item["horizon_id"] = horizon
            item["horizon_code"] = horizon
        errors = report["errors"]
        if isinstance(errors, list):
            errors.append(item)
        try:
            warning = getattr(self.log, "warning", None)
            if callable(warning):
                warning("[world_model_shadow] %s", item)
        except Exception:
            # Logging is not allowed to turn a failed shadow side effect into a
            # caller-visible failure either.
            pass

    def _capture_episodes(
        self,
        episodes: Iterable[object],
        report: dict[str, object],
    ) -> list[object]:
        canonical_episodes: list[object] = []
        try:
            iterator = iter(episodes)
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="episodes_iter", error=exc)
            return canonical_episodes

        while True:
            try:
                raw_episode = next(iterator)
            except StopIteration:
                return canonical_episodes
            except Exception as exc:  # noqa: BLE001
                self._error(report, stage="episodes_iter", error=exc)
                return canonical_episodes
            report["episodes_received"] = int(report["episodes_received"]) + 1
            try:
                episode = _clone(raw_episode)
            except Exception as exc:  # noqa: BLE001
                self._error(report, stage="episode_clone", error=exc)
                continue
            identifier = _episode_id(episode)
            if identifier is None:
                self._error(report, stage="episode_id", error=ValueError("missing_episode_id"), episode=episode)
                continue

            canonical_episode = episode
            try:
                existing = self._lookup_canonical_episode(episode)
            except Exception as load_exc:  # noqa: BLE001 - shadow conflict never escapes
                self._error(
                    report,
                    stage="get_episode",
                    error=load_exc,
                    episode=episode,
                )
                continue
            if existing is not None:
                report["episodes_existing"] = int(report["episodes_existing"]) + 1
                canonical_episodes.append(existing)
                continue
            try:
                appended = self.store.append_episode(_clone(episode))
            except Exception as exc:  # noqa: BLE001
                try:
                    existing = self._lookup_canonical_episode(episode)
                    if existing is None:
                        existing = self._load_existing_episode(identifier, episode)
                except Exception as load_exc:  # noqa: BLE001 - shadow conflict never escapes
                    self._error(
                        report,
                        stage="get_episode",
                        error=load_exc,
                        episode=episode,
                    )
                    continue
                if existing is not None:
                    report["episodes_existing"] = int(report["episodes_existing"]) + 1
                    canonical_episode = existing
                else:
                    self._error(report, stage="append_episode", error=exc, episode=episode)
                    continue
            else:
                if appended is False:
                    report["episodes_existing"] = int(report["episodes_existing"]) + 1
                    reused = self._lookup_canonical_episode(episode)
                    if reused is not None:
                        canonical_episode = reused
                else:
                    report["episodes_appended"] = int(report["episodes_appended"]) + 1
            canonical_episodes.append(canonical_episode)

    def _lookup_canonical_episode(self, incoming_episode: object) -> object | None:
        """Reuse the first canonical V2/V3 episode for a market slot when the store can."""

        v2_slot = _v2_market_slot(incoming_episode)
        v3_slot = _v3_market_slot(incoming_episode)
        if v2_slot is not None:
            lookup = getattr(self.store, "get_episode_by_v2_slot", None)
            slot = v2_slot
        elif v3_slot is not None:
            lookup = getattr(self.store, "get_episode_by_v3_slot", None)
            slot = v3_slot
        else:
            return None
        if not callable(lookup):
            return None
        stored = lookup(**slot)
        if stored is None:
            return None
        canonical = _clone(_episode_payload(stored))
        if _episode_market_signature(canonical) != _episode_market_signature(incoming_episode):
            raise ValueError(
                "existing_episode_market_evidence_conflict: same sampling slot has "
                "different source, OHLCV, feature, or eligibility evidence"
            )
        return canonical

    def _load_existing_episode(
        self,
        episode_id: str,
        incoming_episode: object,
    ) -> object | None:
        """Reuse the first immutable observation recorded for one sampling slot.

        A later daemon cycle can see the same completed bar with a later fetch
        or capture clock.  The deterministic episode id intentionally denotes
        the market slot, so the first point-in-time evidence remains canonical;
        attempting to append the later envelope would correctly conflict in the
        strict store.  Direct store callers still retain that conflict check.
        """

        stored = self.store.get_episode(episode_id)
        if stored is None:
            return None
        canonical = _clone(_episode_payload(stored))
        if _episode_market_signature(canonical) != _episode_market_signature(incoming_episode):
            raise ValueError(
                "existing_episode_market_evidence_conflict: same sampling slot has "
                "different source, OHLCV, feature, or eligibility evidence"
            )
        return canonical

    def _predict_one(self, episode: object, horizon: str, report: dict[str, object]) -> None:
        identifier = _episode_id(episode)
        if identifier is None:
            return
        if not self.predictors:
            self._error(
                report, stage="predict", error=RuntimeError("predictor_unavailable"), episode=episode, horizon=horizon
            )
            return
        for predictor in self.predictors:
            if not _predictor_accepts(predictor, episode):
                continue
            if not self._predictor_sees_episode(predictor, episode):
                continue
            model_id, model_version = self._predictor_identities[id(predictor)]
            key = (identifier, horizon, model_id, model_version)
            if key in self._prediction_keys:
                report["predictions_existing"] = int(report["predictions_existing"]) + 1
                continue
            try:
                prediction = self._predict(predictor, episode, horizon, _parse_timestamp(report["as_of"]))
                if prediction is None:
                    raise ValueError("empty_prediction")
                event = self._prediction_event(
                    prediction,
                    episode,
                    identifier,
                    horizon,
                    report["as_of"],
                    model_id=model_id,
                    model_version=model_version,
                    predictor=predictor,
                )
                appended = self.store.append_prediction(_clone(event))
            except Exception as exc:  # noqa: BLE001
                self._error(
                    report,
                    stage=f"predict:{model_id}:{model_version}",
                    error=exc,
                    episode=episode,
                    horizon=horizon,
                )
                continue
            self._prediction_keys.add(key)
            if appended is False:
                report["predictions_existing"] = int(report["predictions_existing"]) + 1
            else:
                report["predictions_appended"] = int(report["predictions_appended"]) + 1

    def _predict(
        self,
        predictor: WorldPredictor,
        episode: object,
        horizon: str,
        predicted_at: datetime | None,
    ) -> WorldPrediction:
        return predictor.predict(_clone(episode), horizon, prediction_at=predicted_at)

    def _prediction_event(
        self,
        prediction: object,
        episode: object,
        episode_id: str,
        horizon: str,
        predicted_at: object,
        *,
        model_id: str,
        model_version: str,
        predictor: object | None = None,
    ) -> dict[str, object]:
        """Normalize a prediction into the append-only store envelope.

        The nested ``prediction`` remains the predictor's detached payload;
        the surrounding fields make older and newer store variants equally
        replayable without asking the predictor to know persistence details.
        Indexed cohort lineage columns are stamped from a cold-lane identity
        when present; V1/V2 shadow predictions omit them.
        """

        if isinstance(prediction, WorldPrediction):
            payload = prediction.to_dict()
        else:
            payload = _mapping_copy(prediction)
        payload_model_id, payload_model_version = _prediction_model_identity(payload)
        if payload_model_id is not None and payload_model_id != model_id:
            raise ValueError(f"prediction model_id mismatch: expected {model_id!r}, got {payload_model_id!r}")
        if payload_model_version is not None and payload_model_version != model_version:
            raise ValueError(
                f"prediction model_version mismatch: expected {model_version!r}, got {payload_model_version!r}"
            )
        lineage = _predictor_cohort_lineage(predictor)
        if lineage:
            payload.update(lineage)
        prediction_id = str(payload.get("prediction_id") or "").strip()
        if not prediction_id:
            prediction_id = _stable_id(
                "world-shadow-prediction",
                {
                    "episode_id": episode_id,
                    "horizon_id": horizon,
                    "model_id": model_id,
                    "model_version": model_version,
                    "run_id": str(payload.get("run_id") or self.run_id),
                    "payload": payload,
                },
            )
        event = {
            "prediction_id": prediction_id,
            "run_id": payload.get("run_id") or payload.get("model_run_id") or self.run_id,
            "episode_id": episode_id,
            "horizon_id": payload.get("horizon_id") or horizon,
            "horizon_code": payload.get("horizon_code") or payload.get("horizon_id") or horizon,
            "model_kind": model_id,
            "model_version": model_version,
            "predicted_at": payload.get("predicted_at") or payload.get("created_at") or predicted_at,
            "input": _observation_payload(episode),
            "prediction": payload,
        }
        if lineage:
            event.update(lineage)
        return event

    def _hydrate_prediction_keys(self, report: dict[str, object]) -> None:
        try:
            rows = self.store.list_predictions()
            for row in list(rows or []):
                identifier = _episode_id(row)
                horizon = _horizon_id(row)
                if not identifier or not horizon:
                    continue
                model_id, model_version = _prediction_model_identity(row)
                candidates = [
                    identity
                    for identity in self._predictor_identities.values()
                    if (model_id is None or identity[0] == model_id)
                    and (model_version is None or identity[1] == model_version)
                ]
                if len(candidates) == 1:
                    identity = candidates[0]
                    self._prediction_keys.add((identifier, horizon, identity[0], identity[1]))
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="list_predictions", error=exc)

    def _observe_predictors(
        self,
        episode: object,
        report: dict[str, object],
        *,
        predictors: Iterable[object] | None = None,
    ) -> bool:
        """Give sequence models one immutable episode without coupling the runtime to GRU."""

        completed = True
        selected = self.predictors if predictors is None else tuple(predictors)
        for predictor in selected:
            observe = getattr(predictor, "observe_episode", None)
            if not callable(observe):
                continue
            if not _predictor_accepts(predictor, episode):
                continue
            if not self._predictor_sees_episode(predictor, episode):
                continue
            model_id, model_version = self._predictor_identities[id(predictor)]
            try:
                observe(_clone(episode))
            except Exception as exc:  # noqa: BLE001 - one challenger never blocks another
                completed = False
                self._error(
                    report,
                    stage=f"observe:{model_id}:{model_version}",
                    error=exc,
                    episode=episode,
                )
        return completed

    def _hydrate_baseline(self, report: dict[str, object], now: datetime | None) -> dict[str, set[int]]:
        """Reconcile predictors with the canonical active outcome leaves.

        Sequence histories are replayed in point-in-time order before outcomes.
        Outcomes are replayed in one canonical order.  The active-leaf
        fingerprint is checked even after initial hydration so delayed labels
        and append-only corrections cannot leave a live daemon with different
        weights from a fresh restart.  The return value records sequence
        observers that already received an episode during this pass, so a
        capture cohort is not sent to a non-idempotent legacy observer twice.
        """

        observed_predictors: dict[str, set[int]] = {}
        if not self.predictors:
            return observed_predictors

        sequence_predictors = [
            predictor for predictor in self.predictors if callable(getattr(predictor, "observe_episode", None))
        ]
        stored_episodes: list[object] = []
        eligible_fingerprint: str | None = None
        eligible_signatures: dict[str, str] | None = None
        if sequence_predictors:
            try:
                stored_episodes = list(self.store.list_eligible_episodes() or [])
                stored_episodes.sort(key=_episode_replay_key)
                episode_payloads = [_mapping_copy(_episode_payload(row)) for row in stored_episodes]
                eligible_signatures = {
                    str(_episode_id(row) or ""): _stable_id(
                        "world-eligible-episode",
                        payload,
                    )
                    for row, payload in zip(stored_episodes, episode_payloads, strict=True)
                    if _episode_id(row) is not None
                }
                eligible_fingerprint = _stable_id(
                    "world-eligible-episodes",
                    {"episodes": episode_payloads},
                )
            except Exception as exc:  # noqa: BLE001
                self._error(report, stage="model_hydrate_observations", error=exc)
                return observed_predictors

        all_apply_predictors = [
            predictor for predictor in self.predictors if callable(getattr(predictor, "apply_outcome", None))
        ]
        try:
            rows = (
                self.store.list_observed_outcomes(active_only=True, training_eligible=True)
                if all_apply_predictors
                else []
            )
            ordered = sorted(list(rows or []), key=_outcome_replay_key)
            active_fingerprint = _stable_id(
                "world-active-outcomes",
                {"labels": [self._outcome_label(row) for row in ordered]},
            )
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="model_hydrate_labels", error=exc)
            return observed_predictors

        causal_cutoff_is_current = (
            now is None or self._model_hydrated_through is None or now >= self._model_hydrated_through
        )
        active_changed = active_fingerprint != self._active_outcome_fingerprint
        eligible_changed = eligible_fingerprint != self._eligible_episode_fingerprint
        if self._baseline_hydrated and not active_changed and not eligible_changed and causal_cutoff_is_current:
            return observed_predictors

        episode_change_requires_reset = False
        new_episode_rows: list[object] = []
        if eligible_changed and self._eligible_episode_signatures is not None:
            previous = self._eligible_episode_signatures
            current = eligible_signatures or {}
            previous_ids = set(previous)
            current_ids = set(current)
            if not previous_ids.issubset(current_ids) or any(
                previous[identifier] != current[identifier] for identifier in previous_ids.intersection(current_ids)
            ):
                episode_change_requires_reset = True
            new_ids = current_ids - previous_ids
            by_id = {identifier: row for row in stored_episodes if (identifier := _episode_id(row)) is not None}
            new_episode_rows = [by_id[identifier] for identifier in new_ids]
            target_rows = [by_id[identifier] for row in ordered if (identifier := _episode_id(row)) in by_id]
            if any(
                _episode_series_identity(new_row) == _episode_series_identity(target_row)
                and _episode_replay_key(new_row) <= _episode_replay_key(target_row)
                for new_row in new_episode_rows
                for target_row in target_rows
            ):
                episode_change_requires_reset = True

        if (
            self._baseline_hydrated
            and not active_changed
            and eligible_changed
            and not episode_change_requires_reset
            and causal_cutoff_is_current
        ):
            # Pure append of later, unlabeled sequence observations: no prior
            # training example can change, so extend caches online without an
            # O(history) replay.
            for row in sorted(new_episode_rows, key=_episode_replay_key):
                if not self._observe_predictors(_clone(_episode_payload(row)), report):
                    self._baseline_hydrated = False
                    return observed_predictors
                identifier = _episode_id(row)
                if identifier is not None:
                    observed_predictors[identifier] = {
                        id(predictor)
                        for predictor in self.predictors
                        if callable(getattr(predictor, "observe_episode", None))
                    }
            self._eligible_episode_fingerprint = eligible_fingerprint
            self._eligible_episode_signatures = eligible_signatures
            self._model_hydrated_through = now
            return observed_predictors

        needs_authoritative_reset = (
            self._active_outcome_fingerprint is not None or self._eligible_episode_fingerprint is not None
        )
        replay_predictors = list(self.predictors)
        if needs_authoritative_reset:
            # The production baseline and GRU implement reset_for_replay.  A
            # legacy duck-typed injected predictor may not; keep its already
            # applied live state instead of double-training it or turning a
            # formerly supported collaborator into a permanent partial run.
            # Such a predictor does not receive correction reconciliation.
            unresettable = [
                predictor
                for predictor in replay_predictors
                if not callable(getattr(predictor, "reset_for_replay", None))
            ]
            report["model_reconcile_skipped_unresettable"] = len(unresettable)
            replay_predictors = [
                predictor for predictor in replay_predictors if callable(getattr(predictor, "reset_for_replay", None))
            ]

        completed = True
        for predictor in replay_predictors:
            reset = getattr(predictor, "reset_for_replay", None)
            if not callable(reset):
                continue
            model_id, model_version = self._predictor_identities[id(predictor)]
            try:
                reset()
            except Exception as exc:  # noqa: BLE001
                completed = False
                self._error(
                    report,
                    stage=f"model_reconcile_reset:{model_id}:{model_version}",
                    error=exc,
                )
        if not completed:
            self._baseline_hydrated = False
            return observed_predictors

        if any(callable(getattr(predictor, "observe_episode", None)) for predictor in replay_predictors):
            try:
                for row in stored_episodes:
                    episode = _clone(_episode_payload(row))
                    if self._observe_predictors(
                        episode,
                        report,
                        predictors=replay_predictors,
                    ):
                        identifier = _episode_id(row)
                        if identifier is not None:
                            observed_predictors[identifier] = {
                                id(predictor)
                                for predictor in replay_predictors
                                if callable(getattr(predictor, "observe_episode", None))
                            }
                        report["model_observations_replayed"] = int(report["model_observations_replayed"]) + 1
                    else:
                        completed = False
            except Exception as exc:  # noqa: BLE001
                self._error(report, stage="model_hydrate_observations", error=exc)
                self._baseline_hydrated = False
                return observed_predictors

        apply_predictors = [
            predictor for predictor in replay_predictors if callable(getattr(predictor, "apply_outcome", None))
        ]
        for row in ordered:
            label = self._outcome_label(row)
            if not self._is_sealed_observed(label, now):
                completed = False
                continue
            identifier = _episode_id(row) or _episode_id(label)
            if identifier is None:
                completed = False
                self._error(
                    report,
                    stage="model_hydrate",
                    error=ValueError("missing_episode_id"),
                    episode=row,
                    horizon=_horizon_id(row),
                )
                continue
            try:
                stored_episode = self.store.get_episode(identifier)
                if stored_episode is None:
                    raise ValueError("episode_not_found")
                episode = _clone(_episode_payload(stored_episode))
            except Exception as exc:  # noqa: BLE001
                completed = False
                self._error(
                    report,
                    stage="model_hydrate_episode",
                    error=exc,
                    episode=row,
                    horizon=_horizon_id(row),
                )
                continue
            for predictor in apply_predictors:
                if not _predictor_accepts(predictor, episode):
                    continue
                if not self._predictor_sees_episode(predictor, episode):
                    continue
                model_id, model_version = self._predictor_identities[id(predictor)]
                try:
                    applied = self._call_baseline_apply(predictor.apply_outcome, label, episode, now)
                except Exception as exc:  # noqa: BLE001
                    completed = False
                    self._error(
                        report,
                        stage=f"model_hydrate:{model_id}:{model_version}",
                        error=exc,
                        episode=row,
                        horizon=_horizon_id(row),
                    )
                    continue
                if applied is False:
                    continue
                self._increment_model_counter(report, "replayed", predictor)
                if predictor is self.predictor:
                    report["baseline_replayed"] = int(report["baseline_replayed"]) + 1
        self._baseline_hydrated = completed
        if completed:
            self._active_outcome_fingerprint = active_fingerprint
            self._eligible_episode_fingerprint = eligible_fingerprint
            self._eligible_episode_signatures = eligible_signatures
            self._model_hydrated_through = now
        return observed_predictors

    def _mature_horizons(
        self,
        now: datetime,
        report: dict[str, object],
        supplied_bars: object,
    ) -> None:
        bars_by_market: dict[tuple[str, str], object | _BarFetchFailure] = {}
        for horizon in self.horizons:
            try:
                pending = self._pending_for_horizon(horizon)
            except Exception as exc:  # noqa: BLE001
                self._error(report, stage="list_pending", error=exc, horizon=horizon)
                continue
            seen: set[str] = set()
            for raw_episode in pending:
                try:
                    episode = _clone(_episode_payload(raw_episode))
                except Exception as exc:  # noqa: BLE001
                    self._error(report, stage="episode_clone", error=exc, horizon=horizon)
                    continue
                identifier = _episode_id(episode)
                if identifier is None:
                    self._error(
                        report,
                        stage="episode_id",
                        error=ValueError("missing_episode_id"),
                        episode=episode,
                        horizon=horizon,
                    )
                    continue
                if identifier in seen:
                    continue
                seen.add(identifier)
                self._mature_one(episode, horizon, now, report, bars_by_market, supplied_bars)

    def _pending_for_horizon(self, horizon: str) -> list[object]:
        rows = self.store.list_pending_episodes(horizon_id=horizon, training_eligible=True)
        return list(rows or [])

    def _mature_one(
        self,
        episode: object,
        horizon: str,
        now: datetime,
        report: dict[str, object],
        bars_by_market: dict[tuple[str, str], object | _BarFetchFailure],
        supplied_bars: object,
    ) -> None:
        identifier = _episode_id(episode)
        assert identifier is not None
        try:
            market_key = (_symbol(episode) or f"episode:{identifier}", _bar_interval(episode))
            bars = bars_by_market.get(market_key, _MISSING)
            if bars is _MISSING:
                supplied = self._supplied_bars(supplied_bars, market_key[0], market_key[1])
                if supplied is _MISSING:
                    report["bar_provider_fetches"] = int(report["bar_provider_fetches"]) + 1
                    try:
                        bars = self._fetch_bars(episode, now)
                    except Exception as exc:  # noqa: BLE001 - cache one provider failure per series/run
                        bars_by_market[market_key] = _BarFetchFailure(exc)
                        raise
                else:
                    bars = supplied
                    report["bars_snapshot_reused"] = int(report["bars_snapshot_reused"]) + 1
                bars_by_market[market_key] = _clone(bars)
            if isinstance(bars, _BarFetchFailure):
                raise bars.error
            outcome = self._label(episode, _clone(bars), horizon, now)
            if outcome is None:
                raise ValueError("empty_outcome")
            event = self._outcome_event(outcome, identifier, horizon)
            label = event["label"]
            assert isinstance(label, Mapping)
            status = str(label.get("status") or "").strip().lower()
            if status == "pending":
                report["outcomes_pending"] = int(report.get("outcomes_pending", 0)) + 1
                return
            if status not in {"observed", "missing", "unknown"}:
                raise ValueError(f"unsupported_outcome_status:{status or 'empty'}")
            appended = self.store.append_outcome_event(_clone(event))
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="mature", error=exc, episode=episode, horizon=horizon)
            return

        if appended is False:
            report["outcomes_existing"] = int(report["outcomes_existing"]) + 1
            return
        report["outcomes_appended"] = int(report["outcomes_appended"]) + 1
        label = event["label"]
        assert isinstance(label, Mapping)
        if str(label.get("status") or "").strip().lower() in {"missing", "unknown"}:
            report["outcomes_terminal"] = int(report["outcomes_terminal"]) + 1
            return
        if not self._is_sealed_observed(label, now):
            return
        self._apply_baseline(label, episode, now, report, horizon)

    @staticmethod
    def _supplied_bars(supplied_bars: object, symbol: str, interval: str) -> object:
        """Read a provided cycle snapshot without falling back to live state.

        Values may be a direct iterable of bars or an interval-keyed mapping.
        An explicitly supplied empty iterable is meaningful and must not cause
        a background fetch, because that would mix two different snapshots.
        """

        raw = _field(supplied_bars, symbol, default=_MISSING)
        if raw is _MISSING:
            return _MISSING if supplied_bars is _MISSING else ()
        if isinstance(raw, Mapping):
            by_interval = _field(raw, interval, default=_MISSING)
            if by_interval is not _MISSING:
                return by_interval
            nested = _field(raw, "bars", "values", default=_MISSING)
            if nested is not _MISSING:
                return nested
            # A mapping can itself be one evidence bar.  Preserve it as one
            # bar rather than interpreting its OHLC keys as interval names.
            if _field(raw, "close", "ts", "timestamp", default=_MISSING) is not _MISSING:
                return [raw]
        return raw

    def _outcome_event(self, outcome: object, episode_id: str, horizon: str) -> dict[str, object]:
        """Envelope one label while preserving its complete immutable evidence."""

        if isinstance(outcome, WorldOutcome):
            label = outcome.to_dict()
        else:
            label = _mapping_copy(outcome)
            move_raw = label.get("move_class") or label.get("direction")
            if move_raw is not None:
                try:
                    move = canonical_prediction_class(move_raw)
                    label["move_class"] = move
                    if "direction" in label:
                        label["direction"] = move
                except (TypeError, ValueError):
                    pass
        label.setdefault("episode_id", episode_id)
        label.setdefault("horizon_id", horizon)
        label.setdefault("horizon_code", horizon)
        outcome_event_id = str(
            label.get("outcome_event_id") or label.get("event_id") or label.get("outcome_id") or ""
        ).strip()
        if not outcome_event_id:
            outcome_event_id = _stable_id(
                "world-shadow-outcome",
                {
                    "episode_id": episode_id,
                    "horizon_id": horizon,
                    "label": label,
                },
            )
        evidence = {
            key: _clone(label[key])
            for key in (
                "anchor_bar",
                "anchor_evidence_id",
                "target_bar",
                "target_evidence_id",
                "as_of_bar_ts",
                "target_at",
                "deadline_at",
                "source",
                "source_raw_sha256",
                "availability_provenance",
                "endpoint_bar_ts",
            )
            if key in label
        }
        move = label.get("move_class") or label.get("direction")
        return {
            "outcome_event_id": outcome_event_id,
            "episode_id": episode_id,
            "horizon_id": str(label.get("horizon_id") or horizon),
            "horizon_code": str(label.get("horizon_code") or label.get("horizon_id") or horizon),
            "label_schema_version": label.get("label_schema_version")
            or label.get("label_semantics_version")
            or label.get("schema_version"),
            "status": label.get("status"),
            "move_class": move,
            "direction": move,
            "training_eligible": label.get("training_eligible"),
            "source": label.get("source"),
            "source_raw_sha256": label.get("source_raw_sha256"),
            "label_available_at": label.get("label_available_at") or label.get("available_at"),
            "sealed_at": label.get("sealed_at") or label.get("computed_at"),
            "supersedes_outcome_event_id": label.get("supersedes_event_id") or label.get("supersedes_outcome_event_id"),
            "label": label,
            "evidence": evidence,
        }

    def _fetch_bars(self, episode: object, now: datetime) -> object:
        provider = self.bar_provider
        if provider is None:
            raise RuntimeError("bar_provider_unavailable")
        symbol = _symbol(episode)
        if symbol is None:
            raise ValueError("missing_symbol")
        interval = _bar_interval(episode)
        return provider.get_bars(symbol, lookback=self.lookback, interval=interval)

    def _label(self, episode: object, bars: object, horizon: str, now: datetime) -> object:
        labeler = self.labeler
        if labeler is None:
            raise RuntimeError("labeler_unavailable")
        return _canonical_outcome(labeler.label_horizon(_clone(episode), _clone(bars), horizon, now=now))

    def _apply_baseline(
        self,
        outcome: Mapping[str, object],
        episode: object,
        now: datetime,
        report: dict[str, object],
        horizon: str,
    ) -> None:
        for predictor in self.predictors:
            apply_outcome = getattr(predictor, "apply_outcome", None)
            if not callable(apply_outcome):
                continue
            if not _predictor_accepts(predictor, episode):
                continue
            if not self._predictor_sees_episode(predictor, episode):
                continue
            model_id, model_version = self._predictor_identities[id(predictor)]
            try:
                applied = self._call_baseline_apply(apply_outcome, outcome, episode, now)
            except Exception as exc:  # noqa: BLE001
                # The durable outcome will no longer be pending.  Mark the
                # in-memory replay dirty so the next shadow pass retries this
                # model from the authoritative ledger instead of losing the
                # update until a process restart.
                self._baseline_hydrated = False
                self._error(
                    report,
                    stage=f"model_update:{model_id}:{model_version}",
                    error=exc,
                    episode=episode,
                    horizon=horizon,
                )
                continue
            if applied is False:
                continue
            self._increment_model_counter(report, "updates", predictor)
            if predictor is self.predictor:
                report["baseline_updates"] = int(report["baseline_updates"]) + 1

    def _increment_model_counter(
        self,
        report: dict[str, object],
        suffix: str,
        predictor: object,
    ) -> None:
        total_key = "model_updates" if suffix == "updates" else "model_replayed"
        by_model_key = "model_updates_by_model" if suffix == "updates" else "model_replayed_by_model"
        report[total_key] = int(report[total_key]) + 1
        counts = report.get(by_model_key)
        if not isinstance(counts, dict):
            counts = {}
            report[by_model_key] = counts
        model_id, model_version = self._predictor_identities[id(predictor)]
        key = f"{model_id}@{model_version}"
        counts[key] = int(counts.get(key, 0)) + 1

    @staticmethod
    def _call_baseline_apply(
        fn: Callable[..., object],
        outcome: Mapping[str, object],
        episode: object,
        now: datetime | None,
    ) -> bool | None:
        result = fn(_clone(outcome), _clone(episode), available_through=now)
        if isinstance(result, Mapping):
            return result.get("applied") is not False
        applied = getattr(result, "applied", _MISSING)
        if applied is not _MISSING:
            return applied is not False
        return result is not False

    @staticmethod
    def _outcome_label(row: object) -> dict[str, object]:
        """Merge an indexed outcome row with its canonical nested label."""

        nested = _field(row, "label")
        if nested is None:
            nested = _field(row, "outcome")
        label = _mapping_copy(nested if nested is not None else row)
        for name in (
            "outcome_event_id",
            "event_id",
            "outcome_id",
            "episode_id",
            "horizon_id",
            "horizon_code",
            "status",
            "training_eligible",
            "available_at",
            "label_available_at",
            "sealed_at",
            "move_class",
            "direction",
        ):
            value = _field(row, name)
            if value is not None and label.get(name) is None:
                label[name] = _clone(value)
        return label

    @staticmethod
    def _is_sealed_observed(outcome: Mapping[str, object], now: datetime | None) -> bool:
        status = str(outcome.get("status") or "").strip().lower()
        if status != "observed" or outcome.get("training_eligible") is not True:
            return False
        if outcome.get("sealed") is False:
            return False
        available_at = _parse_timestamp(outcome.get("available_at") or outcome.get("label_available_at"))
        return now is not None and available_at is not None and available_at <= now

    def _invalidate_cohort_runtime_state(self) -> None:
        self._started_cutoffs = None
        self._admitted_ids_by_cohort = None
        self._blocked_lanes_by_cohort = None

    def _refresh_cohort_runtime_state(self) -> None:
        cutoffs: dict[str, datetime] = {}
        admitted: dict[str, set[str]] = {}
        blocked: dict[str, set[str]] = {}
        list_ids = getattr(self.store, "list_collecting_cohort_ids", None)
        load = getattr(self.store, "load", None)
        query = getattr(self.cohort_service, "query", None)
        if self.cohort_service is None or not callable(list_ids) or not callable(load) or query is None:
            self._started_cutoffs = cutoffs
            self._admitted_ids_by_cohort = admitted
            self._blocked_lanes_by_cohort = blocked
            return
        envelope_for = getattr(query, "envelope_for", None)
        if not callable(envelope_for):
            self._started_cutoffs = cutoffs
            self._admitted_ids_by_cohort = admitted
            self._blocked_lanes_by_cohort = blocked
            return
        for cohort_id in list_ids():
            try:
                cohort = load(cohort_id)
                started = getattr(cohort, "started_event", None)
                if started is None:
                    continue
                evidence = envelope_for(started).require_proven()
                cutoffs[started.event_id] = evidence.effective_ready_at
                refs: set[str] = set()
                for slot in getattr(cohort, "admitted_slots", ()):
                    refs.update(dict(slot.episode_refs_by_contract).values())
                admitted[str(cohort.cohort_id)] = refs
                blocked[str(cohort.cohort_id)] = {
                    lane_id
                    for lane_id, state in dict(getattr(cohort, "lane_states", {})).items()
                    if getattr(state, "status", None) is LaneOperationalStatus.BLOCKED
                }
            except Exception:  # noqa: BLE001 - missing start evidence stays fail-open
                continue
        self._started_cutoffs = cutoffs
        self._admitted_ids_by_cohort = admitted
        self._blocked_lanes_by_cohort = blocked

    def _cohort_runtime_state(self) -> tuple[dict[str, datetime], dict[str, set[str]], dict[str, set[str]]]:
        if (
            self._started_cutoffs is None
            or self._admitted_ids_by_cohort is None
            or self._blocked_lanes_by_cohort is None
        ):
            self._refresh_cohort_runtime_state()
        assert self._started_cutoffs is not None
        assert self._admitted_ids_by_cohort is not None
        assert self._blocked_lanes_by_cohort is not None
        return self._started_cutoffs, self._admitted_ids_by_cohort, self._blocked_lanes_by_cohort

    def _predictor_sees_episode(self, predictor: object, episode: object) -> bool:
        identity = getattr(predictor, "lane_identity", None)
        if identity is None:
            return True
        cutoffs, admitted, blocked = self._cohort_runtime_state()
        cutoff = cutoffs.get(str(getattr(identity, "started_event_id", "") or ""))
        if cutoff is None:
            return False
        as_of = _episode_as_of(episode)
        if as_of is None or as_of <= cutoff:
            return False
        cohort_id = str(getattr(identity, "study_cohort_id", "") or "")
        identifier = _episode_id(episode)
        if identifier is None or identifier not in admitted.get(cohort_id, set()):
            return False
        lane_id = str(getattr(identity, "lane_id", "") or "")
        if lane_id in blocked.get(cohort_id, set()):
            return False
        return True

    def _admit_collecting_slots(self, episodes: Iterable[object], report: dict[str, object]) -> None:
        if self.cohort_service is None:
            return
        list_ids = getattr(self.store, "list_collecting_cohort_ids", None)
        load = getattr(self.store, "load", None)
        admit = getattr(self.cohort_service, "admit_slot", None)
        query = getattr(self.cohort_service, "query", None)
        envelope_for = getattr(query, "envelope_for", None) if query is not None else None
        if not callable(list_ids) or not callable(load) or not callable(admit) or not callable(envelope_for):
            return
        for cohort_id in list_ids():
            try:
                cohort = load(cohort_id)
                started = getattr(cohort, "started_event", None)
                if started is None:
                    continue
                evidence = envelope_for(started).require_proven()
            except Exception:  # noqa: BLE001 - unproven start never blocks V1 shadow
                continue
            grouped: dict[tuple[str, str, str, str], dict[str, str]] = {}
            for episode in episodes:
                anchor = observation_market_anchor(episode)
                if anchor is None:
                    continue
                venue, symbol, interval, as_of = anchor
                if venue not in set(cohort.manifest.venues) or interval != cohort.manifest.bar_interval:
                    continue
                as_of_dt = _parse_timestamp(as_of)
                if as_of_dt is None or as_of_dt <= evidence.effective_ready_at:
                    continue
                identifier = _episode_id(episode)
                if identifier is None:
                    continue
                try:
                    contract_id = feature_contract_version_of(episode)
                except Exception:  # noqa: BLE001
                    continue
                if not contract_id:
                    continue
                grouped.setdefault(anchor, {}).setdefault(contract_id, identifier)
            for anchor, refs in grouped.items():
                try:
                    declared = {lane.feature_contract_id for lane in cohort.manifest.lanes}
                    filtered = {
                        contract_id: identifier
                        for contract_id, identifier in refs.items()
                        if contract_id in declared
                    }
                    if not filtered:
                        continue
                    venue, symbol, _interval, _as_of = anchor
                    resolution = _scope_resolution_for(
                        cohort,
                        venue=venue,
                        symbol=symbol,
                        resolver=self.scope_resolver,
                    )
                    slot = _slot_from_captured(
                        cohort,
                        anchor,
                        filtered,
                        started.event_id,
                        scope_resolution=resolution,
                    )
                    admit(AdmitWorldCohortSlot(slot=slot, started_evidence=evidence))
                except Exception as exc:  # noqa: BLE001 - admission failure stays shadow-local
                    self._error(report, stage="cohort_admit", error=exc)
        self._invalidate_cohort_runtime_state()


def _predictor_cohort_lineage(predictor: object | None) -> dict[str, str] | None:
    if predictor is None:
        return None
    identity = getattr(predictor, "lane_identity", None)
    if identity is None:
        return None
    contract = getattr(predictor, "feature_contract", None)
    mask = getattr(predictor, "feature_mask", None)
    contract_fp = getattr(contract, "fingerprint", None)
    mask_fp = getattr(mask, "fingerprint", None)
    study_cohort_id = str(getattr(identity, "study_cohort_id", "") or "").strip()
    lane_id = str(getattr(identity, "lane_id", "") or "").strip()
    manifest_sha256 = str(getattr(identity, "manifest_sha256", "") or "").strip()
    if not study_cohort_id or not lane_id or not manifest_sha256:
        return None
    if not isinstance(contract_fp, str) or not contract_fp.strip():
        return None
    if not isinstance(mask_fp, str) or not mask_fp.strip():
        return None
    return {
        "study_cohort_id": study_cohort_id,
        "lane_id": lane_id,
        "manifest_sha256": manifest_sha256,
        "feature_contract_fingerprint": contract_fp.strip(),
        "feature_mask_fingerprint": mask_fp.strip(),
    }


def _episode_as_of(episode: object) -> datetime | None:
    payload = _episode_payload(episode)
    observation = _field(payload, "observation")
    raw = _field(payload, "as_of_bar_ts")
    if raw is None and observation is not None:
        raw = _field(observation, "as_of_bar_ts")
    return _parse_timestamp(raw)


def _scope_resolution_for(
    cohort: object,
    *,
    venue: str,
    symbol: str,
    resolver: object | None,
) -> WorldScopeResolution | None:
    mapping = getattr(getattr(cohort, "manifest", None), "scope_mapping", None)
    if mapping is None:
        return None
    if resolver is None:
        raise ValueError("scope resolution is required when the manifest declares scope_mapping")
    resolve = getattr(resolver, "resolve", None)
    if not callable(resolve):
        raise TypeError("scope_resolver must expose resolve(anchor)")
    resolution = resolve(WorldMarketAnchorRef(market_venue=venue, instrument=symbol))
    if not isinstance(resolution, WorldScopeResolution):
        resolution = WorldScopeResolution.from_mapping(resolution)
    if resolution.mapping_id != mapping.mapping_id or resolution.mapping_sha256 != mapping.mapping_sha256:
        raise ValueError("scope resolution mapping identity must match the manifest")
    return resolution


def _slot_from_captured(
    cohort: object,
    anchor: tuple[str, str, str, str],
    refs: Mapping[str, str],
    started_event_id: str,
    *,
    scope_resolution: WorldScopeResolution | None = None,
) -> WorldCohortSlot:
    venue, symbol, interval, as_of = anchor
    manifest = cohort.manifest
    lanes = tuple(manifest.lanes)
    return WorldCohortSlot(
        cohort_id=cohort.cohort_id,
        manifest_sha256=manifest.manifest_sha256,
        venue=venue,
        symbol=symbol,
        bar_interval=interval,
        as_of_bar_ts=as_of,
        anchor_end_at=as_of,
        comparison_batch_id=_stable_id(
            "world-cohort-slot-batch",
            {
                "study_cohort_id": cohort.cohort_id,
                "venue": venue,
                "symbol": symbol,
                "bar_interval": interval,
                "as_of_bar_ts": as_of,
            },
        ),
        episode_refs_by_contract=dict(refs),
        expected_lane_ids=tuple(lane.lane_id for lane in lanes),
        feature_contract_fingerprints={lane.lane_id: lane.feature_contract_fingerprint for lane in lanes},
        feature_mask_fingerprints={lane.lane_id: lane.feature_mask_fingerprint for lane in lanes},
        started_event_id=started_event_id,
        scope_resolution=scope_resolution,
    )


WorldModelRuntime = WorldModelService


__all__ = [
    "DEFAULT_HORIZONS",
    "WorldModelRuntime",
    "WorldModelService",
]
