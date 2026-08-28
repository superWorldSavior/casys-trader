from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_resource import (
    DEFAULT_MAX_DB_BYTES,
    REASON_DB_SIZE_EXCEEDED,
    REASON_PROBE_ERROR,
    WorldResourceBudget,
    WorldResourceUsage,
)
from trader.runtime.world_model_runtime import WorldModelBackgroundRunner


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)
GiB = 1024**3


class RecordingLog:
    def __init__(self) -> None:
        self.warnings: list[object] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(args[0] if args else message)


class RecordingRuntime:
    def __init__(self, trader_callback=None) -> None:
        self.trader_callback = trader_callback
        self.mature_calls: list[object] = []
        self.capture_calls: list[object] = []

    def mature_pending(self, now: datetime, *, bars_by_symbol=None) -> dict[str, object]:
        self.mature_calls.append({"now": now, "bars": bars_by_symbol})
        if self.trader_callback is not None:
            self.trader_callback("mature")
        return {"status": "ok", "as_of": now.isoformat()}

    def capture_and_predict(self, episodes, *, now: datetime | None = None) -> dict[str, object]:
        self.capture_calls.append({"episodes": tuple(episodes), "now": now})
        if self.trader_callback is not None:
            self.trader_callback("capture")
        return {"status": "ok"}


class RecordingEnricher:
    def __init__(self) -> None:
        self.calls = 0

    def enrich(self, episodes):
        self.calls += 1
        return tuple(episodes)


class RecordingPatternWorkflow:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, _as_of: datetime) -> dict[str, object]:
        self.calls += 1
        return {"status": "completed"}


class FakeProbe:
    def __init__(self, usage: WorldResourceUsage | BaseException) -> None:
        self.usage = usage
        self.calls = 0

    def measure(self) -> WorldResourceUsage:
        self.calls += 1
        if isinstance(self.usage, BaseException):
            raise self.usage
        return self.usage


def _usage(**overrides: object) -> WorldResourceUsage:
    values: dict[str, object] = {
        "logical_bytes": 64 * 1024 * 1024,
        "on_disk_bytes": 80 * 1024 * 1024,
        "free_bytes": 7 * GiB,
        "store_exists": True,
    }
    values.update(overrides)
    return WorldResourceUsage(**values)  # type: ignore[arg-type]


def _guard(probe: FakeProbe):
    from trader.application.world_model.resource_budget import WorldResourceBudgetGuard

    return WorldResourceBudgetGuard(
        budget=WorldResourceBudget.conservative_defaults(),
        probe=probe,
    )


def _episode(episode_id: str) -> dict[str, object]:
    return {
        "episode_id": episode_id,
        "observation": {"symbol": "SPY", "bar_interval": "15m", "features": {"close": 100.0}},
    }


def _join(triggered: dict[str, object]) -> None:
    thread = triggered.get("_thread")
    assert thread is not None
    thread.join(timeout=2)


def test_under_budget_write_batch_probes_once_then_captures() -> None:
    probe = FakeProbe(_usage())
    runtime = RecordingRuntime()
    runner = WorldModelBackgroundRunner(runtime=runtime, resource_guard=_guard(probe))
    triggered = runner.trigger(
        episodes=[_episode("e-one"), _episode("e-two")],
        now=NOW,
        bars_by_symbol={"SPY": [{"close": 100.0}]},
        reason="market_snapshot_pre_dispatch",
    )
    assert triggered["triggered"] is True
    _join(triggered)
    assert probe.calls == 1
    assert len(runtime.mature_calls) == 1
    assert len(runtime.capture_calls) == 1
    status = runner.status()
    assert status["resource_budget"]["status"] == "allowed"
    assert status["capture"]["status"] == "ok"


def test_db_limit_skips_capture_and_training_for_the_cycle() -> None:
    probe = FakeProbe(_usage(logical_bytes=DEFAULT_MAX_DB_BYTES, on_disk_bytes=DEFAULT_MAX_DB_BYTES))
    runtime = RecordingRuntime()
    enricher = RecordingEnricher()
    patterns = RecordingPatternWorkflow()
    runner = WorldModelBackgroundRunner(
        runtime=runtime,
        resource_guard=_guard(probe),
        context_enricher=enricher,
        graph_enricher=enricher,
        pattern_workflow=patterns,
    )
    triggered = runner.trigger(episodes=[_episode("e-skip")], now=NOW)
    assert triggered["triggered"] is True
    _join(triggered)
    assert probe.calls == 1
    assert runtime.mature_calls == []
    assert runtime.capture_calls == []
    assert enricher.calls == 0
    assert patterns.calls == 0
    status = runner.status()
    assert status["status"] == "skipped"
    assert status["reason"] == REASON_DB_SIZE_EXCEEDED
    assert status["resource_budget"]["reason"] == REASON_DB_SIZE_EXCEEDED
    assert "capture" not in status


def test_port_error_fail_safe_skips_writes_without_raising() -> None:
    probe = FakeProbe(OSError("stat failed"))
    runtime = RecordingRuntime()
    runner = WorldModelBackgroundRunner(runtime=runtime, resource_guard=_guard(probe))
    triggered = runner.trigger(episodes=[_episode("e-probe")], now=NOW)
    _join(triggered)
    assert runtime.mature_calls == []
    assert runtime.capture_calls == []
    status = runner.status()
    assert status["status"] == "skipped"
    assert status["reason"] == REASON_PROBE_ERROR


def test_rate_limited_observability_warns_once_per_interval() -> None:
    probe = FakeProbe(_usage(logical_bytes=DEFAULT_MAX_DB_BYTES))
    log = RecordingLog()
    runner = WorldModelBackgroundRunner(
        runtime=RecordingRuntime(),
        resource_guard=_guard(probe),
        logger=log,
    )
    first = runner.trigger(episodes=[_episode("e-a")], now=NOW)
    _join(first)
    second = runner.trigger(episodes=[_episode("e-b")], now=NOW + timedelta(seconds=1))
    _join(second)
    assert len(log.warnings) == 1
    payload = log.warnings[0]
    assert isinstance(payload, dict)
    assert payload["stage"] == "resource_budget"
    assert payload["reason"] == REASON_DB_SIZE_EXCEEDED
    assert payload["status"] == "skipped"


def test_resource_budget_skip_does_not_invoke_or_affect_trader_callback() -> None:
    trader_calls: list[str] = []

    def trader_callback(*_args: object, **_kwargs: object) -> None:
        trader_calls.append("trader")

    with pytest.raises(TypeError):
        WorldModelBackgroundRunner(
            runtime=RecordingRuntime(),
            trader_callback=trader_callback,
        )

    signature = inspect.signature(WorldModelBackgroundRunner.__init__)
    for name in signature.parameters:
        lowered = name.lower()
        assert "trader" not in lowered
        assert "brain" not in lowered
        assert "broker" not in lowered
        assert "universe" not in lowered
        assert "callback" not in lowered

    runtime = RecordingRuntime(trader_callback=trader_callback)
    runner = WorldModelBackgroundRunner(
        runtime=runtime,
        resource_guard=_guard(FakeProbe(_usage(logical_bytes=DEFAULT_MAX_DB_BYTES))),
    )
    triggered = runner.trigger(episodes=[_episode("e-isolated")], now=NOW)
    _join(triggered)
    trader_callback()
    assert trader_calls == ["trader"]
    assert runtime.mature_calls == []
    assert runtime.capture_calls == []
    assert runner.status()["reason"] == REASON_DB_SIZE_EXCEEDED


def test_compose_guard_does_not_create_missing_store(tmp_path) -> None:
    from trader.runtime.world_model_runtime import compose_world_resource_guard

    db_path = tmp_path / "world_model.db"
    guard = compose_world_resource_guard(db_path=db_path, config_dir=REPO_ROOT / "config")
    evaluation = guard.evaluate(now=NOW)
    assert db_path.exists() is False
    assert evaluation.decision.budget.max_db_bytes == 3 * GiB


def test_daemon_wires_typed_resource_guard_without_magic_env_reads() -> None:
    daemon_source = (REPO_ROOT / "trader" / "runtime" / "daemon.py").read_text(encoding="utf-8")
    runtime_source = (REPO_ROOT / "trader" / "runtime" / "world_model_runtime.py").read_text(encoding="utf-8")
    assert "compose_world_resource_guard" in daemon_source
    assert "resource_guard=" in daemon_source
    assert "CASYS_WORLD_RESOURCE" not in daemon_source
    assert "os.environ" not in runtime_source
    assert "VACUUM" not in runtime_source
    assert "TRUNCATE" not in runtime_source
