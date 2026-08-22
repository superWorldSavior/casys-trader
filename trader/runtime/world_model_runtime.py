"""Fail-open, shadow-only runtime for WorldEpisode predictions and labels.

This module deliberately has no dependency on the trading daemon, scheduler,
broker, RiskGate, or Brain.  It is an adapter around injected collaborators so
the world-model data path can be run beside trading without becoming an
authority over it.

The storage/domain implementations are intentionally duck-typed while they
are introduced incrementally.  The preferred store surface is:

``append_episode``, ``append_prediction``, ``append_outcome_event``,
``list_pending_episodes``, ``list_predictions`` and ``list_outcome_events``.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import inspect
import json
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone


# These are stable contract IDs, not aliases.  In particular, an elapsed 1d
# target must never borrow the 4h label when its own endpoint is unavailable.
DEFAULT_HORIZONS: tuple[str, ...] = ("elapsed_4h.v1", "elapsed_1d.v1")

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


def _episode_market_signature(value: object) -> str:
    """Fingerprint stable market evidence while ignoring repeat-fetch clocks."""

    payload = _mapping_copy(value)
    containers: list[dict[str, object]] = [payload]
    observation = payload.get("observation")
    if isinstance(observation, Mapping):
        detached_observation = copy.deepcopy(dict(observation))
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


def _observation_payload(episode: object) -> dict[str, object]:
    observation = _field(episode, "observation")
    if observation is None:
        return {}
    return _mapping_copy(observation)


def _stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _normalise_horizons(values: Iterable[object]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        raw = value if isinstance(value, str) else _field(value, "horizon_id", "horizon_code")
        text = str(raw or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _call_compatible(
    fn: Callable[..., object],
    variants: Iterable[tuple[tuple[object, ...], dict[str, object]]],
) -> object:
    """Invoke the first signature-compatible variant without swallowing body errors."""

    choices = tuple(variants)
    if not choices:
        raise TypeError("no_call_variants")
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        args, kwargs = choices[0]
        return fn(*args, **kwargs)

    for args, kwargs in choices:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return fn(*args, **kwargs)
    args, kwargs = choices[0]
    return fn(*args, **kwargs)


class WorldModelRuntime:
    """A synchronous, single-flight, non-authoritative WorldEpisode worker.

    All collaborators are injected.  A collaborator may use the preferred
    methods documented above, or compatible aliases retained while the domain
    and store land in parallel.  Failures are recorded in the returned report
    and logger only; they never propagate to the caller.
    """

    def __init__(
        self,
        *,
        store: object,
        predictor: object | None,
        labeler: object | None,
        bar_provider: object | None,
        horizons: Iterable[str] = DEFAULT_HORIZONS,
        logger: object | None = None,
        lookback: str = "5d",
        run_id: str = "world_shadow.v1",
    ) -> None:
        normalized = _normalise_horizons(horizons)
        self.store = store
        self.predictor = predictor
        self.labeler = labeler
        self.bar_provider = bar_provider
        self.horizons = normalized or DEFAULT_HORIZONS
        self.lookback = str(lookback)
        self.run_id = str(run_id).strip() or "world_shadow.v1"
        self.log = logger or logging.getLogger("casys-trader")
        self._lock = threading.Lock()
        self._running: str | None = None
        self._last_report: dict[str, object] = {}
        self._prediction_keys: set[tuple[str, str]] = set()
        self._baseline_hydrated = False

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
            self._capture_episodes(episodes, report)
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

    def _capture_episodes(self, episodes: Iterable[object], report: dict[str, object]) -> None:
        try:
            iterator = iter(episodes)
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="episodes_iter", error=exc)
            return

        while True:
            try:
                raw_episode = next(iterator)
            except StopIteration:
                return
            except Exception as exc:  # noqa: BLE001
                self._error(report, stage="episodes_iter", error=exc)
                return
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
                existing = self._load_existing_episode(identifier, episode)
            except Exception as exc:  # noqa: BLE001 - shadow conflict never escapes
                self._error(
                    report,
                    stage="get_episode",
                    error=exc,
                    episode=episode,
                )
                continue
            if existing is not None:
                report["episodes_existing"] = int(report["episodes_existing"]) + 1
                canonical_episode = existing
            else:
                try:
                    appended = self._store_append("append_episode", _clone(episode))
                except Exception as exc:  # noqa: BLE001
                    self._error(report, stage="append_episode", error=exc, episode=episode)
                    continue
                if appended is False:
                    report["episodes_existing"] = int(report["episodes_existing"]) + 1
                else:
                    report["episodes_appended"] = int(report["episodes_appended"]) + 1

            for horizon in self.horizons:
                self._predict_one(canonical_episode, horizon, report)

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

        fn = self._store_method("get_episode")
        if fn is None:
            return None
        stored = _call_compatible(fn, (((episode_id,), {}),))
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
        key = (identifier, horizon)
        if key in self._prediction_keys:
            report["predictions_existing"] = int(report["predictions_existing"]) + 1
            return
        if self.predictor is None:
            self._error(report, stage="predict", error=RuntimeError("predictor_unavailable"), episode=episode, horizon=horizon)
            return
        try:
            prediction = self._predict(episode, horizon, _parse_timestamp(report["as_of"]))
            if prediction is None:
                raise ValueError("empty_prediction")
            event = self._prediction_event(prediction, episode, identifier, horizon, report["as_of"])
            appended = self._store_append("append_prediction", _clone(event))
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="predict", error=exc, episode=episode, horizon=horizon)
            return
        self._prediction_keys.add(key)
        if appended is False:
            report["predictions_existing"] = int(report["predictions_existing"]) + 1
        else:
            report["predictions_appended"] = int(report["predictions_appended"]) + 1

    def _predict(self, episode: object, horizon: str, predicted_at: datetime | None) -> object:
        predictor = self.predictor
        assert predictor is not None
        fn = getattr(predictor, "predict", predictor)
        if not callable(fn):
            raise TypeError("predictor_not_callable")
        return _call_compatible(
            fn,
            (
                ((_clone(episode), horizon), {"prediction_at": predicted_at}),
                ((_clone(episode),), {"horizon_id": horizon, "prediction_at": predicted_at}),
                ((_clone(episode),), {"horizon_code": horizon, "prediction_at": predicted_at}),
                ((_clone(episode),), {"horizon": horizon, "prediction_at": predicted_at}),
                ((_clone(episode), horizon), {}),
                ((_clone(episode),), {"horizon_id": horizon}),
                ((_clone(episode),), {"horizon_code": horizon}),
                ((_clone(episode),), {"horizon": horizon}),
            ),
        )

    def _prediction_event(
        self,
        prediction: object,
        episode: object,
        episode_id: str,
        horizon: str,
        predicted_at: object,
    ) -> dict[str, object]:
        """Normalize a prediction into the append-only store envelope.

        The nested ``prediction`` remains the predictor's detached payload;
        the surrounding fields make older and newer store variants equally
        replayable without asking the predictor to know persistence details.
        """

        payload = _mapping_copy(prediction)
        prediction_id = str(payload.get("prediction_id") or "").strip()
        if not prediction_id:
            prediction_id = _stable_id(
                "world-shadow-prediction",
                {
                    "episode_id": episode_id,
                    "horizon_id": horizon,
                    "run_id": str(payload.get("run_id") or self.run_id),
                    "payload": payload,
                },
            )
        model_kind = payload.get("model_kind") or payload.get("model_id") or payload.get("kind")
        model_version = payload.get("model_version") or payload.get("version")
        return {
            "prediction_id": prediction_id,
            "run_id": payload.get("run_id") or payload.get("model_run_id") or self.run_id,
            "episode_id": episode_id,
            "horizon_id": payload.get("horizon_id") or horizon,
            "horizon_code": payload.get("horizon_code") or payload.get("horizon_id") or horizon,
            "model_kind": model_kind,
            "model_version": model_version,
            "predicted_at": payload.get("predicted_at") or payload.get("created_at") or predicted_at,
            "input": _observation_payload(episode),
            "prediction": payload,
        }

    def _hydrate_prediction_keys(self, report: dict[str, object]) -> None:
        fn = self._store_method("list_predictions")
        if fn is None:
            return
        try:
            rows = _call_compatible(fn, (((), {}),))
            for row in list(rows or []):
                identifier = _episode_id(row)
                horizon = _horizon_id(row)
                if identifier and horizon:
                    self._prediction_keys.add((identifier, horizon))
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="list_predictions", error=exc)

    def _hydrate_baseline(self, report: dict[str, object], now: datetime | None) -> None:
        """Replay only durable causal labels into an in-memory shadow baseline.

        The predictor has no persistence authority of its own.  Rebuilding it
        from the append-only outcome ledger keeps a daemon/process restart from
        silently resetting progressive world learning.  This never consults a
        broker, decision, or scheduler store.
        """

        if self._baseline_hydrated or self.predictor is None:
            return
        apply = self._baseline_apply_method()
        if apply is None:
            self._baseline_hydrated = True
            return
        fn = self._store_method("list_observed_outcomes", "list_outcome_events")
        episode_fn = self._store_method("get_episode")
        if fn is None or episode_fn is None:
            self._baseline_hydrated = True
            return
        try:
            rows = _call_compatible(
                fn,
                (
                    ((), {"active_only": True, "training_eligible": True}),
                    ((), {"active_only": True, "status": "observed", "training_eligible": True}),
                    ((), {"training_eligible": True}),
                    ((), {"status": "observed", "training_eligible": True}),
                    ((), {"status": "observed"}),
                    ((), {}),
                ),
            )
            ordered = sorted(
                list(rows or []),
                key=lambda row: (
                    _parse_timestamp(
                        _field(row, "label_available_at", "available_at", "sealed_at")
                    )
                    or datetime.max.replace(tzinfo=timezone.utc),
                    str(_episode_id(row) or ""),
                    str(_horizon_id(row) or ""),
                    str(_field(row, "outcome_event_id", "event_id", "outcome_id") or ""),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="baseline_hydrate_list", error=exc)
            return

        completed = True
        for row in ordered:
            try:
                label = self._outcome_label(row)
                if not self._is_sealed_observed(label, now):
                    completed = False
                    continue
                identifier = _episode_id(row) or _episode_id(label)
                if identifier is None:
                    raise ValueError("missing_episode_id")
                stored_episode = _call_compatible(episode_fn, (((identifier,), {}),))
                if stored_episode is None:
                    raise ValueError("episode_not_found")
                episode = _clone(_episode_payload(stored_episode))
                applied = self._call_baseline_apply(apply, label, episode, now)
                if applied is not False:
                    report["baseline_replayed"] = int(report["baseline_replayed"]) + 1
            except Exception as exc:  # noqa: BLE001
                completed = False
                self._error(report, stage="baseline_hydrate", error=exc, episode=row, horizon=_horizon_id(row))
        self._baseline_hydrated = completed

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
        fn = self._store_method("list_pending_episodes", "load_pending_episodes", "pending_episodes")
        if fn is None:
            raise RuntimeError("pending_episode_store_unavailable")
        rows = _call_compatible(
            fn,
            (
                ((), {"horizon_code": horizon, "training_eligible": True}),
                ((), {"horizon_id": horizon, "training_eligible": True}),
                ((), {"horizon_code": horizon}),
                ((), {"horizon_id": horizon}),
                ((horizon,), {}),
                ((), {}),
            ),
        )
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
            appended = self._store_append("append_outcome_event", _clone(event))
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

        label = _mapping_copy(outcome)
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
            )
            if key in label
        }
        return {
            "outcome_event_id": outcome_event_id,
            "episode_id": episode_id,
            "horizon_id": str(label.get("horizon_id") or horizon),
            "horizon_code": str(label.get("horizon_code") or label.get("horizon_id") or horizon),
            "label_schema_version": label.get("label_schema_version") or label.get("label_semantics_version"),
            "status": label.get("status"),
            "move_class": label.get("move_class") or label.get("direction"),
            "training_eligible": label.get("training_eligible"),
            "label_available_at": label.get("label_available_at") or label.get("available_at"),
            "sealed_at": label.get("sealed_at") or label.get("computed_at"),
            "label": label,
            "evidence": evidence,
        }

    def _fetch_bars(self, episode: object, now: datetime) -> object:
        provider = self.bar_provider
        if provider is None:
            raise RuntimeError("bar_provider_unavailable")
        fn = getattr(provider, "get_bars", provider)
        if not callable(fn):
            raise TypeError("bar_provider_not_callable")
        symbol = _symbol(episode)
        if symbol is None:
            raise ValueError("missing_symbol")
        interval = _bar_interval(episode)
        return _call_compatible(
            fn,
            (
                ((symbol,), {"lookback": self.lookback, "interval": interval}),
                ((symbol,), {"interval": interval}),
                ((symbol,), {}),
                ((_clone(episode),), {"now": now}),
                ((_clone(episode),), {}),
            ),
        )

    def _label(self, episode: object, bars: object, horizon: str, now: datetime) -> object:
        labeler = self.labeler
        if labeler is None:
            raise RuntimeError("labeler_unavailable")
        label_horizon = getattr(labeler, "label_horizon", None)
        if not callable(label_horizon) and callable(labeler):
            # Accept injection of the pure ``label_horizon`` function directly
            # as well as a module/object exposing it.
            if getattr(labeler, "__name__", "") == "label_horizon":
                label_horizon = labeler
        if callable(label_horizon):
            return _call_compatible(
                label_horizon,
                (
                    ((_clone(episode), _clone(bars), horizon), {"now": now}),
                    ((_clone(episode), _clone(bars)), {"horizon_id": horizon, "now": now}),
                    ((_clone(episode), _clone(bars)), {"horizon_code": horizon, "now": now}),
                ),
            )

        label_episode = getattr(labeler, "label_episode", labeler)
        if not callable(label_episode):
            raise TypeError("labeler_not_callable")
        labelled = _call_compatible(
            label_episode,
            (
                ((_clone(episode), _clone(bars)), {"now": now, "horizons": (horizon,)}),
                ((_clone(episode), _clone(bars)), {"horizons": (horizon,), "now": now}),
                ((_clone(episode), _clone(bars)), {"horizon_id": horizon, "now": now}),
            ),
        )
        if isinstance(labelled, Mapping):
            return labelled
        for outcome in list(labelled or []):
            if _horizon_id(outcome) in (None, horizon):
                return outcome
        raise ValueError("missing_horizon_outcome")

    def _apply_baseline(
        self,
        outcome: Mapping[str, object],
        episode: object,
        now: datetime,
        report: dict[str, object],
        horizon: str,
    ) -> None:
        fn = self._baseline_apply_method()
        if not callable(fn):
            return
        try:
            applied = self._call_baseline_apply(fn, outcome, episode, now)
        except Exception as exc:  # noqa: BLE001
            self._error(report, stage="baseline", error=exc, episode=episode, horizon=horizon)
            return
        if applied is not False:
            report["baseline_updates"] = int(report["baseline_updates"]) + 1

    def _baseline_apply_method(self) -> Callable[..., object] | None:
        predictor = self.predictor
        if predictor is None:
            return None
        for name in ("apply_outcome", "learn", "update", "update_baseline"):
            candidate = getattr(predictor, name, None)
            if callable(candidate):
                return candidate
        return None

    @staticmethod
    def _call_baseline_apply(
        fn: Callable[..., object],
        outcome: Mapping[str, object],
        episode: object,
        now: datetime | None,
    ) -> bool | None:
        result = _call_compatible(
            fn,
            (
                ((_clone(outcome), _clone(episode)), {"available_through": now}),
                ((_clone(outcome), _clone(episode)), {"now": now}),
                ((_clone(outcome), _clone(episode)), {}),
            ),
        )
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

    def _store_method(self, *names: str) -> Callable[..., object] | None:
        for name in names:
            candidate = getattr(self.store, name, None)
            if callable(candidate):
                return candidate
        return None

    def _store_append(self, method_name: str, payload: object) -> object:
        aliases = {
            "append_episode": ("append_episode",),
            "append_prediction": ("append_prediction", "append_prediction_event"),
            "append_outcome_event": ("append_outcome_event", "append_outcome", "append_label"),
        }
        fn = self._store_method(*aliases[method_name])
        if fn is None:
            raise RuntimeError(f"store_method_unavailable:{method_name}")
        return _call_compatible(fn, (((payload,), {}), (((), {"payload": payload}))))


@dataclasses.dataclass(frozen=True)
class _BackgroundSnapshot:
    episodes: tuple[object, ...]
    bars_by_symbol: object
    now: datetime
    reason: str


class WorldModelBackgroundRunner:
    """Coalescing background wrapper around :class:`WorldModelRuntime`.

    ``trigger`` freezes caller-owned observations and bars, then returns as
    soon as a daemon worker is started or a latest snapshot is queued.  The
    worker always matures old episodes before it captures/predicts the new
    snapshot.  It is strictly shadow maintenance: no scheduler, broker,
    RiskGate, Brain, or decision callback is accepted by this class.
    """

    def __init__(
        self,
        *,
        runtime: WorldModelRuntime,
        logger: object | None = None,
        thread_name: str = "world-model-shadow",
    ) -> None:
        self.runtime = runtime
        self.log = logger or logging.getLogger("casys-trader")
        self.thread_name = str(thread_name).strip() or "world-model-shadow"
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
            return {
                **copy.deepcopy(self._last_report),
                "running": self._thread is not None,
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
                    {"status": "partial", "stage": "stop", "error": f"{type(exc).__name__}:{exc}"}
                )

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
                report["mature"] = _call_compatible(
                    self.runtime.mature_pending,
                    (
                        ((current.now,), {"bars_by_symbol": _clone(current.bars_by_symbol)}),
                        ((current.now,), {}),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - runtime is shadow-only even if its contract breaks
                self._background_error(report, stage="mature", error=exc)
            try:
                report["capture"] = _call_compatible(
                    self.runtime.capture_and_predict,
                    (
                        ((_clone(current.episodes),), {"now": current.now}),
                        ((_clone(current.episodes),), {}),
                    ),
                )
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


WorldModelShadowRuntime = WorldModelRuntime
WorldModelRunner = WorldModelRuntime


__all__ = [
    "DEFAULT_HORIZONS",
    "WorldModelBackgroundRunner",
    "WorldModelRunner",
    "WorldModelRuntime",
    "WorldModelShadowRuntime",
]
