from __future__ import annotations

from datetime import timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.application.test_world_dynamics_workflow import _inputs, Clock
from tests.runtime.test_daemon_world_model import _quarter_hour_bars
from trader.reporting.read_models.world_dynamics_status import read_world_dynamics_status
from trader.runtime import daemon
from trader.runtime.world_dynamics_runtime import compose_world_dynamics_runtime
from trader.runtime.world_model_runtime import WorldModelBackgroundRunner


class MarketRuntime:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.captured = 0

    def mature_pending(self, now, *, bars_by_symbol):
        return {"provided_bars": sum(len(rows) for rows in bars_by_symbol.values())}

    def capture_and_predict(self, episodes, *, now):
        self.captured += 1
        if self.broken:
            raise ValueError("legacy capture unavailable")
        return {"episodes": len(episodes)}


def _cycle(runner, inputs):
    report = runner.trigger(**inputs)
    assert report["triggered"] is True
    report["_thread"].join(timeout=5)
    assert not report["_thread"].is_alive()
    return runner.status()


def test_default_composition_runs_on_full_snapshot_and_publishes_discoverable_shadow(tmp_path):
    inputs = _inputs(64)
    clock = Clock(inputs["now"] + timedelta(seconds=5))
    dynamics = compose_world_dynamics_runtime(state_dir=tmp_path, clock=clock)
    assert dynamics is not None
    assert json.loads((tmp_path / "world_dynamics_status.json").read_text())["enabled"] is True
    market = MarketRuntime()
    runner = WorldModelBackgroundRunner(runtime=market, dynamics_workflow=dynamics, state_dir=tmp_path)
    status = _cycle(runner, inputs)
    assert status["status"] == "ok"
    assert status["dynamics"]["journal"]["retained_bars"] == 64
    assert status["dynamics"]["status"] == "ready"
    (tmp_path / "daemon.pid").write_text(str(os.getpid()))
    public = read_world_dynamics_status(tmp_path, now=clock())
    assert public["status"] == "ready" and public["process_alive"] is True
    assert public["authority"] == "shadow_only" and public["decision_effect"] == "none"
    assert public["recommendation"] == "NO_GO"
    assert public["series"][0]["support"] == 63
    report = json.loads(open(public["series"][0]["report_path"]).read())
    assert report["evaluation"]["origins"] == 0
    assert report["rollout"]["paths"] == 20
    assert report["scope"]["evidence_kind"] == "observed_market_bar"
    assert market.captured == 1
    assert not (tmp_path / "world_model.db").exists()


def test_identical_snapshot_and_restart_preserve_receipts_and_rebuild_deterministically(tmp_path):
    inputs = _inputs(64)
    clock = Clock(inputs["now"] + timedelta(seconds=5))
    dynamics = compose_world_dynamics_runtime(state_dir=tmp_path, clock=clock)
    first = dynamics.run(**inputs)
    raw_journal = (tmp_path / "world_dynamics" / "bars.json").read_bytes()
    with patch("trader.application.world_model.dynamics_workflow.run_dynamics_replay", side_effect=AssertionError):
        repeated = dynamics.run(**inputs)
    assert repeated.summary["replayed_series"] == 0
    assert (tmp_path / "world_dynamics" / "bars.json").read_bytes() == raw_journal
    restarted = compose_world_dynamics_runtime(state_dir=tmp_path, clock=clock)
    rebuilt = restarted.run(**inputs)
    assert rebuilt.reports[0].result.model_fingerprint == first.reports[0].result.model_fingerprint
    assert rebuilt.reports[0].result.trajectories == first.reports[0].result.trajectories
    assert (tmp_path / "world_dynamics" / "bars.json").read_bytes() == raw_journal


def test_daemon_market_snapshot_supplies_full_history_without_another_fetch(tmp_path):
    from trader.domain.world_episode import parse_utc_timestamp

    bars = _quarter_hour_bars(count=64, base=100)
    captured = parse_utc_timestamp(bars[-1].ts, "ts") + timedelta(minutes=17)
    dynamics = compose_world_dynamics_runtime(state_dir=tmp_path, clock=Clock(captured))
    runner = WorldModelBackgroundRunner(runtime=MarketRuntime(), dynamics_workflow=dynamics)
    provider = SimpleNamespace(get_bars=lambda *_args, **_kwargs: pytest.fail("unexpected market fetch"))
    scheduled = daemon._trigger_world_model_shadow(
        runner=runner, active_symbols=["2330.TW"], tradable_symbols=["2330.TW"],
        bars_by_symbol={"2330.TW": bars}, data_age_by_symbol={"2330.TW": 120},
        runtime_data_source_by_symbol={"2330.TW": "yfinance"}, data_source=provider,
        runtime_interval="15m", now=captured,
    )
    assert scheduled["triggered"]
    scheduled["_thread"].join(timeout=5)
    summary = runner.status()["dynamics"]
    assert summary["status"] == "ready"
    assert summary["journal"]["retained_bars"] == 64
    assert summary["series"][0]["support"] == 63


def test_explicit_disable_is_visible_and_never_creates_evidence(tmp_path):
    assert compose_world_dynamics_runtime(state_dir=tmp_path, enabled=False) is None
    status = json.loads((tmp_path / "world_dynamics_status.json").read_text())
    assert status["status"] == "disabled" and status["enabled"] is False
    assert not (tmp_path / "world_dynamics").exists()


def test_legacy_capture_failure_still_runs_dynamics_and_stays_isolated(tmp_path):
    inputs = _inputs(64)
    dynamics = compose_world_dynamics_runtime(state_dir=tmp_path, clock=Clock(inputs["now"]))
    runner = WorldModelBackgroundRunner(runtime=MarketRuntime(broken=True), dynamics_workflow=dynamics)
    status = _cycle(runner, inputs)
    assert status["status"] == "partial"
    assert status["errors"][0]["stage"] == "capture"
    assert status["dynamics"]["status"] == "ready"


def test_corrupt_evidence_stops_only_dynamics_and_exposes_failure(tmp_path):
    inputs = _inputs(64)
    dynamics = compose_world_dynamics_runtime(state_dir=tmp_path, clock=Clock(inputs["now"]))
    path = tmp_path / "world_dynamics" / "bars.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":"corrupt"}')
    market = MarketRuntime()
    status = _cycle(WorldModelBackgroundRunner(runtime=market, dynamics_workflow=dynamics), inputs)
    assert market.captured == 1 and status["status"] == "partial"
    assert status["errors"][0]["stage"] == "dynamics"
    saved = json.loads((tmp_path / "world_dynamics_status.json").read_text())
    assert saved["status"] == "unavailable" and saved["reason"] == "maintenance_error"
    assert path.read_text() == '{"schema_version":"corrupt"}'


def test_resource_guard_skips_training_and_publishes_pause(tmp_path):
    inputs = _inputs(64)
    dynamics = compose_world_dynamics_runtime(state_dir=tmp_path, clock=Clock(inputs["now"]))
    guard = SimpleNamespace(evaluate=lambda **_: SimpleNamespace(allowed=False, reason="disk_budget"))
    market = MarketRuntime()
    runner = WorldModelBackgroundRunner(runtime=market, dynamics_workflow=dynamics, resource_guard=guard)
    status = _cycle(runner, inputs)
    assert status["status"] == "skipped" and market.captured == 0
    saved = json.loads((tmp_path / "world_dynamics_status.json").read_text())
    assert saved["status"] == "paused" and saved["reason"] == "world_model_resource_budget"
    assert not (tmp_path / "world_dynamics").exists()


def test_unwritable_status_cannot_break_daemon_composition(tmp_path):
    (tmp_path / "world_dynamics_status.json").mkdir()
    assert compose_world_dynamics_runtime(state_dir=tmp_path) is None


def test_runtime_records_error_then_reraises_for_worker_isolation(tmp_path):
    inputs = _inputs(64)
    dynamics = compose_world_dynamics_runtime(state_dir=tmp_path, clock=Clock(inputs["now"]))
    with patch.object(dynamics.workflow, "run", side_effect=ValueError("invalid receipt")):
        with pytest.raises(ValueError, match="invalid receipt"):
            dynamics.run(**inputs)
    status = json.loads((tmp_path / "world_dynamics_status.json").read_text())
    assert status["error"] == "ValueError:invalid receipt"
