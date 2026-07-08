from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trader.runtime import market_rotation_runtime


@dataclass(frozen=True)
class FakeRadarParams:
    override_enabled: bool


class RecordingLogger:
    def __init__(self) -> None:
        self.exceptions: list[tuple] = []

    def exception(self, *args: object) -> None:
        self.exceptions.append(args)


def test_build_llm_override_fn_returns_callable(monkeypatch) -> None:
    from trader.agent import llm as llm_module

    class _FakeCompletion:
        text = '{"add":[],"remove":[]}'

    class _FakeRouter:
        def complete(self, prompt, *, timeout_s):
            return _FakeCompletion()

    monkeypatch.setattr(llm_module, "build_default_router_from_env", lambda **kw: _FakeRouter())

    fn = market_rotation_runtime.build_llm_override_fn()

    assert callable(fn)


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
    calls: list[dict] = []

    def build_context(payload):
        calls.append({"build_context_payload": payload})
        return market_context

    def tick(*args, **kwargs):
        calls.append({"tick_args": args, "tick_kwargs": kwargs})

    market_rotation_runtime.tick_market_rotation(
        config_dir=config_dir,
        state_dir=state_dir,
        loop_now=now,
        load_radar_params_fn=lambda _config_dir: FakeRadarParams(override_enabled=True),
        build_override_fn=lambda: override,
        build_market_context_from_regime_fn=build_context,
        rotation_tick_fn=tick,
    )

    assert calls[0] == {"build_context_payload": {"family_bias": {"tech": 0.7}}}
    assert calls[1]["tick_args"] == (config_dir, state_dir, now.isoformat())
    assert calls[1]["tick_kwargs"]["override_fn"] is override
    assert callable(calls[1]["tick_kwargs"]["sticky_fn"])
    assert calls[1]["tick_kwargs"]["market_context"] is market_context


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
