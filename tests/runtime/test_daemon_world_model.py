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

    assert (
        daemon._world_model_snapshot_cutoff(
            started,
            completed_at=completed,
        )
        == completed
    )
    assert (
        daemon._world_model_snapshot_cutoff(
            completed,
            completed_at=started,
        )
        == completed
    )


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
    from trader.application.world_model.gru import OnlineGRUWorldChallenger
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
            predictors=(OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4),),
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
            "predictions": 8,
        }
        assert {row["horizon_code"] for row in store.list_predictions()} == {"elapsed_4h.v1", "elapsed_1d.v1"}

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
            "predictions": 8,
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
            "predictions": 8,
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


def test_world_shadow_trigger_never_reads_context_on_the_cycle_thread(monkeypatch) -> None:
    class Boom:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("WorldContextReader must not be constructed on the daemon thread")

    monkeypatch.setattr(
        "trader.infrastructure.state_db.world_context_reader.WorldContextReader",
        Boom,
    )
    runner = _Runner()
    result = daemon._trigger_world_model_shadow(
        runner=runner,
        active_symbols=["AAA"],
        tradable_symbols=["AAA"],
        bars_by_symbol={"AAA": _bars(base=100.0)},
        data_age_by_symbol={"AAA": 3.0},
        runtime_data_source_by_symbol={"AAA": "yfinance"},
        data_source=object(),
        runtime_interval="15m",
        now=NOW,
        context_v2_enabled=True,
    )
    assert result["triggered"] is True
    episodes = runner.calls[0]["episodes"]
    assert len(episodes) == 1
    assert episodes[0].observation.context is None


def test_repeated_poll_of_the_same_bar_keeps_one_v2_row(tmp_path) -> None:
    from trader.application.world_model import labeler
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.gru import OnlineGRUWorldChallenger
    from trader.domain.world_context import ALLOWED_CONTEXT_CATEGORICAL_FEATURES, CONTEXT_FEATURE_CONTRACT_VERSION
    from trader.infrastructure.state_db.world_context_reader import WorldContextReader
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import (
        WorldContextEpisodeEnricher,
        WorldModelBackgroundRunner,
        WorldModelRuntime,
    )

    store = WorldModelStore(tmp_path / "world_model.db")
    enricher = WorldContextEpisodeEnricher(
        WorldContextReader(news_dir=tmp_path / "news", company_dir=tmp_path / "company")
    )
    runner = WorldModelBackgroundRunner(
        runtime=WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            predictors=(
                OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4),
                HierarchicalDirichletWorldBaseline(
                    model_version="context.v2",
                    include_context=True,
                    accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_VERSION}),
                ),
                OnlineGRUWorldChallenger(
                    model_version="context.v2",
                    encoder_version="world_gru_encoder.v2",
                    hidden_size=4,
                    sequence_len=4,
                    include_context=True,
                    extra_categorical_keys=ALLOWED_CONTEXT_CATEGORICAL_FEATURES,
                    accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_VERSION}),
                ),
            ),
            labeler=labeler,
            bar_provider=None,
        ),
        context_enricher=enricher,
    )
    try:
        first = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _bars(base=100.0)},
            data_age_by_symbol={"AAA": 3.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW,
        )
        first["_thread"].join(timeout=2)
        replay = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _bars(base=100.0)},
            data_age_by_symbol={"AAA": 8.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW + timedelta(minutes=5),
        )
        replay["_thread"].join(timeout=2)
        counts = store.counts()
        assert counts["episodes"] == 2
        versions = {
            (row.get("feature_contract_version") or row["observation"]["feature_contract_version"])
            for row in store.list_eligible_episodes()
        }
        assert "market_ohlcv_causal.v1" in versions
        assert CONTEXT_FEATURE_CONTRACT_VERSION in versions
        revised = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _bars(base=101.0)},
            data_age_by_symbol={"AAA": 9.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW + timedelta(minutes=6),
        )
        revised["_thread"].join(timeout=2)
        capture = runner.status()["capture"]
        assert capture["status"] == "partial"
        assert any("existing_episode_market_evidence_conflict" in item["error"] for item in capture["errors"])
        assert store.counts()["episodes"] == 2
    finally:
        runner.stop()
        store.close()


def test_context_enrichment_failure_keeps_v1_and_is_visible(tmp_path) -> None:
    from trader.application.world_model import labeler
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import WorldModelBackgroundRunner, WorldModelRuntime

    class BrokenEnricher:
        def enrich(self, _episodes):
            raise OSError("context disk unavailable")

    store = WorldModelStore(tmp_path / "world_model.db")
    runner = WorldModelBackgroundRunner(
        runtime=WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=labeler,
            bar_provider=None,
        ),
        context_enricher=BrokenEnricher(),
    )
    try:
        result = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _bars(base=100.0)},
            data_age_by_symbol={"AAA": 3.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW,
        )
        result["_thread"].join(timeout=2)
        status = runner.status()
        assert status["status"] == "partial"
        assert status["errors"][0]["stage"] == "context_enrich"
        assert store.counts()["episodes"] == 1
        assert store.list_eligible_episodes()[0]["observation"]["feature_contract_version"] == "market_ohlcv_causal.v1"
    finally:
        runner.stop()
        store.close()


def test_flag_off_keeps_two_lanes_without_an_enricher() -> None:
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.gru import OnlineGRUWorldChallenger
    from trader.runtime.world_model_runtime import WorldModelBackgroundRunner

    runner = WorldModelBackgroundRunner(
        runtime=object(),  # type: ignore[arg-type]
        context_enricher=None,
    )
    assert runner.context_enricher is None
    identities = {
        (HierarchicalDirichletWorldBaseline().model_id, HierarchicalDirichletWorldBaseline().model_version),
        (OnlineGRUWorldChallenger().model_id, OnlineGRUWorldChallenger().model_version),
    }
    assert len(identities) == 2


def test_daemon_boot_wires_world_cohort_service_but_never_registers_or_starts() -> None:
    from inspect import signature
    from pathlib import Path

    source = Path(daemon.__file__).read_text(encoding="utf-8")
    boot_start = source.index("if _env_int(\"CASYS_WORLD_MODEL_SHADOW_ENABLED\"")
    boot = source[boot_start : source.index("claimed_resources.learning_sync_runner", boot_start)]
    assert "WorldCohortService(" in boot
    assert "repository=_world_model_store" in boot
    assert "query=_world_model_store" in boot
    assert "cohort_service=_world_cohort_service" in boot
    assert "RegisterWorldCohort" not in boot
    assert "ArmWorldCohort" not in boot
    assert "StartWorldCohort" not in boot
    assert "AdmitWorldCohortSlot" not in boot
    assert ".register(" not in boot
    assert ".arm(" not in boot
    assert ".start(" not in boot.replace("thread.start()", "")
    assert "HierarchicalDirichletWorldBaseline()" in boot
    assert "OnlineGRUWorldChallenger()" in boot
    assert "authority=shadow_only" in boot
    assert signature(daemon.run_cycle).parameters["world_model_runner"].default is None


def test_daemon_shadow_without_active_cohort_keeps_v1_gru_lanes(tmp_path) -> None:
    from trader.application.world_model import labeler
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.application.world_model.gru import OnlineGRUWorldChallenger
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import WorldModelBackgroundRunner, WorldModelRuntime

    store = WorldModelStore(tmp_path / "world_model.db")
    cohort_service = WorldCohortService(repository=store, query=store)
    runner = WorldModelBackgroundRunner(
        runtime=WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            predictors=(OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4),),
            labeler=labeler,
            bar_provider=None,
            cohort_service=cohort_service,
        )
    )
    try:
        assert store.list_collecting_cohort_ids() == ()
        result = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _bars(base=100.0)},
            data_age_by_symbol={"AAA": 3.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW,
        )
        assert result["triggered"] is True
        result["_thread"].join(timeout=2)
        assert runner.status()["running"] is False
        rows = store.list_predictions()
        assert rows
        assert all(not row.get("study_cohort_id") for row in rows)
        assert all((row.get("prediction_record") or {}).get("decision_effect", "none") == "none" for row in rows)
        assert {row["model_kind"] for row in rows} >= {
            HierarchicalDirichletWorldBaseline().model_id,
            OnlineGRUWorldChallenger().model_id,
        }
    finally:
        runner.stop()
        store.close()


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
        return any(str(key).lower() in forbidden or _contains_control_field(nested) for key, nested in value.items())
    if isinstance(value, (tuple, list)):
        return any(_contains_control_field(item) for item in value)
    return False


def test_macro_flag_defaults_off_via_existing_env_int_surface() -> None:
    from pathlib import Path

    source = Path(daemon.__file__).read_text(encoding="utf-8")
    assert '_env_int("CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED", 0)' in source
    assert "CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED=1" not in source


def test_run_cycle_has_no_macro_io_or_runner_parameter() -> None:
    import inspect

    signature = inspect.signature(daemon.run_cycle)
    assert "world_macro_runner" not in signature.parameters
    cycle_src = inspect.getsource(daemon.run_cycle)
    assert "world_macro" not in cycle_src
    assert "macro_source_only" not in cycle_src
    assert "wire_world_macro_runtime" not in cycle_src
    assert "_trigger_world_macro_source_only" not in cycle_src


def test_macro_producer_boot_is_independent_of_v2_and_does_not_start_cohort() -> None:
    from pathlib import Path

    source = Path(daemon.__file__).read_text(encoding="utf-8")
    macro_flag = source.index('_env_int("CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED", 0)')
    v2_flag = source.index('_env_int("CASYS_WORLD_MODEL_CONTEXT_V2_ENABLED", 0)')
    shadow_if = source.index('if _env_int("CASYS_WORLD_MODEL_SHADOW_ENABLED"')
    macro_if = source.index("if _world_macro_source_only:")
    v2_if = source.index("if _world_model_context_v2:")
    assert macro_if < shadow_if
    assert macro_if != v2_if
    assert v2_flag < shadow_if
    boot = source[macro_flag:shadow_if]
    assert "wire_world_macro_runtime(" in boot
    assert ".trigger(" not in boot
    assert "RegisterWorldCohort" not in boot
    assert "ArmWorldCohort" not in boot
    assert "StartWorldCohort" not in boot
    assert "AdmitWorldCohortSlot" not in boot
    assert ".register(" not in boot
    assert ".arm(" not in boot
    assert ".start(" not in boot
    assert "market_sources.world_macro" not in source
    assert "build_macro_source_ports" not in source
    assert "DBnomicsSeriesAdapter" not in source
    assert "YahooCommodityAdapter" not in source


def test_v2_predictors_use_macro_lane_identity_only_when_producer_store_is_wired() -> None:
    from pathlib import Path

    from trader.runtime.world_macro_runtime import MACRO_LANE_IDENTITY

    source = Path(daemon.__file__).read_text(encoding="utf-8")
    assert MACRO_LANE_IDENTITY == "context.v2.macro_source.v1"
    assert "MACRO_LANE_IDENTITY" in source
    assert 'else "context.v2"' in source
    assert "macro_store=" in source
    assert "scope_mapping=" in source
    shadow_start = source.index('if _env_int("CASYS_WORLD_MODEL_SHADOW_ENABLED"')
    v2_block = source[source.index("if _world_model_context_v2:", shadow_start) :]
    assert "MACRO_LANE_IDENTITY" in v2_block
    assert 'else "context.v2"' in v2_block


def test_world_macro_disabled_is_a_noop() -> None:
    assert daemon._trigger_world_macro_source_only(runner=None, now=NOW) == {
        "triggered": False,
        "reason": "disabled",
    }


def test_world_macro_trigger_failure_is_logged_and_never_escapes(monkeypatch) -> None:
    class BrokenRunner:
        def trigger(self, **_kwargs: object) -> None:
            raise OSError("macro disk unavailable")

    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(daemon.log, "warning", lambda *args: warnings.append(args))

    result = daemon._trigger_world_macro_source_only(runner=BrokenRunner(), now=NOW)

    assert result["triggered"] is False
    assert result["reason"] == "trigger_error"
    assert result["error"] == "OSError:macro disk unavailable"
    assert warnings


def test_daemon_post_cycle_macro_trigger_cannot_break_trader_loop() -> None:
    import inspect
    from pathlib import Path

    source = Path(daemon.__file__).read_text(encoding="utf-8")
    main_idx = source.index("def main(")
    assert "_trigger_world_macro_source_only(" in source[main_idx:]
    loop_idx = main_idx + source[main_idx:].index("_trigger_world_macro_source_only(")
    assert "if not args.once:" in source[loop_idx - 400 : loop_idx]
    assert "_trigger_world_macro_source_only" not in inspect.getsource(daemon.run_cycle)


def test_graph_v3_flag_defaults_off_via_existing_env_int_surface() -> None:
    from pathlib import Path

    from trader.runtime.world_macro_runtime import GRAPH_V3_FLAG

    source = Path(daemon.__file__).read_text(encoding="utf-8")
    assert GRAPH_V3_FLAG == "CASYS_WORLD_MODEL_GRAPH_V3_ENABLED"
    assert '_env_int("CASYS_WORLD_MODEL_GRAPH_V3_ENABLED", 0)' in source
    assert "CASYS_WORLD_MODEL_GRAPH_V3_ENABLED=1" not in source
    assert "CASYS_WORLD_MODEL_GRAPH_ENABLED" not in source


def test_daemon_boot_wires_graph_v3_only_inside_shadow_without_starting_cohort() -> None:
    from pathlib import Path

    source = Path(daemon.__file__).read_text(encoding="utf-8")
    flag_idx = source.index('_env_int("CASYS_WORLD_MODEL_GRAPH_V3_ENABLED", 0)')
    shadow_if = source.index('if _env_int("CASYS_WORLD_MODEL_SHADOW_ENABLED"')
    boot = source[shadow_if : source.index("claimed_resources.learning_sync_runner", shadow_if)]
    assert flag_idx < shadow_if or "_world_model_graph_v3" in boot
    assert "compose_local_graph_v3_lanes" in boot
    assert "graph_enricher" in boot
    assert "_world_model_graph_v3" in boot
    assert "RegisterWorldCohort" not in boot
    assert "ArmWorldCohort" not in boot
    assert "StartWorldCohort" not in boot
    assert "AdmitWorldCohortSlot" not in boot
    assert ".register(" not in boot
    assert ".arm(" not in boot
    assert ".start(" not in boot.replace("thread.start()", "")
    assert "authority=shadow_only" in boot


def test_world_shadow_trigger_never_attaches_graph_on_the_cycle_thread(monkeypatch) -> None:
    class Boom:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("graph capture must not run on the daemon cycle thread")

        def enrich(self, *_args, **_kwargs):
            raise AssertionError("graph enricher must not run on the daemon cycle thread")

    monkeypatch.setattr(
        "trader.application.world_model.graph_capture.attach_world_graph",
        Boom,
    )
    runner = _Runner()
    result = daemon._trigger_world_model_shadow(
        runner=runner,
        active_symbols=["AAA"],
        tradable_symbols=["AAA"],
        bars_by_symbol={"AAA": _bars(base=100.0)},
        data_age_by_symbol={"AAA": 3.0},
        runtime_data_source_by_symbol={"AAA": "yfinance"},
        data_source=object(),
        runtime_interval="15m",
        now=NOW,
    )
    assert result["triggered"] is True
    episodes = runner.calls[0]["episodes"]
    assert len(episodes) == 1
    assert episodes[0].observation.feature_contract_version == "market_ohlcv_causal.v1"
    assert getattr(episodes[0].observation, "graph", None) is None


def test_graph_v3_enrichment_failure_keeps_v1_and_is_visible(tmp_path) -> None:
    from trader.application.world_model import labeler
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import WorldModelBackgroundRunner, WorldModelRuntime

    class BrokenGraphEnricher:
        def enrich(self, _episodes):
            raise OSError("graph disk unavailable")

    store = WorldModelStore(tmp_path / "world_model.db")
    runner = WorldModelBackgroundRunner(
        runtime=WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=labeler,
            bar_provider=None,
        ),
        graph_enricher=BrokenGraphEnricher(),
    )
    try:
        result = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _bars(base=100.0)},
            data_age_by_symbol={"AAA": 3.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW,
        )
        result["_thread"].join(timeout=2)
        status = runner.status()
        assert status["status"] == "partial"
        assert status["errors"][0]["stage"] == "graph_enrich"
        assert store.counts()["episodes"] == 1
        assert store.list_eligible_episodes()[0]["observation"]["feature_contract_version"] == "market_ohlcv_causal.v1"
        assert store.list_collecting_cohort_ids() == ()
    finally:
        runner.stop()
        store.close()


def test_idle_unmapped_cycle_does_not_write_v3_from_graph_wiring_alone(tmp_path) -> None:
    from pathlib import Path

    from trader.application.world_model import labeler
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.domain.world_feature_contract import GRAPH_FEATURE_CONTRACT_VERSION
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import (
        WorldModelBackgroundRunner,
        WorldModelRuntime,
        compose_local_graph_v3_lanes,
    )

    store = WorldModelStore(tmp_path / "world_model.db")
    repo_config = Path(daemon.__file__).resolve().parents[2] / "config"
    predictors, enricher = compose_local_graph_v3_lanes(
        enabled=True,
        store=store,
        config_dir=repo_config,
    )
    assert enricher is not None
    assert predictors
    assert store.counts()["episodes"] == 0
    runner = WorldModelBackgroundRunner(
        runtime=WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            predictors=predictors,
            labeler=labeler,
            bar_provider=None,
        ),
        graph_enricher=enricher,
    )
    try:
        assert runner.status()["graph"]["wired"] is True
        assert store.counts()["episodes"] == 0
        result = daemon._trigger_world_model_shadow(
            runner=runner,
            active_symbols=["AAA"],
            tradable_symbols=["AAA"],
            bars_by_symbol={"AAA": _bars(base=100.0)},
            data_age_by_symbol={"AAA": 3.0},
            runtime_data_source_by_symbol={"AAA": "yfinance"},
            data_source=object(),
            runtime_interval="15m",
            now=NOW,
        )
        assert result["triggered"] is True
        result["_thread"].join(timeout=2)
        rows = store.list_eligible_episodes()
        versions = {
            (row.get("feature_contract_version") or row["observation"]["feature_contract_version"])
            for row in rows
        }
        assert "market_ohlcv_causal.v1" in versions
        assert GRAPH_FEATURE_CONTRACT_VERSION not in versions
        assert store.list_collecting_cohort_ids() == ()
        assert all((row.get("prediction_record") or {}).get("decision_effect", "none") == "none" for row in store.list_predictions())
    finally:
        runner.stop()
        store.close()
