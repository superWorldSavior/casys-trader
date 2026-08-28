from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
from trader.application.world_model.gru import OnlineGRUWorldChallenger
from trader.application.world_model.labeler import DEFAULT_HORIZONS as LABEL_HORIZONS
from trader.application.world_model.labeler import label_horizon
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
)
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.application.world_model.service import WorldModelService
from trader.runtime.world_model_runtime import WorldModelBackgroundRunner, WorldModelRuntime


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


def _domain_episode(at: datetime, *, symbol: str = "SPY", market_return: float = 0.01) -> WorldEpisode:
    return WorldEpisode(
        observation=WorldObservation(
            venue="US",
            symbol=symbol,
            bar_interval="1h",
            as_of_bar_ts=at,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            sampling_policy_version="fresh-active-v1",
            anchor=AnchorBar(
                ts=at,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000.0,
                source="fixture",
            ),
            available_at=at,
            captured_at=at + timedelta(minutes=1),
            freshness="fresh",
            categorical_features={
                "asset_family": "equities",
                "venue": "US",
                "bar_interval": "1h",
                "session_phase": "regular",
                "market_regime": "trend_up" if market_return >= 0 else "trend_down",
                "volatility_state": "normal",
            },
            numeric_features={"return": market_return, "atr_pct": 0.01},
        )
    )


def _episode(episode_id: str, symbol: str = "SPY") -> dict[str, object]:
    return {
        "episode_id": episode_id,
        "observation": {
            "symbol": symbol,
            "bar_interval": "1h",
            "features": {"close": 100.0},
        },
    }


class MemoryWorldStore:
    def __init__(self, episodes: list[dict[str, object]] | None = None) -> None:
        self.episodes = {str(row["episode_id"]): copy.deepcopy(row) for row in episodes or []}
        self.predictions: dict[tuple[str, str], dict[str, object]] = {}
        self.outcomes: dict[tuple[str, str], dict[str, object]] = {}
        self.events: list[tuple[str, object]] = []

    def append_episode(self, episode: object) -> bool:
        row = copy.deepcopy(episode)
        assert isinstance(row, dict)
        identifier = str(row["episode_id"])
        self.events.append(("episode", identifier))
        if identifier in self.episodes:
            return False
        self.episodes[identifier] = row
        return True

    def append_prediction(self, prediction: object) -> bool:
        row = copy.deepcopy(prediction)
        assert isinstance(row, dict)
        key = (str(row["episode_id"]), str(row["horizon_id"]))
        self.events.append(("prediction", key))
        if key in self.predictions:
            return False
        self.predictions[key] = row
        return True

    def append_outcome_event(self, outcome: object) -> bool:
        row = copy.deepcopy(outcome)
        assert isinstance(row, dict)
        key = (str(row["episode_id"]), str(row["horizon_id"]))
        self.events.append(("outcome", key))
        if key in self.outcomes:
            return False
        self.outcomes[key] = row
        return True

    def get_episode(self, episode_id: str) -> dict[str, object] | None:
        row = self.episodes.get(episode_id)
        return None if row is None else copy.deepcopy(row)

    def list_eligible_episodes(self) -> list[dict[str, object]]:
        return [
            copy.deepcopy(episode)
            for episode in self.episodes.values()
            if episode.get("training_eligible") is not False
        ]

    def list_predictions(self) -> list[dict[str, object]]:
        return list(self.predictions.values())

    def list_prediction_identities(self) -> list[dict[str, object]]:
        return list(self.predictions.values())

    def list_pending_episodes(
        self,
        *,
        horizon_id: str | None = None,
        training_eligible: bool | None = None,
    ) -> list[dict[str, object]]:
        assert horizon_id is not None
        terminal = {"observed", "missing", "unknown"}
        rows = []
        for episode_id, episode in self.episodes.items():
            if training_eligible is True and episode.get("training_eligible") is False:
                continue
            if self.outcomes.get((episode_id, horizon_id), {}).get("status") not in terminal:
                rows.append(copy.deepcopy(episode))
        return rows

    def list_outcome_events(self, **_kwargs) -> list[dict[str, object]]:
        return list(self.outcomes.values())

    def list_observed_outcomes(self, **kwargs) -> list[dict[str, object]]:
        return [
            row
            for row in self.outcomes.values()
            if str(row.get("status") or "").lower() == "observed"
            and (kwargs.get("training_eligible") is not True or row.get("training_eligible") is True)
        ]


class RecordingPredictor:
    model_id = "recording_predictor"
    model_version = "v1"

    def __init__(self) -> None:
        self.predictions: list[tuple[str, str]] = []
        self.baseline_updates: list[tuple[str, str]] = []

    def predict(
        self,
        episode: dict[str, object],
        horizon_id: str,
        *,
        prediction_at: datetime | None = None,
    ) -> dict[str, object]:
        episode["mutated_by_predictor"] = True
        identifier = str(episode["episode_id"])
        self.predictions.append((identifier, horizon_id))
        return {
            "prediction_id": f"pred:{identifier}:{horizon_id}",
            "episode_id": identifier,
            "horizon_id": horizon_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "created_at": (prediction_at or NOW).isoformat(),
            "probabilities": {"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
            "predicted_class": "UP",
            "status": "shadow_only",
            "recommendation": "NO_GO",
            "authority": "shadow_only",
            "decision_effect": "none",
        }

    def apply_outcome(
        self,
        outcome: dict[str, object],
        observation: dict[str, object],
        *,
        available_through: datetime,
    ) -> None:
        assert outcome["status"] == "observed"
        assert outcome["training_eligible"] is True
        assert available_through == NOW
        observation["mutated_by_baseline"] = True
        self.baseline_updates.append((str(outcome["episode_id"]), str(outcome["horizon_id"])))


class RecordingBars:
    def __init__(self, *, fail_symbols: set[str] | None = None) -> None:
        self.fail_symbols = fail_symbols or set()
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol: str, *, lookback: str, interval: str) -> list[dict[str, object]]:
        self.calls.append((symbol, lookback, interval))
        if symbol in self.fail_symbols:
            raise OSError(f"unavailable:{symbol}")
        return [{"ts": NOW.isoformat(), "close": 100.0}]


class RecordingLabeler:
    def __init__(
        self, *, fail_horizons: set[tuple[str, str]] | None = None, pending_horizons: set[str] | None = None
    ) -> None:
        self.fail_horizons = fail_horizons or set()
        self.pending_horizons = pending_horizons or set()
        self.calls: list[tuple[str, str]] = []

    def label_horizon(
        self,
        episode: dict[str, object],
        bars: list[dict[str, object]],
        horizon_id: str,
        *,
        now: datetime,
    ) -> dict[str, object]:
        identifier = str(episode["episode_id"])
        self.calls.append((identifier, horizon_id))
        assert bars
        if (identifier, horizon_id) in self.fail_horizons:
            raise ValueError(f"bad_horizon:{horizon_id}")
        if horizon_id in self.pending_horizons:
            return {
                "outcome_event_id": f"out:{identifier}:{horizon_id}",
                "episode_id": identifier,
                "horizon_id": horizon_id,
                "status": "pending",
                "training_eligible": False,
                "available_at": now.isoformat(),
            }
        return {
            "outcome_event_id": f"out:{identifier}:{horizon_id}",
            "episode_id": identifier,
            "horizon_id": horizon_id,
            "status": "observed",
            "training_eligible": True,
            "sealed": True,
            "available_at": now.isoformat(),
            "forward_return": 0.01,
        }


def _runtime(
    store: MemoryWorldStore,
    predictor: RecordingPredictor | None = None,
    labeler: RecordingLabeler | None = None,
    bars: RecordingBars | None = None,
) -> WorldModelRuntime:
    return WorldModelService(
        store=store,
        predictor=predictor or RecordingPredictor(),
        labeler=labeler or RecordingLabeler(),
        bar_provider=bars or RecordingBars(),
    )


def test_capture_store_oserror_is_fail_open_and_input_is_detached() -> None:
    class OSErrorStore(MemoryWorldStore):
        def append_episode(self, episode: object) -> bool:
            assert isinstance(episode, dict)
            observation = episode["observation"]
            assert isinstance(observation, dict)
            observation["features"]["close"] = -1.0
            raise OSError("disk full")

    original = _episode("e-oserror")
    predictor = RecordingPredictor()
    labeler = RecordingLabeler()
    bars = RecordingBars()

    report = _runtime(OSErrorStore(), predictor, labeler, bars).capture_and_predict([original])

    assert report["status"] == "partial"
    assert report["predictions_appended"] == 0
    assert report["errors"][0]["stage"] == "append_episode"
    assert original["observation"]["features"]["close"] == 100.0
    assert "mutated_by_predictor" not in original
    assert predictor.predictions == []
    assert labeler.calls == []
    assert bars.calls == []


def test_capture_appends_each_prediction_before_any_label_and_is_idempotent() -> None:
    store = MemoryWorldStore()
    predictor = RecordingPredictor()
    labeler = RecordingLabeler()
    bars = RecordingBars()
    runtime = _runtime(store, predictor, labeler, bars)
    original = _episode("e-capture")

    first = runtime.capture_and_predict([original])
    second = runtime.capture_and_predict([original])

    assert first["episodes_appended"] == 1
    assert first["predictions_appended"] == 2
    assert second["errors"] == []
    assert second["episodes_existing"] == 1
    assert second["episodes_appended"] == 0
    assert second["predictions_appended"] == 0
    assert predictor.predictions == [
        ("e-capture", "elapsed_4h.v1"),
        ("e-capture", "elapsed_1d.v1"),
    ]
    assert store.events == [
        ("episode", "e-capture"),
        ("prediction", ("e-capture", "elapsed_4h.v1")),
        ("prediction", ("e-capture", "elapsed_1d.v1")),
    ]
    assert labeler.calls == []
    assert bars.calls == []
    assert "mutated_by_predictor" not in original


def test_mature_keeps_bad_symbol_and_bad_horizon_from_blocking_other_horizon() -> None:
    store = MemoryWorldStore([_episode("e-bad", "BAD"), _episode("e-good", "GOOD")])
    predictor = RecordingPredictor()
    labeler = RecordingLabeler(fail_horizons={("e-good", "elapsed_4h.v1")})
    bars = RecordingBars(fail_symbols={"BAD"})

    report = _runtime(store, predictor, labeler, bars).mature_pending(NOW)

    assert report["status"] == "partial"
    assert ("e-good", "elapsed_1d.v1") in store.outcomes
    assert ("e-good", "elapsed_4h.v1") not in store.outcomes
    assert all(key[0] != "e-bad" for key in store.outcomes)
    assert predictor.baseline_updates == [("e-good", "elapsed_1d.v1")]
    errors = {(item.get("episode_id"), item.get("horizon_id")) for item in report["errors"]}
    assert ("e-bad", "elapsed_4h.v1") in errors
    assert ("e-bad", "elapsed_1d.v1") in errors
    assert ("e-good", "elapsed_4h.v1") in errors
    assert ("e-good", "elapsed_1d.v1") not in errors


def test_mature_outcomes_are_independent_and_rerun_does_not_retrain_baseline() -> None:
    store = MemoryWorldStore([_episode("e-independent")])
    predictor = RecordingPredictor()
    labeler = RecordingLabeler(pending_horizons={"elapsed_1d.v1"})
    runtime = _runtime(store, predictor, labeler, RecordingBars())

    first = runtime.mature_pending(NOW)
    second = runtime.mature_pending(NOW)

    assert first["outcomes_appended"] == 1
    assert first["outcomes_pending"] == 1
    assert store.outcomes[("e-independent", "elapsed_4h.v1")]["status"] == "observed"
    assert ("e-independent", "elapsed_1d.v1") not in store.outcomes
    assert predictor.baseline_updates == [("e-independent", "elapsed_4h.v1")]
    assert second["outcomes_appended"] == 0
    assert second["baseline_updates"] == 0
    assert len(store.outcomes) == 1


def test_missing_outcome_is_persisted_once_and_stops_unbounded_rematuration() -> None:
    class MissingLabeler(RecordingLabeler):
        def label_horizon(
            self,
            episode: dict[str, object],
            bars: object,
            horizon_id: str,
            *,
            now: datetime,
        ) -> dict[str, object]:
            assert bars
            return {
                "outcome_event_id": f"missing:{episode['episode_id']}:{horizon_id}",
                "episode_id": episode["episode_id"],
                "horizon_id": horizon_id,
                "status": "missing",
                "training_eligible": False,
                "computed_at": now.isoformat(),
                "reason": "endpoint_missing_within_lateness",
            }

    store = MemoryWorldStore([_episode("e-terminal")])
    predictor = RecordingPredictor()
    provider = RecordingBars()
    runtime = _runtime(store, predictor, MissingLabeler(), provider)

    first = runtime.mature_pending(NOW)
    second = runtime.mature_pending(NOW + timedelta(hours=1))

    assert first["outcomes_appended"] == 2
    assert first["outcomes_terminal"] == 2
    assert second["outcomes_appended"] == 0
    assert provider.calls == [("SPY", "5d", "1h")]
    assert predictor.baseline_updates == []

    # A superseding pending correction makes the store authoritative slot
    # pending again.  The same live runtime must not retain a stale terminal
    # cache until restart.
    for outcome in store.outcomes.values():
        outcome["status"] = "pending"
    third = runtime.mature_pending(NOW + timedelta(hours=2))
    assert third["outcomes_existing"] == 2
    assert provider.calls == [("SPY", "5d", "1h"), ("SPY", "5d", "1h")]


def test_mature_fetches_one_series_per_symbol_interval_and_can_reuse_cycle_bars() -> None:
    episodes = [_episode("e-cache-one", "CACHE"), _episode("e-cache-two", "CACHE")]
    store = MemoryWorldStore(episodes)
    provider = RecordingBars()

    fetched = _runtime(store, RecordingPredictor(), RecordingLabeler(), provider).mature_pending(NOW)

    assert provider.calls == [("CACHE", "5d", "1h")]
    assert fetched["bar_provider_fetches"] == 1
    assert fetched["bars_snapshot_reused"] == 0
    assert len(store.outcomes) == 4

    snapshot_store = MemoryWorldStore(episodes)
    no_io = RecordingBars(fail_symbols={"CACHE"})
    snapshot = [{"ts": NOW.isoformat(), "close": 100.0}]
    reused = _runtime(snapshot_store, RecordingPredictor(), RecordingLabeler(), no_io).mature_pending(
        NOW,
        bars_by_symbol={"CACHE": snapshot},
    )

    assert no_io.calls == []
    assert reused["bar_provider_fetches"] == 0
    assert reused["bars_snapshot_reused"] == 1
    assert len(snapshot_store.outcomes) == 4


def test_supplied_snapshot_never_falls_back_to_post_snapshot_market_io() -> None:
    class PendingWithoutBars(RecordingLabeler):
        def label_horizon(
            self,
            episode: dict[str, object],
            bars: object,
            horizon_id: str,
            *,
            now: datetime,
        ) -> dict[str, object]:
            assert not bars
            return {
                "outcome_event_id": f"pending:{episode['episode_id']}:{horizon_id}",
                "episode_id": episode["episode_id"],
                "horizon_id": horizon_id,
                "status": "pending",
                "training_eligible": False,
                "available_at": now.isoformat(),
            }

    store = MemoryWorldStore([_episode("e-not-in-snapshot", "ABSENT")])
    provider = RecordingBars(fail_symbols={"ABSENT"})
    report = _runtime(
        store,
        RecordingPredictor(),
        PendingWithoutBars(),
        provider,
    ).mature_pending(NOW, bars_by_symbol={})

    assert provider.calls == []
    assert report["bar_provider_fetches"] == 0
    assert report["outcomes_pending"] == 2
    assert report["errors"] == []


def test_single_flight_returns_a_shadow_skip_without_invoking_market_or_trade_paths() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingStore(MemoryWorldStore):
        def append_episode(self, episode: object) -> bool:
            entered.set()
            assert release.wait(timeout=2)
            return super().append_episode(episode)

    store = BlockingStore()
    runtime = _runtime(store)
    thread = threading.Thread(target=lambda: runtime.capture_and_predict([_episode("e-lock")]))
    thread.start()
    assert entered.wait(timeout=2)

    skipped = runtime.mature_pending(NOW)
    release.set()
    thread.join(timeout=2)

    assert skipped == {
        "operation": "mature_pending",
        "status": "skipped",
        "reason": "single_flight_busy",
        "running": "capture_and_predict",
        "errors": [],
    }


def test_background_runner_returns_immediately_coalesces_and_freezes_snapshots() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingRuntime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def mature_pending(self, now: datetime, *, bars_by_symbol=None) -> dict[str, object]:
            self.calls.append(("mature", copy.deepcopy(bars_by_symbol)))
            if len([name for name, _value in self.calls if name == "mature"]) == 1:
                entered.set()
                assert release.wait(timeout=2)
            return {"status": "ok", "as_of": now.isoformat()}

        def capture_and_predict(self, episodes) -> dict[str, object]:
            self.calls.append(("capture", copy.deepcopy(tuple(episodes))))
            return {"status": "ok"}

    runtime = BlockingRuntime()
    runner = WorldModelBackgroundRunner(runtime=runtime)  # type: ignore[arg-type]
    first_episode = _episode("e-background-one")
    first_bars = {"SPY": [{"close": 100.0}]}

    first = runner.trigger(episodes=[first_episode], now=NOW, bars_by_symbol=first_bars, reason="first")
    assert first["triggered"] is True
    assert entered.wait(timeout=2)

    first_episode["observation"]["features"]["close"] = -1.0
    first_bars["SPY"][0]["close"] = -1.0
    second = runner.trigger(
        episodes=[_episode("e-background-two")],
        now=NOW + timedelta(minutes=1),
        bars_by_symbol={"SPY": [{"close": 101.0}]},
        reason="latest",
    )
    assert second == {"triggered": False, "reason": "queued_latest"}

    release.set()
    first["_thread"].join(timeout=2)  # type: ignore[index,union-attr]

    assert runtime.calls == [
        ("mature", {"SPY": [{"close": 100.0}]}),
        ("capture", (_episode("e-background-one"),)),
        ("mature", {"SPY": [{"close": 101.0}]}),
        ("capture", (_episode("e-background-two"),)),
    ]
    assert runner.status()["running"] is False


def test_background_runner_catches_runtime_failure_and_still_captures() -> None:
    class FailingRuntime:
        def __init__(self) -> None:
            self.captured = 0

        def mature_pending(self, _now: datetime, **_kwargs) -> dict[str, object]:
            raise OSError("label store unavailable")

        def capture_and_predict(self, _episodes) -> dict[str, object]:
            self.captured += 1
            return {"status": "ok"}

    runtime = FailingRuntime()
    runner = WorldModelBackgroundRunner(runtime=runtime)  # type: ignore[arg-type]
    triggered = runner.trigger(episodes=[_episode("e-background-fail")], now=NOW)
    triggered["_thread"].join(timeout=2)  # type: ignore[index,union-attr]

    assert runtime.captured == 1
    status = runner.status()
    assert status["status"] == "partial"
    assert status["errors"][0]["stage"] == "mature"


def test_background_runner_preserves_the_snapshot_clock_for_delayed_prediction() -> None:
    class SnapshotClockRuntime:
        def __init__(self) -> None:
            self.mature_clock: datetime | None = None
            self.capture_clock: datetime | None = None

        def mature_pending(
            self,
            now: datetime,
            *,
            bars_by_symbol=None,
        ) -> dict[str, object]:
            self.mature_clock = now
            return {"status": "ok", "bars": bars_by_symbol}

        def capture_and_predict(
            self,
            _episodes,
            *,
            now: datetime,
        ) -> dict[str, object]:
            # This represents a worker that happens to execute much later.  Its
            # causal cutoff must still be the frozen snapshot clock.
            self.capture_clock = now
            return {"status": "ok", "as_of": now.isoformat()}

    runtime = SnapshotClockRuntime()
    runner = WorldModelBackgroundRunner(runtime=runtime)  # type: ignore[arg-type]
    triggered = runner.trigger(episodes=[_episode("e-clock")], now=NOW)
    triggered["_thread"].join(timeout=2)  # type: ignore[index,union-attr]

    assert runtime.mature_clock == NOW
    assert runtime.capture_clock == NOW
    assert runner.status()["capture"]["as_of"] == NOW.isoformat()


def test_background_runner_runs_pattern_lifecycle_after_capture() -> None:
    calls: list[tuple[str, object]] = []

    class RecordingRuntime:
        def mature_pending(self, now: datetime, **_kwargs) -> dict[str, object]:
            calls.append(("mature", now))
            return {"status": "ok"}

        def capture_and_predict(self, _episodes, *, now: datetime) -> dict[str, object]:
            calls.append(("capture", now))
            return {"status": "ok"}

    class Result:
        def to_dict(self) -> dict[str, object]:
            return {"status": "completed", "authority": "shadow_only"}

    class PatternWorkflow:
        def run(self, as_of: datetime) -> Result:
            calls.append(("patterns", as_of))
            return Result()

    runner = WorldModelBackgroundRunner(
        runtime=RecordingRuntime(),  # type: ignore[arg-type]
        pattern_workflow=PatternWorkflow(),
    )
    triggered = runner.trigger(episodes=[_episode("e-pattern")], now=NOW)
    triggered["_thread"].join(timeout=2)  # type: ignore[index,union-attr]

    assert calls == [("mature", NOW), ("capture", NOW), ("patterns", NOW)]
    assert runner.status()["pattern_lifecycle"] == {
        "status": "completed",
        "authority": "shadow_only",
    }


def test_background_runner_pattern_failure_is_fail_open_and_reported() -> None:
    class RecordingRuntime:
        def mature_pending(self, _now: datetime, **_kwargs) -> dict[str, object]:
            return {"status": "ok"}

        def capture_and_predict(self, _episodes, **_kwargs) -> dict[str, object]:
            return {"status": "ok"}

    class BrokenPatternWorkflow:
        def run(self, _as_of: datetime) -> object:
            raise OSError("pattern store unavailable")

    runner = WorldModelBackgroundRunner(
        runtime=RecordingRuntime(),  # type: ignore[arg-type]
        pattern_workflow=BrokenPatternWorkflow(),
    )
    triggered = runner.trigger(episodes=[_episode("e-pattern-fail")], now=NOW)
    triggered["_thread"].join(timeout=2)  # type: ignore[index,union-attr]

    status = runner.status()
    assert status["status"] == "partial"
    assert status["capture"] == {"status": "ok"}
    assert status["errors"][-1]["stage"] == "pattern_lifecycle"


def test_background_runner_propagates_partial_pattern_status_without_raising() -> None:
    class RecordingRuntime:
        def mature_pending(self, _now: datetime, **_kwargs) -> dict[str, object]:
            return {"status": "ok"}

        def capture_and_predict(self, _episodes, **_kwargs) -> dict[str, object]:
            return {"status": "ok"}

    class PartialPatternWorkflow:
        def run(self, _as_of: datetime) -> dict[str, object]:
            return {"status": "partial", "stages": [{"stage": "evaluation", "status": "failed"}]}

    runner = WorldModelBackgroundRunner(
        runtime=RecordingRuntime(),  # type: ignore[arg-type]
        pattern_workflow=PartialPatternWorkflow(),
    )
    triggered = runner.trigger(episodes=[_episode("e-pattern-partial")], now=NOW)
    triggered["_thread"].join(timeout=2)  # type: ignore[index,union-attr]

    status = runner.status()
    assert status["status"] == "partial"
    assert status["errors"] == []
    assert status["pattern_lifecycle"]["status"] == "partial"


def test_real_domain_store_labeler_and_restart_rehydrate_all_models(tmp_path) -> None:
    started = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    observation = WorldObservation(
        venue="US",
        symbol="MSFT",
        bar_interval="1h",
        as_of_bar_ts=started,
        feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
        sampling_policy_version="fresh-active-v1",
        anchor=AnchorBar(
            ts=started,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000.0,
            source="fixture",
        ),
        available_at=started,
        captured_at=started + timedelta(minutes=1),
        freshness="fresh",
        categorical_features={
            "asset_family": "equities",
            "venue": "US",
            "bar_interval": "1h",
            "session_phase": "regular",
            "market_regime": "trend_up",
            "volatility_state": "normal",
        },
        numeric_features={"return": 0.01, "atr_pct": 0.01},
    )
    episode = WorldEpisode(observation=observation)
    mature_at = started + timedelta(days=1, hours=1)

    class FixtureBars:
        def get_bars(self, _symbol: str, *, lookback: str, interval: str) -> list[dict[str, object]]:
            assert lookback == "5d"
            assert interval == "1h"
            return [
                {
                    "ts": started + timedelta(hours=4),
                    "open": 101.0,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.0,
                    "volume": 1_100.0,
                    "source": "fixture",
                    "interval": "1h",
                    "timestamp_semantics": "bar_close",
                    "available_at": started + timedelta(hours=4),
                },
                {
                    "ts": started + timedelta(days=1),
                    "open": 102.0,
                    "high": 103.0,
                    "low": 101.0,
                    "close": 102.0,
                    "volume": 1_200.0,
                    "source": "fixture",
                    "interval": "1h",
                    "timestamp_semantics": "bar_close",
                    "available_at": started + timedelta(days=1),
                },
            ]

    horizons = tuple(spec.horizon_id for spec in LABEL_HORIZONS)
    store = WorldModelStore(tmp_path / "world_model.db")
    baseline = HierarchicalDirichletWorldBaseline(allowed_horizons=horizons)
    gru = OnlineGRUWorldChallenger(allowed_horizons=horizons, hidden_size=4, sequence_len=4)
    runtime = WorldModelRuntime(
        store=store,
        predictor=baseline,
        predictors=(gru,),
        labeler=label_horizon,
        bar_provider=FixtureBars(),
        horizons=LABEL_HORIZONS,
    )

    captured = runtime.capture_and_predict([episode])
    matured = runtime.mature_pending(mature_at)

    assert captured["errors"] == []
    assert captured["predictions_appended"] == 4
    assert matured["errors"] == []
    assert matured["outcomes_appended"] == 2
    assert matured["baseline_updates"] == 2
    assert matured["model_updates"] == 4
    assert gru.support("elapsed_4h.v1") == 1
    assert gru.support("elapsed_1d.v1") == 1
    assert store.counts() == {"episodes": 1, "outcome_events": 2, "predictions": 4}

    restarted_baseline = HierarchicalDirichletWorldBaseline(allowed_horizons=horizons)
    restarted_gru = OnlineGRUWorldChallenger(
        allowed_horizons=horizons,
        hidden_size=4,
        sequence_len=4,
    )
    restarted = WorldModelRuntime(
        store=store,
        predictor=restarted_baseline,
        predictors=(restarted_gru,),
        labeler=label_horizon,
        bar_provider=FixtureBars(),
        horizons=LABEL_HORIZONS,
    )
    replay = restarted.capture_and_predict([episode])

    assert replay["errors"] == []
    assert replay["baseline_replayed"] == 2
    assert replay["model_replayed"] == 4
    assert replay["model_observations_replayed"] == 1
    assert replay["predictions_appended"] == 0
    assert restarted_gru.model_fingerprint("elapsed_4h.v1") == gru.model_fingerprint("elapsed_4h.v1")
    assert restarted_gru.model_fingerprint("elapsed_1d.v1") == gru.model_fingerprint("elapsed_1d.v1")
    assert store.counts() == {"episodes": 1, "outcome_events": 2, "predictions": 4}
    store.close()


def test_live_delayed_labels_reconcile_to_the_same_gru_as_restart(tmp_path) -> None:
    started = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    earlier = _domain_episode(started, market_return=0.01)
    later = _domain_episode(started + timedelta(hours=1), market_return=-0.01)
    available_at = {
        earlier.episode_id: started + timedelta(hours=10),
        later.episode_id: started + timedelta(hours=5),
    }
    move_class = {earlier.episode_id: "UP", later.episode_id: "DOWN"}

    class DelayedLabeler:
        def label_horizon(
            self,
            episode: dict[str, object],
            _bars: object,
            horizon_id: str,
            *,
            now: datetime,
        ) -> dict[str, object]:
            identifier = str(episode["episode_id"])
            label_at = available_at[identifier]
            assert label_at <= now
            return {
                "outcome_event_id": f"outcome:{identifier}:{horizon_id}",
                "episode_id": identifier,
                "horizon_id": horizon_id,
                "status": "observed",
                "move_class": move_class[identifier],
                "training_eligible": True,
                "sealed": True,
                "available_at": label_at.isoformat(),
                "source_raw_sha256": f"evidence:{identifier}",
            }

    class FixtureBars:
        def get_bars(self, _symbol: str, *, lookback: str, interval: str) -> list[dict[str, object]]:
            assert lookback == "5d"
            assert interval == "1h"
            return [{"ts": started, "close": 100.0}]

    horizon = "elapsed_4h.v1"
    store = WorldModelStore(tmp_path / "world_model.db")
    assert store.append_episode(earlier)
    assert store.append_episode(later)
    baseline = HierarchicalDirichletWorldBaseline(allowed_horizons=(horizon,))
    gru = OnlineGRUWorldChallenger(
        allowed_horizons=(horizon,),
        hidden_size=4,
        sequence_len=4,
    )
    runtime = WorldModelRuntime(
        store=store,
        predictor=baseline,
        predictors=(gru,),
        labeler=DelayedLabeler(),
        bar_provider=FixtureBars(),
        horizons=(horizon,),
    )

    live = runtime.mature_pending(started + timedelta(hours=12))

    assert live["errors"] == []
    assert live["outcomes_appended"] == 2
    assert gru.support(horizon) == 2
    restarted_gru = OnlineGRUWorldChallenger(
        allowed_horizons=(horizon,),
        hidden_size=4,
        sequence_len=4,
    )
    restarted = WorldModelRuntime(
        store=store,
        predictor=restarted_gru,
        labeler=DelayedLabeler(),
        bar_provider=FixtureBars(),
        horizons=(horizon,),
    )
    replay = restarted.capture_and_predict([], now=started + timedelta(hours=12))

    assert replay["errors"] == []
    assert restarted_gru.support(horizon) == 2
    assert restarted_gru.model_fingerprint(horizon) == gru.model_fingerprint(horizon)
    store.close()


def test_live_superseding_outcome_rebuilds_active_gru_leaf_without_restart(tmp_path) -> None:
    started = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    horizon = "elapsed_4h.v1"
    episode = _domain_episode(started)
    original = WorldOutcome(
        episode_id=episode.episode_id,
        horizon={"horizon_id": horizon, "duration_seconds": 4 * 60 * 60},
        status="observed",
        target_at=started + timedelta(hours=4),
        available_at=started + timedelta(hours=4),
        computed_at=started + timedelta(hours=4),
        anchor_close=100.0,
        endpoint_close=102.0,
        endpoint_bar_ts=started + timedelta(hours=4),
        source="fixture",
        source_raw_sha256="original-up",
        training_eligible=True,
    )
    correction = WorldOutcome(
        episode_id=episode.episode_id,
        horizon={"horizon_id": horizon, "duration_seconds": 4 * 60 * 60},
        status="observed",
        target_at=started + timedelta(hours=4),
        available_at=started + timedelta(hours=5),
        computed_at=started + timedelta(hours=5),
        anchor_close=100.0,
        endpoint_close=98.0,
        endpoint_bar_ts=started + timedelta(hours=4),
        source="fixture-correction",
        source_raw_sha256="correction-down",
        supersedes_event_id=original.event_id,
        training_eligible=True,
    )
    store = WorldModelStore(tmp_path / "world_model.db")
    assert store.append_episode(episode)
    assert store.append_outcome_event({**original.to_dict(), "move_class": "UP"})
    gru = OnlineGRUWorldChallenger(
        allowed_horizons=(horizon,),
        hidden_size=4,
        sequence_len=4,
    )
    runtime = WorldModelRuntime(
        store=store,
        predictor=gru,
        labeler=None,
        bar_provider=None,
        horizons=(horizon,),
    )
    first = runtime.capture_and_predict([], now=started + timedelta(hours=6))
    original_fingerprint = gru.model_fingerprint(horizon)

    assert first["errors"] == []
    assert gru.support(horizon) == 1
    assert store.append_outcome_event({**correction.to_dict(), "move_class": "DOWN"})
    reconciled = runtime.capture_and_predict([], now=started + timedelta(hours=7))

    assert reconciled["errors"] == []
    assert gru.support(horizon) == 1
    assert gru.model_fingerprint(horizon) != original_fingerprint
    restarted_gru = OnlineGRUWorldChallenger(
        allowed_horizons=(horizon,),
        hidden_size=4,
        sequence_len=4,
    )
    restarted = WorldModelRuntime(
        store=store,
        predictor=restarted_gru,
        labeler=None,
        bar_provider=None,
        horizons=(horizon,),
    )
    replay = restarted.capture_and_predict([], now=started + timedelta(hours=7))

    assert replay["errors"] == []
    assert restarted_gru.model_fingerprint(horizon) == gru.model_fingerprint(horizon)
    store.close()


def test_legacy_duck_typed_predictor_remains_compatible_without_replay_reset(tmp_path) -> None:
    started = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    horizon = "elapsed_4h.v1"
    episode = _domain_episode(started)

    class LegacyPredictor:
        model_id = "legacy-world-predictor"
        model_version = "v1"

        def __init__(self) -> None:
            self.applied = 0
            self.observed_episode_ids: list[str] = []

        def observe_episode(self, observation: dict[str, object]) -> None:
            self.observed_episode_ids.append(str(observation["episode_id"]))

        def predict(
            self,
            observation: dict[str, object],
            horizon_id: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            return {
                "episode_id": observation["episode_id"],
                "horizon_id": horizon_id,
                "probabilities": {"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
            }

        def apply_outcome(self, *_args: object, **_kwargs: object) -> None:
            self.applied += 1

    class Labeler:
        def label_horizon(
            self,
            observation: dict[str, object],
            _bars: object,
            horizon_id: str,
            *,
            now: datetime,
        ) -> dict[str, object]:
            return {
                "outcome_event_id": f"outcome:{observation['episode_id']}:{horizon_id}",
                "episode_id": observation["episode_id"],
                "horizon_id": horizon_id,
                "status": "observed",
                "move_class": "UP",
                "training_eligible": True,
                "sealed": True,
                "available_at": now.isoformat(),
                "source_raw_sha256": f"evidence:{observation['episode_id']}",
            }

    class Bars:
        def get_bars(self, _symbol: str, **_kwargs: object) -> list[dict[str, object]]:
            return [{"ts": started, "close": 100.0}]

    store = WorldModelStore(tmp_path / "world_model.db")
    predictor = LegacyPredictor()
    runtime = WorldModelRuntime(
        store=store,
        predictor=predictor,
        labeler=Labeler(),
        bar_provider=Bars(),
        horizons=(horizon,),
    )
    captured = runtime.capture_and_predict([episode], now=started + timedelta(hours=1))
    matured = runtime.mature_pending(started + timedelta(hours=4))

    assert captured["errors"] == []
    assert predictor.observed_episode_ids == [episode.episode_id]
    assert matured["errors"] == []
    assert matured["status"] == "ok"
    assert matured["model_reconcile_skipped_unresettable"] == 1
    assert predictor.applied == 1
    store.close()


def test_late_causally_earlier_episode_reconciles_training_sequence_before_prediction(tmp_path) -> None:
    started = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    horizon = "elapsed_4h.v1"
    earlier = _domain_episode(started, market_return=-0.01)
    trained_target = _domain_episode(started + timedelta(hours=1), market_return=0.01)
    outcome = WorldOutcome(
        episode_id=trained_target.episode_id,
        horizon={"horizon_id": horizon, "duration_seconds": 4 * 60 * 60},
        status="observed",
        target_at=started + timedelta(hours=5),
        available_at=started + timedelta(hours=5),
        computed_at=started + timedelta(hours=5),
        anchor_close=100.0,
        endpoint_close=102.0,
        endpoint_bar_ts=started + timedelta(hours=5),
        source="fixture",
        source_raw_sha256="trained-target-up",
        training_eligible=True,
    )
    store = WorldModelStore(tmp_path / "world_model.db")
    gru = OnlineGRUWorldChallenger(
        allowed_horizons=(horizon,),
        hidden_size=4,
        sequence_len=4,
    )
    runtime = WorldModelRuntime(
        store=store,
        predictor=gru,
        labeler=None,
        bar_provider=None,
        horizons=(horizon,),
    )
    assert (
        runtime.capture_and_predict(
            [trained_target],
            now=started + timedelta(hours=2),
        )["errors"]
        == []
    )
    assert store.append_outcome_event({**outcome.to_dict(), "move_class": "UP"})
    assert runtime.capture_and_predict([], now=started + timedelta(hours=6))["errors"] == []
    before = gru.model_fingerprint(horizon)

    backfill = runtime.capture_and_predict([earlier], now=started + timedelta(hours=7))

    assert backfill["errors"] == []
    assert gru.support(horizon) == 1
    assert gru.model_fingerprint(horizon) != before
    assert gru.sequence_metadata(trained_target).episode_ids == (
        earlier.episode_id,
        trained_target.episode_id,
    )
    restarted_gru = OnlineGRUWorldChallenger(
        allowed_horizons=(horizon,),
        hidden_size=4,
        sequence_len=4,
    )
    restarted = WorldModelRuntime(
        store=store,
        predictor=restarted_gru,
        labeler=None,
        bar_provider=None,
        horizons=(horizon,),
    )
    replay = restarted.capture_and_predict([], now=started + timedelta(hours=7))

    assert replay["errors"] == []
    assert restarted_gru.model_fingerprint(horizon) == gru.model_fingerprint(horizon)
    store.close()


def test_runtime_wires_cohort_service_from_store_without_starting_a_cohort(tmp_path) -> None:
    from inspect import signature

    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.application.world_model.service import WorldModelService

    store = WorldModelStore(tmp_path / "world_model.db")
    try:
        runtime = WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
        )
        assert "cohort_service" in signature(WorldModelRuntime.__init__).parameters
        assert isinstance(runtime.cohort_service, WorldCohortService)
        assert runtime.cohort_service.repository is store
        assert runtime.cohort_service.query is store
        assert store.list_collecting_cohort_ids() == ()
        assert all(getattr(predictor, "lane_identity", None) is None for predictor in runtime.predictors)
        episode = _domain_episode(NOW)
        report = runtime.capture_and_predict((episode,), now=NOW)
        assert report["errors"] == []
        rows = store.list_predictions()
        assert rows
        assert all(not row.get("study_cohort_id") for row in rows)
        assert all(row.get("prediction_record", {}).get("decision_effect", "none") == "none" for row in rows)
        assert WorldModelService is not WorldModelRuntime
    finally:
        store.close()


def test_runtime_restart_keeps_cohort_fingerprints_and_does_not_backfill_downtime(tmp_path) -> None:
    from trader.application.world_model.cohort_service import WorldCohortService
    from tests.application.test_world_cohort_service import ANCHOR_TS, LATER_TS, START_READY
    from tests.application.test_world_context_lanes import (
        _cohort_market_episode,
        _collecting_cohort_service,
    )

    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=lambda: START_READY)
    try:
        cohort_service, cohort, _ready = _collecting_cohort_service(store)
        first = _cohort_market_episode(ANCHOR_TS)
        runtime = WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            predictors=(OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4),),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=cohort_service,
        )
        first_report = runtime.capture_and_predict((first,), now=ANCHOR_TS)
        assert first_report["errors"] == []
        first_rows = {
            (row["lane_id"], row["horizon_code"], row["prediction_sha256"], row["feature_contract_fingerprint"])
            for row in store.list_predictions()
            if row.get("lane_id") == "markov.market"
        }
        assert first_rows
        fingerprints = {
            predictor.model_fingerprint("elapsed_4h.v1")
            for predictor in runtime.predictors
            if getattr(getattr(predictor, "lane_identity", None), "lane_id", None) == "markov.market"
        }
        store.close()

        restarted_store = WorldModelStore(path, clock=lambda: ANCHOR_TS + timedelta(minutes=5))
        restarted_service = WorldCohortService(repository=restarted_store, query=restarted_store)
        restarted = WorldModelRuntime(
            store=restarted_store,
            predictor=HierarchicalDirichletWorldBaseline(),
            predictors=(OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4),),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=restarted_service,
        )
        replay = restarted.capture_and_predict((first,), now=LATER_TS)
        assert replay["errors"] == []
        replay_rows = {
            (row["lane_id"], row["horizon_code"], row["prediction_sha256"], row["feature_contract_fingerprint"])
            for row in restarted_store.list_predictions()
            if row.get("lane_id") == "markov.market"
        }
        assert replay_rows == first_rows
        restarted_fingerprints = {
            predictor.model_fingerprint("elapsed_4h.v1")
            for predictor in restarted.predictors
            if getattr(getattr(predictor, "lane_identity", None), "lane_id", None) == "markov.market"
        }
        assert restarted_fingerprints == fingerprints
        later = _cohort_market_episode(LATER_TS, market_return=-0.02)
        later_report = restarted.capture_and_predict((later,), now=LATER_TS)
        assert later_report["errors"] == []
        slots = restarted_service.list_slots(cohort.cohort_id)
        assert {slot.as_of_bar_ts for slot in slots} == {ANCHOR_TS, LATER_TS}
        assert ANCHOR_TS + timedelta(hours=1) not in {slot.as_of_bar_ts for slot in slots}
        started_envelope = restarted_store.envelope_for(cohort.started_event)
        assert started_envelope.require_proven().receipt.ready_at == START_READY
        restarted_store.close()
    finally:
        store.close()


def test_graph_status_overlay_is_wiring_only_and_does_not_claim_ledger_activation() -> None:
    from pathlib import Path

    from trader.interfaces.cli.world_model import read_world_graph_status as cli_status
    from trader.reporting.read_models.world_graph import read_world_graph_status
    from trader.runtime import world_model_runtime
    from trader.runtime.world_model_runtime import graph_status_overlay

    assert cli_status is read_world_graph_status
    overlay = graph_status_overlay(wired=True)
    assert overlay["wired"] is True
    assert overlay["gaps"]["writes"] == "none_until_due_cycle"
    assert overlay["gaps"]["cohort_activation"] == "not_read_from_ledger"
    assert overlay["authority"] == "shadow_only"
    assert overlay["decision_effect"] == "none"
    assert overlay["causal_claim"] is False
    assert overlay["pnl_claim"] is False
    assert overlay["recommendation"] == "NO_GO"
    assert "schema_version" not in overlay
    source = Path(world_model_runtime.__file__).read_text(encoding="utf-8")
    assert source.count("graph_status_overlay(") == 2
    assert 'payload["graph"] = graph_status_overlay(wired=True)' in source
    assert "read_world_graph_status" not in source
    assert 'cohort_activation": "not_started"' not in source


def test_wired_runner_overlay_does_not_mirror_persisted_graph_collecting(tmp_path) -> None:
    from tests.application.test_world_graph_capture import _unpublished_config
    from tests.read_models.test_world_graph_report import _graph_manifest, _persist
    from trader.reporting.read_models.world_graph import read_world_graph_status
    from trader.runtime.world_model_runtime import compose_local_graph_lanes

    _persist(tmp_path, _graph_manifest(), phase="collecting")
    store = WorldModelStore(tmp_path / "world_model.db")
    try:
        predictors, enricher = compose_local_graph_lanes(
            enabled=True,
            capture=_unpublished_config(),
        )
        runner = WorldModelBackgroundRunner(
            runtime=WorldModelRuntime(
                store=store,
                predictor=HierarchicalDirichletWorldBaseline(),
                predictors=predictors,
                labeler=None,
                bar_provider=None,
                horizons=("elapsed_4h.v1",),
            ),
            graph_enricher=enricher,
        )
        overlay = runner.status()["graph"]
        ledger = read_world_graph_status(tmp_path)
        assert overlay["wired"] is True
        assert overlay["gaps"]["writes"] == "none_until_due_cycle"
        assert overlay["gaps"]["cohort_activation"] == "not_read_from_ledger"
        assert overlay["authority"] == "shadow_only"
        assert overlay["decision_effect"] == "none"
        assert ledger["gaps"]["cohort_activation"] == "collecting"
        assert ledger["schema_version"] == "world_graph_status.v1"
    finally:
        store.close()


def test_graph_flag_reuses_macro_runtime_name_and_defaults_off() -> None:
    from pathlib import Path

    from trader.runtime.world_macro_runtime import GRAPH_FLAG, graph_enabled
    from trader.runtime import world_model_runtime

    assert GRAPH_FLAG == "CASYS_WORLD_MODEL_GRAPH_ENABLED"
    assert graph_enabled({}) is False
    assert graph_enabled({"CASYS_WORLD_MODEL_GRAPH_ENABLED": "0"}) is False
    assert graph_enabled({"CASYS_WORLD_MODEL_GRAPH_ENABLED": "1"}) is True
    source = Path(world_model_runtime.__file__).read_text(encoding="utf-8")
    assert "CASYS_WORLD_MODEL_GRAPH_ENABLED" in source
    assert "def graph_enabled" not in source
    assert "graph_enabled" in source


def test_compose_local_graph_lanes_requires_flag_and_capture_and_predictors() -> None:
    from tests.application.test_world_graph_capture import _unpublished_config
    from trader.runtime.world_model_runtime import (
        WorldGraphEpisodeEnricher,
        compose_local_graph_lanes,
    )

    capture = _unpublished_config()
    dummy = object()
    assert compose_local_graph_lanes(enabled=False, capture=capture, predictors=(dummy,)) == ((), None)
    assert compose_local_graph_lanes(enabled=True, capture=None, predictors=(dummy,)) == ((), None)
    assert compose_local_graph_lanes(enabled=True, capture=capture, predictors=()) == ((), None)
    predictors, enricher = compose_local_graph_lanes(
        enabled=True,
        capture=capture,
        predictors=(dummy,),
    )
    assert predictors == (dummy,)
    assert isinstance(enricher, WorldGraphEpisodeEnricher)
    assert enricher.config is capture


def test_graph_enricher_appends_graph_without_mutating_market() -> None:
    from tests.application.test_world_context_capture import V1_EPISODE_ID, _market_episode
    from tests.application.test_world_graph_capture import _unpublished_config
    from trader.domain.world_feature_contract import GRAPH_FEATURE_CONTRACT_ID
    from trader.runtime.world_model_runtime import WorldGraphEpisodeEnricher

    v1 = _market_episode()
    original_id = v1.episode_id
    enricher = WorldGraphEpisodeEnricher(_unpublished_config())
    out = enricher.enrich((v1,))
    assert v1.episode_id == original_id == V1_EPISODE_ID
    assert v1.observation.feature_contract_version == "world_feature.market.v1"
    assert len(out) == 2
    assert out[0].episode_id == V1_EPISODE_ID
    assert out[1].observation.feature_contract_version == GRAPH_FEATURE_CONTRACT_ID
    assert out[1].observation.symbol == v1.observation.symbol


def test_background_graph_enrich_failure_keeps_market_capture() -> None:
    class BrokenGraphEnricher:
        def enrich(self, _episodes):
            raise OSError("graph snapshot unavailable")

    class RecordingRuntime:
        def __init__(self) -> None:
            self.captured: list[object] = []

        def mature_pending(self, _now: datetime, **_kwargs) -> dict[str, object]:
            return {"status": "ok"}

        def capture_and_predict(self, episodes, **_kwargs) -> dict[str, object]:
            self.captured.append(tuple(episodes))
            return {"status": "ok"}

    runtime = RecordingRuntime()
    runner = WorldModelBackgroundRunner(
        runtime=runtime,  # type: ignore[arg-type]
        graph_enricher=BrokenGraphEnricher(),
    )
    original = _episode("e-graph-fail")
    triggered = runner.trigger(episodes=[original], now=NOW)
    triggered["_thread"].join(timeout=2)  # type: ignore[index,union-attr]

    assert runtime.captured == [(_episode("e-graph-fail"),)]
    status = runner.status()
    assert status["status"] == "partial"
    assert status["errors"][0]["stage"] == "graph_enrich"
    assert status["graph"]["authority"] == "shadow_only"
    assert status["graph"]["decision_effect"] == "none"
    assert status["graph"]["causal_claim"] is False
    assert status["graph"]["pnl_claim"] is False


def test_compose_and_runner_wire_graph_without_writing_until_due_cycle(tmp_path) -> None:
    from tests.application.test_world_graph_capture import _unpublished_config
    from trader.domain.world_feature_contract import GRAPH_FEATURE_CONTRACT_ID
    from trader.runtime.world_model_runtime import compose_local_graph_lanes

    store = WorldModelStore(tmp_path / "world_model.db")
    try:
        predictors, enricher = compose_local_graph_lanes(
            enabled=True,
            capture=_unpublished_config(),
        )
        assert enricher is not None
        assert predictors
        assert store.counts()["episodes"] == 0
        runner = WorldModelBackgroundRunner(
            runtime=WorldModelRuntime(
                store=store,
                predictor=HierarchicalDirichletWorldBaseline(),
                predictors=predictors,
                labeler=None,
                bar_provider=None,
                horizons=("elapsed_4h.v1",),
            ),
            graph_enricher=enricher,
        )
        wired = runner.status()
        assert wired["running"] is False
        assert wired["graph"]["wired"] is True
        assert wired["graph"]["gaps"]["writes"] == "none_until_due_cycle"
        assert wired["graph"]["budgets"]["max_depth"] == 4
        assert wired["graph"]["budgets"]["max_paths_per_root"] == 32
        assert store.counts()["episodes"] == 0

        from tests.application.test_world_context_capture import _market_episode

        triggered = runner.trigger(episodes=[_market_episode()], now=NOW)
        triggered["_thread"].join(timeout=2)  # type: ignore[index,union-attr]
        versions = {row["observation"]["feature_contract_version"] for row in store.list_eligible_episodes()}
        assert "world_feature.market.v1" in versions
        assert GRAPH_FEATURE_CONTRACT_ID in versions
        assert store.list_collecting_cohort_ids() == ()
        capture = runner.status()["capture"]
        assert all(
            (row.get("prediction_record") or {}).get("decision_effect", "none") == "none"
            for row in store.list_predictions()
        )
        assert "causal" not in str(capture).lower() or capture.get("status") in {"ok", "partial"}
    finally:
        store.close()


def test_compose_graph_accepts_frozen_study_cohort_id_without_a_second_scope_mapping() -> None:
    from tests.application.test_world_graph_capture import _unpublished_config
    from trader.application.world_model.graph_capture import WorldGraphCaptureConfig
    from trader.runtime.world_model_runtime import compose_local_graph_lanes

    cohort_id = "world_cohort:v1:" + "c" * 64
    capture = _unpublished_config(study_cohort_id=cohort_id)
    predictors, enricher = compose_local_graph_lanes(
        enabled=True,
        capture=capture,
        study_cohort_id="world_cohort:v1:" + "d" * 64,
    )
    assert enricher is not None
    assert isinstance(enricher.config, WorldGraphCaptureConfig)
    assert enricher.config.study_cohort_id == cohort_id
    assert enricher.config.scope_mapping is capture.scope_mapping


def test_compose_local_graph_injects_study_cohort_id_when_capture_is_composed(tmp_path) -> None:
    from pathlib import Path

    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import compose_local_graph_lanes

    store = WorldModelStore(tmp_path / "world_model.db")
    cohort_id = "world_cohort:v1:" + "e" * 64
    try:
        predictors, enricher = compose_local_graph_lanes(
            enabled=True,
            store=store,
            config_dir=Path(__file__).resolve().parents[2] / "config",
            study_cohort_id=cohort_id,
        )
        assert enricher is not None
        assert enricher.config.study_cohort_id == cohort_id
        assert store.counts()["episodes"] == 0
    finally:
        store.close()


def _persist_historical_unmapped_v3(store: WorldModelStore, payload: dict[str, object]) -> None:
    from trader.domain.world_episode import canonical_json, canonical_sha256

    observation = payload["observation"]
    assert isinstance(observation, dict)
    snapshot = observation["graph_features"]["snapshot"]
    assert isinstance(snapshot, dict)
    root = snapshot.get("root_entity") or {}
    recorded_at = str(observation.get("captured_at") or observation.get("available_at"))
    payload_json = canonical_json(payload)
    snapshot_json = canonical_json(snapshot)
    source = {
        key: observation[key]
        for key in ("anchor", "freshness", "available_at", "captured_at", "graph_features")
        if key in observation
    }
    source_json = canonical_json(source)
    with store._db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO world_graph_snapshots(
                snapshot_id, root_episode_id, root_entity_kind, root_entity_id, cutoff_at,
                ontology_revision, ontology_hash, identity_map_hash, scope_mapping_id,
                scope_mapping_hash, status, payload_json, payload_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot["snapshot_id"],
                snapshot["root_episode_id"],
                root.get("kind") or "",
                root.get("entity_id") or "",
                snapshot["cutoff_at"],
                snapshot["ontology_revision"],
                snapshot["ontology_hash"],
                snapshot["identity_map_hash"],
                snapshot["scope_mapping_id"],
                snapshot["scope_mapping_hash"],
                snapshot["status"],
                snapshot_json,
                canonical_sha256(snapshot),
                recorded_at,
            ),
        )
        cur.execute(
            """
            INSERT INTO world_episodes(
                episode_id, capture_id, venue, symbol, observed_at, available_at, as_of_bar_ts,
                bar_interval, feature_contract_version, sampling_policy_version, training_eligible,
                training_reason, payload_json, payload_sha256, source_evidence_json,
                source_evidence_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["episode_id"],
                None,
                observation["venue"],
                observation["symbol"],
                observation["as_of_bar_ts"],
                observation["available_at"],
                observation["as_of_bar_ts"],
                observation["bar_interval"],
                observation["feature_contract_version"],
                observation["sampling_policy_version"],
                1,
                None,
                payload_json,
                canonical_sha256(payload),
                source_json,
                canonical_sha256(source),
                recorded_at,
            ),
        )


def test_gru_replay_observes_historical_unmapped_graph_without_selecting_a_root(tmp_path) -> None:
    from tests.application.test_world_graph_capture import _attach, _us_aaa_episode, _us_gm_published_config
    from trader.application.world_model.encoding import world_lane_encoder_profile
    from trader.application.world_model.gru import OnlineGRUWorldChallenger
    from trader.domain.world_feature_contract import (
        GRAPH_FEATURE_CONTRACT_ID,
        GRAPH_GRU_MODEL_IDENTITY,
        GRAPH_MARKOV_MODEL_IDENTITY,
        GRAPH_MODEL_VERSION,
    )
    from trader.domain.world_graph import WorldGraphSnapshot

    v3 = _attach((_us_aaa_episode(),), _us_gm_published_config())[0]
    payload = v3.to_dict()
    snapshot = payload["observation"]["graph_features"]["snapshot"]
    snapshot["root_entity"] = {
        "kind": "instrument",
        "entity_id": "mic:XNYS:symbol:AAA",
        "node_kind": "world_entity",
    }
    with pytest.raises(ValueError, match="unmapped|root"):
        WorldGraphSnapshot.from_mapping(snapshot)

    store = WorldModelStore(tmp_path / "world_model.db")
    runner = None
    try:
        _persist_historical_unmapped_v3(store, payload)
        status_profile = world_lane_encoder_profile("topology_status_only")
        content_profile = world_lane_encoder_profile("graph_content")
        markov = HierarchicalDirichletWorldBaseline(
            model_id=GRAPH_MARKOV_MODEL_IDENTITY,
            model_version=GRAPH_MODEL_VERSION,
            feature_contract=status_profile.contract,
            feature_mask=status_profile.mask,
            accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_ID}),
        )
        gru = OnlineGRUWorldChallenger(
            model_id=GRAPH_GRU_MODEL_IDENTITY,
            model_version=GRAPH_MODEL_VERSION,
            feature_contract=content_profile.contract,
            feature_mask=content_profile.mask,
            accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_ID}),
        )
        runtime = WorldModelRuntime(
            store=store,
            predictor=markov,
            predictors=(gru,),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
        )
        runner = WorldModelBackgroundRunner(runtime=runtime)
        result = runner.trigger(episodes=(), now=NOW, reason="hydrate")
        assert result["triggered"] is True
        result["_thread"].join(timeout=5)
        assert result["_thread"].is_alive() is False
        status = runner.status()
        errors = status.get("errors") or []
        assert not any(
            "unmapped/ambiguous snapshot must not select a world entity root" in str(item) for item in errors
        )
        observed = gru.observe_episode(store.list_eligible_episodes()[0]["episode"])
        assert observed == payload["episode_id"]
        again = gru.observe_episode(store.list_eligible_episodes()[0]["episode"])
        assert again == observed
        canonical = WorldEpisode.from_dict(store.list_eligible_episodes()[0]["episode"])
        assert canonical.observation.graph is not None
        assert canonical.observation.graph.root_entity is None
        dumped = json.dumps(canonical.observation.graph.to_dict())
        assert "XNYS" not in dumped
        assert "mic:" not in dumped
        features = canonical.observation.graph_features["categorical_features"]
        assert features["graph_scope_status"] == "unmapped"
        assert features["graph_missingness_status"] == "unmapped"
        forecast = markov.predict(canonical, horizon_id="elapsed_4h.v1", prediction_at=NOW)
        assert forecast.status in {"warming_up", "shadow_only"}
        assert forecast.model_id == GRAPH_MARKOV_MODEL_IDENTITY
        gru_forecast = gru.predict(canonical, horizon_id="elapsed_4h.v1", prediction_at=NOW)
        assert gru_forecast.status in {"warming_up", "shadow_only"}
        assert gru_forecast.model_id == GRAPH_GRU_MODEL_IDENTITY
    finally:
        if runner is not None:
            runner.stop()
        store.close()
