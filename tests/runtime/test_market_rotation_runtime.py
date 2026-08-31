from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from trader.runtime import market_rotation_runtime


@dataclass(frozen=True)
class FakeRadarParams:
    override_enabled: bool


class RecordingLogger:
    def __init__(self) -> None:
        self.exceptions: list[tuple] = []
        self.warnings: list[tuple] = []

    def exception(self, *args: object) -> None:
        self.exceptions.append(args)

    def warning(self, *args: object) -> None:
        self.warnings.append(args)


def test_load_effective_universe_propagates_injected_cycle_clock(
    monkeypatch,
    tmp_path,
) -> None:
    universe_path = tmp_path / "universe.yaml"
    universe_path.write_text("symbols:\n  - SPY\n", encoding="utf-8")
    cycle_now = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    observed: list[datetime] = []

    def build_sticky(_state_dir, *, now_fn=None):
        def sticky():
            observed.append(now_fn())
            return set()

        return sticky

    monkeypatch.setattr(market_rotation_runtime, "build_sticky_fn", build_sticky)

    symbols = market_rotation_runtime.load_effective_universe(
        universe_path,
        tmp_path / "state",
        now=cycle_now,
    )

    assert symbols == ["SPY"]
    assert observed == [cycle_now]


def test_build_llm_override_fn_returns_callable(monkeypatch) -> None:
    from trader.agent import llm as llm_module

    captured = {}

    class _FakeCompletion:
        text = '{"add":[],"remove":[]}'

    class _FakeRouter:
        def complete(self, prompt, *, timeout_s):
            return _FakeCompletion()

    def build_router(**kwargs):
        captured.update(kwargs)
        return _FakeRouter()

    monkeypatch.setattr(llm_module, "build_default_router_from_env", build_router)

    fn = market_rotation_runtime.build_llm_override_fn()

    assert callable(fn)
    assert captured["spark_model"] == "gpt-5.6-sol"


def test_build_llm_override_fn_parses_llm_response(monkeypatch) -> None:
    from trader.agent import llm as llm_module

    class _FakeCompletion:
        text = '{"add":[],"remove":[]}'

    class _FakeRouter:
        def complete(self, prompt, *, timeout_s):
            return _FakeCompletion()

    monkeypatch.setattr(llm_module, "build_default_router_from_env", lambda **kw: _FakeRouter())

    fn = market_rotation_runtime.build_llm_override_fn()
    result = fn({"ranked": [], "default_hot": []})

    assert result == {"add": [], "remove": []}


def test_tick_market_rotation_passes_override_and_cached_market_context(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "last_regime.json").write_text('{"family_bias": {"tech": 0.7}}')
    now = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    override = object()
    market_context = object()
    news_challenger_fn = object()
    challenger_build_kwargs = {}
    calls: list[dict] = []

    def build_context(payload):
        calls.append({"build_context_payload": payload})
        return market_context

    def tick(*args, **kwargs):
        calls.append({"tick_args": args, "tick_kwargs": kwargs})

    def build_challengers(**kwargs):
        challenger_build_kwargs.update(kwargs)
        return news_challenger_fn

    market_rotation_runtime.tick_market_rotation(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        load_radar_params_fn=lambda _config_dir: FakeRadarParams(override_enabled=True),
        build_override_fn=lambda: override,
        build_market_context_from_regime_fn=build_context,
        build_news_challenger_fn=build_challengers,
        rotation_tick_fn=tick,
    )

    assert calls[0] == {"build_context_payload": {"family_bias": {"tech": 0.7}}}
    assert calls[1]["tick_args"] == (config_dir, state_dir, now.isoformat())
    assert calls[1]["tick_kwargs"]["override_fn"] is override
    assert calls[1]["tick_kwargs"]["prepared_universe_fn"] is None
    assert callable(calls[1]["tick_kwargs"]["candidate_scope_observer"])
    assert callable(calls[1]["tick_kwargs"]["radar_score_audit_observer"])
    assert callable(calls[1]["tick_kwargs"]["sticky_fn"])
    assert calls[1]["tick_kwargs"]["market_context"] is market_context
    assert calls[1]["tick_kwargs"]["news_challenger_fn"] is news_challenger_fn
    assert challenger_build_kwargs == {
        "config_dir": config_dir,
        "state_dir": state_dir,
        "as_of": now,
    }


def test_tick_market_rotation_uses_prepared_provider_by_default_and_persists_scope(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    now = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    prepared_provider = object()
    calls: list[dict] = []

    def tick(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        kwargs["candidate_scope_observer"](
            {
                "candidate_scope_id": "scope-us-1",
                "venue": "US",
                "as_of": now.isoformat(),
                "candidates": [{"symbol": "AAPL"}],
                "default_hotlist": ["AAPL"],
            }
        )

    market_rotation_runtime.tick_market_rotation(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        load_radar_params_fn=lambda _config_dir: FakeRadarParams(override_enabled=True),
        build_prepared_universe_provider_fn=lambda _state_dir: prepared_provider,
        build_market_context_from_regime_fn=lambda _payload: None,
        rotation_tick_fn=tick,
    )

    assert calls[0]["kwargs"]["override_fn"] is None
    assert calls[0]["kwargs"]["prepared_universe_fn"] is prepared_provider
    current = json.loads(
        (state_dir / "candidate_scopes" / "current-US.json").read_text(encoding="utf-8")
    )
    assert current["candidate_scope_id"] == "scope-us-1"


def test_radar_score_audit_observer_writes_atomic_shadow_artifact(tmp_path) -> None:
    observer = market_rotation_runtime.build_radar_score_audit_observer(tmp_path)

    ref = observer(
        {
            "schema_version": 1,
            "status": "shadow_only",
            "selection_effect": "none",
            "input_signature": "abc123",
        }
    )

    payload = json.loads((tmp_path / "radar_score_audit.json").read_text(encoding="utf-8"))
    assert payload["selection_effect"] == "none"
    assert ref == {"path": "radar_score_audit.json", "input_signature": "abc123"}


def test_prepared_provider_distinguishes_missing_scope_brief_pending_and_success(tmp_path) -> None:
    from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
    from trader.infrastructure.state_db.universe_run_store import UniverseRunStore
    from trader.domain.situation import NewsMacroBrief

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    provider = market_rotation_runtime.build_prepared_universe_fn(state_dir)
    as_of = "2026-07-10T12:00:00+00:00"

    assert provider(
        venue="EU",
        candidate_scope_id="",
        as_of=as_of,
    )["reason"] == "candidate_scope_missing"
    assert provider(
        venue="EU",
        candidate_scope_id="scope-eu-1",
        as_of=as_of,
    )["reason"] == "brief_missing"

    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "brief-eu-1",
            "venue": "EU",
            "as_of": "2026-07-10T11:00:00+00:00",
            "valid_until": "2026-07-11T07:00:00+00:00",
            "input_refs": {"candidate_scope_id": "scope-eu-1"},
        }
    )
    assert brief is not None
    NewsMacroBriefStore(state_dir / "news_briefs").append(brief)
    assert provider(
        venue="EU",
        candidate_scope_id="scope-eu-1",
        as_of=as_of,
    )["reason"] == "prepare_pending"

    run = {
        "status": "success",
        "candidate_scope_id": "scope-eu-1",
        "venue": "EU",
        "as_of": as_of,
        "valid_until": "2026-07-11T07:00:00+00:00",
        "selected_hotlist": ["AIR.PA"],
    }
    store = UniverseRunStore(state_dir / "universe_runs")
    store.append(run)
    store.write_prepared("scope-eu-1", run)
    assert provider(
        venue="EU",
        candidate_scope_id="scope-eu-1",
        as_of=as_of,
    )["selected_hotlist"] == ["AIR.PA"]


def test_prepared_provider_does_not_activate_yesterday_hotlist_under_waiting_overlay(
    tmp_path,
) -> None:
    from trader.infrastructure.state_db.universe_run_store import UniverseRunStore

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    provider = market_rotation_runtime.build_prepared_universe_fn(state_dir)
    as_of = "2026-07-10T13:15:00+00:00"
    store = UniverseRunStore(state_dir / "universe_runs")
    yesterday = {
        "status": "success",
        "candidate_scope_id": "scope-yesterday",
        "venue": "US",
        "as_of": "2026-07-09T20:05:00+00:00",
        "valid_until": "2026-07-10T20:00:00+00:00",
        "selected_hotlist": ["AAPL", "MSFT"],
    }
    waiting = {
        "status": "waiting_brief",
        "candidate_scope_id": "scope-today",
        "venue": "US",
        "as_of": as_of,
        "error_code": "brief_scope_mismatch",
    }
    store.append(yesterday)
    store.write_prepared("scope-yesterday", yesterday)
    store.append(waiting)

    latest = store.read_latest("US")
    assert latest["selected_hotlist"] == ["AAPL", "MSFT"]
    assert latest["latest_waiting"]["candidate_scope_id"] == "scope-today"

    result = provider(venue="US", candidate_scope_id="scope-today", as_of=as_of)
    assert result.get("selected_hotlist") != ["AAPL", "MSFT"]
    assert result["status"] in {"missing", "waiting_brief", "pending"}
    assert result["reason"] in {
        "brief_missing",
        "brief_scope_mismatch",
        "agent_brief_scope_mismatch",
        "prepare_pending",
    }


def test_tick_market_rotation_ignores_invalid_cached_regime(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "last_regime.json").write_text("{bad json")
    now = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    calls: list[dict] = []

    def tick(*args, **kwargs):
        calls.append({"tick_args": args, "tick_kwargs": kwargs})

    market_rotation_runtime.tick_market_rotation(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        load_radar_params_fn=lambda _config_dir: FakeRadarParams(override_enabled=False),
        build_override_fn=lambda: object(),
        build_market_context_from_regime_fn=lambda _payload: object(),
        rotation_tick_fn=tick,
    )

    assert len(calls) == 1
    assert calls[0]["tick_args"] == (config_dir, state_dir, now.isoformat())
    assert calls[0]["tick_kwargs"]["override_fn"] is None
    assert callable(calls[0]["tick_kwargs"]["sticky_fn"])
    assert calls[0]["tick_kwargs"]["market_context"] is None
    assert callable(calls[0]["tick_kwargs"]["news_challenger_fn"])


def test_tick_market_rotation_ignores_market_context_builder_errors(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "last_regime.json").write_text('{"family_bias": {"tech": 0.7}}')
    now = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    calls: list[dict] = []

    def build_context(_payload):
        raise RuntimeError("bad regime schema")

    def tick(*args, **kwargs):
        calls.append({"tick_args": args, "tick_kwargs": kwargs})

    market_rotation_runtime.tick_market_rotation(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        load_radar_params_fn=lambda _config_dir: FakeRadarParams(override_enabled=False),
        build_market_context_from_regime_fn=build_context,
        rotation_tick_fn=tick,
    )

    assert len(calls) == 1
    assert calls[0]["tick_args"] == (config_dir, state_dir, now.isoformat())
    assert calls[0]["tick_kwargs"]["override_fn"] is None
    assert callable(calls[0]["tick_kwargs"]["sticky_fn"])
    assert calls[0]["tick_kwargs"]["market_context"] is None
    assert callable(calls[0]["tick_kwargs"]["news_challenger_fn"])


def test_tick_market_rotation_logs_and_swallows_errors(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    logger = RecordingLogger()

    def tick(*_args, **_kwargs):
        raise RuntimeError("rotation broken")

    market_rotation_runtime.tick_market_rotation(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc),
        load_radar_params_fn=lambda _config_dir: FakeRadarParams(override_enabled=False),
        build_market_context_from_regime_fn=lambda _payload: object(),
        rotation_tick_fn=tick,
        logger=logger,
    )

    assert logger.exceptions == [("rotation tick (D10) échouée",)]


def test_tick_market_rotation_keeps_deterministic_rotation_when_challenger_builder_fails(
    tmp_path,
) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    logger = RecordingLogger()
    calls: list[dict] = []

    def build_challengers(**_kwargs):
        raise RuntimeError("bad challenger state")

    def tick(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

    market_rotation_runtime.tick_market_rotation(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc),
        load_radar_params_fn=lambda _config_dir: FakeRadarParams(override_enabled=False),
        build_market_context_from_regime_fn=lambda _payload: object(),
        build_news_challenger_fn=build_challengers,
        rotation_tick_fn=tick,
        logger=logger,
    )

    assert len(calls) == 1
    assert calls[0]["kwargs"]["news_challenger_fn"] is None
    assert len(logger.warnings) == 1
    assert logger.warnings[0][0] == "news challenger provider indisponible: %s"
    assert isinstance(logger.warnings[0][1], RuntimeError)
    assert str(logger.warnings[0][1]) == "bad challenger state"
