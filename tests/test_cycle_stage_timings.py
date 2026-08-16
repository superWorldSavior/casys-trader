"""Le payload ``cycle_completed`` porte ``stage_timings_ms`` sans changer la décision."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from trader.agent.client import Decision
from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler
from trader.runtime import daemon
from trader.runtime.cycle_timings import STAGE_KEYS
from tests.conftest import write_runtime_config as _write_runtime_config


def _run_one_cycle(monkeypatch, tmp_path, patch_batch, make_data_source):
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))
    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )
    events = [
        json.loads(line)
        for line in (state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    completed = [row for row in events if row.get("event") == "cycle_completed"]
    return report, completed


def test_run_cycle_ajoute_stage_timings_ms_au_cycle_completed(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    report, completed = _run_one_cycle(monkeypatch, tmp_path, patch_batch, make_data_source)
    assert report["decisions"][0]["symbol"] == "SPY"
    assert len(completed) == 1
    event = completed[0]
    assert event["decisions_done"] == 1
    assert "model_calls_used" in event
    timings = event["stage_timings_ms"]
    assert set(STAGE_KEYS).issubset(timings)
    assert "total_ms" in timings
    assert all(isinstance(value, int) for value in timings.values())
    assert all(value >= 0 for value in timings.values())


def test_run_cycle_survive_un_echec_de_chronometrage(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    monkeypatch.setattr(
        daemon,
        "try_stage_clock",
        lambda: (_ for _ in ()).throw(RuntimeError("clock")),
    )
    monkeypatch.setattr(
        daemon,
        "attach_stage_timings",
        lambda payload, clock: (_ for _ in ()).throw(RuntimeError("attach")),
    )
    report, completed = _run_one_cycle(monkeypatch, tmp_path, patch_batch, make_data_source)
    assert report["decisions"][0]["action"] == "HOLD"
    assert report["decisions"][0]["symbol"] == "SPY"
    assert len(completed) == 1
    assert completed[0]["decisions_done"] == 1
    assert "stage_timings_ms" not in completed[0]
