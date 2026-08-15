"""Tests TDD — LlmGateStore (cadence last_llm_at persistée)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.application.cycle.infra_holds import quiet_gate_decisions
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
from trader.infrastructure.state_db.migrations import LLM_GATE_MIGRATION
from trader.runtime.cycle_process_state import CycleProcessState


NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


class _WakeSource:
    def symbols_with_wake(self) -> set[str]:
        return set()


def _store(tmp_path: Path) -> LlmGateStore:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])
    return LlmGateStore(db)


def _quiet_gate(last_llm_at, *, state_key: str, now: datetime = NOW):
    return quiet_gate_decisions(
        symbols=["SPY", "QQQ"],
        now=now,
        state_key=state_key,
        last_llm_at=last_llm_at,
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, []], ["QQQ", False, []]],
        },
        regime_families={},
        active_families={},
        wake_source=_WakeSource(),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )


def test_record_then_load_all_returns_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state_dir = str(tmp_path / "state")
    at = NOW - timedelta(hours=1)

    store.record(state_dir, "SPY", at)

    loaded = store.load_all()
    assert loaded == {(state_dir, "SPY"): at}


def test_record_upserts_same_symbol_last_timestamp_wins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state_dir = str(tmp_path / "state")
    first = NOW - timedelta(hours=3)
    second = NOW - timedelta(minutes=15)

    store.record(state_dir, "SPY", first)
    store.record(state_dir, "SPY", second)

    loaded = store.load_all()
    assert loaded == {(state_dir, "SPY"): second}


def test_restart_does_not_treat_persisted_symbols_as_never_seen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state_dir = str(tmp_path / "state")
    seen_at = NOW - timedelta(hours=1)
    store.record(state_dir, "SPY", seen_at)
    store.record(state_dir, "QQQ", seen_at)

    restarted = CycleProcessState(last_llm_at=store.load_all())
    after_restart = _quiet_gate(restarted.last_llm_at, state_key=state_dir)

    assert after_restart.kept_symbols == []
    assert after_restart.gated_symbols == ["SPY", "QQQ"]
    assert after_restart.reasons == {}

    cold_start = _quiet_gate(CycleProcessState().last_llm_at, state_key=state_dir)
    assert cold_start.kept_symbols == ["SPY", "QQQ"]
    assert cold_start.reasons == {"SPY": "periodic_review", "QQQ": "periodic_review"}
