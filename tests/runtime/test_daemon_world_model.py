from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trader.domain.market_data import Bar
from trader.runtime import daemon


NOW = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)


def _bars(*, base: float) -> list[Bar]:
    return [
        Bar(
            ts="2026-08-22T10:00:00+00:00",
            open=base,
            high=base + 2.0,
            low=base - 1.0,
            close=base + 1.0,
            volume=1_000.0,
        ),
        Bar(
            ts="2026-08-22T10:15:00+00:00",
            open=base + 1.0,
            high=base + 3.0,
            low=base,
            close=base + 2.0,
            volume=1_200.0,
        ),
    ]


def _quarter_hour_bars(*, count: int, base: float) -> list[Bar]:
    start = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    return [
        Bar(
            ts=(start + timedelta(minutes=15 * index)).isoformat(),
            open=base + index,
            high=base + index + 1.5,
            low=base + index - 0.5,
            close=base + index + 1.0,
            volume=1_000.0 + index,
        )
        for index in range(count)
    ]


class _Runner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def trigger(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"triggered": True, "reason": "accepted"}


def test_world_snapshot_cutoff_is_never_before_fetch_completion() -> None:
    started = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    completed = started + timedelta(minutes=3)

    assert daemon._world_model_snapshot_cutoff(
        started,
        completed_at=completed,
    ) == completed
    assert daemon._world_model_snapshot_cutoff(
        completed,
        completed_at=started,
    ) == completed


def test_world_shadow_freezes_every_active_tradable_symbol_before_dispatch() -> None:
    runner = _Runner()

    result = daemon._trigger_world_model_shadow(
        runner=runner,
        active_symbols=["AAA", "BBB", "INACTIVE"],
        tradable_symbols=["BBB", "AAA", "NOT_ACTIVE"],
        bars_by_symbol={"AAA": _bars(base=100.0), "BBB": _bars(base=50.0)},
        data_age_by_symbol={"AAA": 3.0, "BBB": 4.0},
        runtime_data_source_by_symbol={"AAA": "yfinance", "BBB": "ib"},
        data_source=object(),
        runtime_interval="15m",
        now=NOW,
    )

    assert result == {"triggered": True, "reason": "accepted"}
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["reason"] == "market_snapshot_pre_dispatch"
    episodes = call["episodes"]
    assert [episode.observation.symbol for episode in episodes] == ["AAA", "BBB"]
    assert all(episode.training_eligible for episode in episodes)
    assert all(episode.observation.captured_at == NOW for episode in episodes)
    assert all(not _contains_control_field(episode.to_dict()) for episode in episodes)

    bars = call["bars_by_symbol"]
    assert bars["AAA"][0]["source"] == "yfinance"
    assert bars["BBB"][0]["source"] == "ib"
    assert bars["AAA"][0]["interval"] == "15m"
    assert bars["AAA"][0]["timestamp_semantics"] == "bar_start"
    assert bars["AAA"][0]["available_at"] == NOW.isoformat()


def test_world_shadow_wiring_persists_predictions_in_the_dedicated_store(tmp_path) -> None:
    from trader.application.world_model import labeler
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import (
        WorldModelBackgroundRunner,
        WorldModelRuntime,
    )

    store = WorldModelStore(tmp_path / "world_model.db")
    runner = WorldModelBackgroundRunner(
        runtime=WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=labeler,
            bar_provider=None,
        )
    )
    try:
        result = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA", "BBB"],
            tradable_symbols=["AAA", "BBB"],
            bars_by_symbol={"AAA": _bars(base=100.0), "BBB": _bars(base=50.0)},
            data_age_by_symbol={"AAA": 3.0, "BBB": 4.0},
            runtime_data_source_by_symbol={"AAA": "yfinance", "BBB": "ib"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW,
        )

        assert result["triggered"] is True
        result["_thread"].join(timeout=2)
        assert runner.status()["running"] is False
        assert store.counts() == {
            "episodes": 2,
            "outcome_events": 0,
            "predictions": 4,
        }
        assert {
            row["horizon_code"] for row in store.list_predictions()
        } == {"elapsed_4h.v1", "elapsed_1d.v1"}

        # A second wake before the next completed 15m bar has the same slot id
        # but a later fetch/capture clock.  The first T0 evidence remains
        # canonical instead of becoming an append-only conflict.
        replay = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA", "BBB"],
            tradable_symbols=["AAA", "BBB"],
            bars_by_symbol={"AAA": _bars(base=100.0), "BBB": _bars(base=50.0)},
            data_age_by_symbol={"AAA": 8.0, "BBB": 9.0},
            runtime_data_source_by_symbol={"AAA": "yfinance", "BBB": "ib"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW + timedelta(minutes=5),
        )
        assert replay["triggered"] is True
        replay["_thread"].join(timeout=2)
        assert runner.status()["capture"]["errors"] == []
        assert store.counts() == {
            "episodes": 2,
            "outcome_events": 0,
            "predictions": 4,
        }

        revised = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA", "BBB"],
            tradable_symbols=["AAA", "BBB"],
            bars_by_symbol={"AAA": _bars(base=101.0), "BBB": _bars(base=50.0)},
            data_age_by_symbol={"AAA": 9.0, "BBB": 10.0},
            runtime_data_source_by_symbol={"AAA": "yfinance", "BBB": "ib"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW + timedelta(minutes=6),
        )
        assert revised["triggered"] is True
        revised["_thread"].join(timeout=2)
        capture = runner.status()["capture"]
        assert capture["status"] == "partial"
        assert capture["errors"][0]["stage"] == "get_episode"
        assert "existing_episode_market_evidence_conflict" in capture["errors"][0]["error"]
        assert store.counts() == {
            "episodes": 2,
            "outcome_events": 0,
            "predictions": 4,
        }
    finally:
        runner.stop()
        store.close()


def test_world_shadow_failure_is_logged_and_never_escapes(monkeypatch) -> None:
    class BrokenRunner:
        def trigger(self, **_kwargs: object) -> None:
            raise OSError("shadow disk unavailable")

    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(daemon.log, "warning", lambda *args: warnings.append(args))

    result = daemon._trigger_world_model_shadow(
        runner=BrokenRunner(),
        active_symbols=["AAA"],
        tradable_symbols=["AAA"],
        bars_by_symbol={"AAA": _bars(base=100.0)},
        data_age_by_symbol={"AAA": 2.0},
        runtime_data_source_by_symbol={"AAA": "yfinance"},
        data_source=object(),
        runtime_interval="15m",
        now=NOW,
    )

    assert result["triggered"] is False
    assert result["reason"] == "capture_error"
    assert result["error"] == "OSError:shadow disk unavailable"
    assert warnings


def test_world_shadow_can_learn_a_label_and_predict_the_same_fresh_snapshot(tmp_path) -> None:
    from trader.application.world_model import labeler
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import (
        WorldModelBackgroundRunner,
        WorldModelRuntime,
    )

    store = WorldModelStore(tmp_path / "world_model.db")
    runner = WorldModelBackgroundRunner(
        runtime=WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=labeler,
            bar_provider=None,
        )
    )
    try:
        first = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _quarter_hour_bars(count=2, base=100.0)},
            data_age_by_symbol={"AAA": 0.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW,
        )
        first["_thread"].join(timeout=2)

        four_hours_later = NOW + timedelta(hours=4)
        second = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _quarter_hour_bars(count=18, base=100.0)},
            data_age_by_symbol={"AAA": 0.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=four_hours_later,
        )
        second["_thread"].join(timeout=2)

        status = runner.status()
        assert status["mature"]["outcomes_appended"] == 1
        assert status["capture"]["errors"] == []
        assert store.counts() == {
            "episodes": 2,
            "outcome_events": 1,
            "predictions": 4,
        }
    finally:
        runner.stop()
        store.close()


def test_world_shadow_disabled_is_a_noop() -> None:
    assert daemon._trigger_world_model_shadow(
        runner=None,
        active_symbols=["AAA"],
        tradable_symbols=["AAA"],
        bars_by_symbol={"AAA": _bars(base=100.0)},
        data_age_by_symbol={"AAA": 2.0},
        runtime_data_source_by_symbol={"AAA": "yfinance"},
        data_source=object(),
        runtime_interval="15m",
        now=NOW,
    ) == {"triggered": False, "reason": "disabled"}


def _contains_control_field(value: object) -> bool:
    forbidden = {
        "action",
        "decision",
        "portfolio",
        "scheduler",
        "risk",
        "prompt",
        "tools",
        "llm",
        "qty",
        "fill",
        "pnl",
        "mandate",
        "memory",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or _contains_control_field(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_control_field(item) for item in value)
    return False
