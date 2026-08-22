from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
from trader.application.world_model.labeler import DEFAULT_HORIZONS as LABEL_HORIZONS
from trader.application.world_model.labeler import label_horizon
from trader.domain.world_episode import AnchorBar, WorldEpisode, WorldObservation
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.runtime.world_model_runtime import WorldModelBackgroundRunner, WorldModelRuntime


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


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

    def list_predictions(self) -> list[dict[str, object]]:
        return list(self.predictions.values())

    def list_pending_episodes(self, *, horizon_code: str | None = None) -> list[dict[str, object]]:
        assert horizon_code is not None
        terminal = {"observed", "missing", "unknown"}
        return [
            copy.deepcopy(episode)
            for episode_id, episode in self.episodes.items()
            if self.outcomes.get((episode_id, horizon_code), {}).get("status") not in terminal
        ]

    def list_outcome_events(self, **_kwargs) -> list[dict[str, object]]:
        return list(self.outcomes.values())


class RecordingPredictor:
    def __init__(self) -> None:
        self.predictions: list[tuple[str, str]] = []
        self.baseline_updates: list[tuple[str, str]] = []

    def predict(self, episode: dict[str, object], horizon_id: str = "elapsed_4h.v1") -> dict[str, object]:
        episode["mutated_by_predictor"] = True
        identifier = str(episode["episode_id"])
        self.predictions.append((identifier, horizon_id))
        return {
            "prediction_id": f"pred:{identifier}:{horizon_id}",
            "episode_id": identifier,
            "horizon_id": horizon_id,
            "distribution": {"up": 0.5, "flat": 0.3, "down": 0.2},
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
    def __init__(self, *, fail_horizons: set[tuple[str, str]] | None = None, pending_horizons: set[str] | None = None) -> None:
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
    return WorldModelRuntime(
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
    assert second["predictions_appended"] == 0
    assert predictor.predictions == [
        ("e-capture", "elapsed_4h.v1"),
        ("e-capture", "elapsed_1d.v1"),
    ]
    assert store.events == [
        ("episode", "e-capture"),
        ("prediction", ("e-capture", "elapsed_4h.v1")),
        ("prediction", ("e-capture", "elapsed_1d.v1")),
        ("episode", "e-capture"),
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


def test_real_domain_store_labeler_and_restart_rehydrate_baseline(tmp_path) -> None:
    started = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    observation = WorldObservation(
        venue="US",
        symbol="MSFT",
        bar_interval="1h",
        as_of_bar_ts=started,
        feature_contract_version="world-features-v1",
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
    runtime = WorldModelRuntime(
        store=store,
        predictor=baseline,
        labeler=label_horizon,
        bar_provider=FixtureBars(),
        horizons=LABEL_HORIZONS,
    )

    captured = runtime.capture_and_predict([episode])
    matured = runtime.mature_pending(mature_at)

    assert captured["errors"] == []
    assert captured["predictions_appended"] == 2
    assert matured["errors"] == []
    assert matured["outcomes_appended"] == 2
    assert matured["baseline_updates"] == 2
    assert store.counts() == {"episodes": 1, "outcome_events": 2, "predictions": 2}

    restarted_baseline = HierarchicalDirichletWorldBaseline(allowed_horizons=horizons)
    restarted = WorldModelRuntime(
        store=store,
        predictor=restarted_baseline,
        labeler=label_horizon,
        bar_provider=FixtureBars(),
        horizons=LABEL_HORIZONS,
    )
    replay = restarted.capture_and_predict([episode])

    assert replay["errors"] == []
    assert replay["baseline_replayed"] == 2
    assert replay["predictions_appended"] == 0
    assert store.counts() == {"episodes": 1, "outcome_events": 2, "predictions": 2}
    store.close()
