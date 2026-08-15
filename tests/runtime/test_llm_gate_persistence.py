"""Câblage daemon : last_llm_at survit à un redémarrage via SQLite."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trader.infrastructure.state_db.connection import open_state_db
from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
from trader.infrastructure.state_db.migrations import LLM_GATE_MIGRATION
from trader.runtime import daemon
from trader.runtime.cycle_process_state import CycleProcessState


def _flat_bars_factory(now_iso: str):
    from trader.market.market_data import Bar

    def factory(symbol, lookback, interval):
        return [
            Bar(ts=now_iso, open=100.0, high=100.1, low=99.9, close=100.0, volume=1000.0)
            for _ in range(4)
        ]

    return factory


def test_run_cycle_persists_last_llm_at_and_hydrate_restores_it(
    monkeypatch, tmp_path, patch_batch, make_data_source, write_runtime_config
) -> None:
    from trader.agent.client import Decision
    from trader.planning.scheduler import Scheduler

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    process_state = CycleProcessState()

    def decide(**kwargs):
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.4,
            rationale="revue",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5",
        )

    patch_batch(decide)

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(_flat_bars_factory(now.isoformat())),
        process_state=process_state,
    )

    key = (str(state_dir), "SPY")
    assert process_state.last_llm_at[key] == now

    db = open_state_db(state_dir / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])
    persisted = LlmGateStore(db).load_all()
    assert persisted[key] == now

    restarted = CycleProcessState()
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    daemon._hydrate_last_llm_at(restarted)
    assert restarted.last_llm_at[key] == now


def test_llm_gate_store_degrades_to_memory_when_backend_is_not_sqlite(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(daemon, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(daemon, "CANONICAL_STATE_BACKEND", "json")

    assert daemon._llm_gate_store() is None
    assert not (tmp_path / "state" / "casys.db").exists()

    process_state = CycleProcessState()
    daemon._hydrate_last_llm_at(process_state)
    assert process_state.last_llm_at == {}


def test_hydrate_last_llm_at_loads_existing_rows(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    now = datetime(2026, 6, 11, 11, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "CANONICAL_STATE_BACKEND", "sqlite")

    store = daemon._llm_gate_store()
    assert store is not None
    store.record(str(state_dir), "QQQ", now)

    process_state = CycleProcessState()
    daemon._hydrate_last_llm_at(process_state)
    assert process_state.last_llm_at[(str(state_dir), "QQQ")] == now
