"""Tests TDD — LlmGateStore (revue et contexte persistés)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.application.cycle.infra_holds import quiet_gate_decisions
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
from trader.infrastructure.state_db.migrations import (
    LLM_GATE_MIGRATIONS,
    SCHEDULER_MIGRATION,
)
from trader.infrastructure.state_db.scheduler_store import SqliteScheduler
from trader.runtime.cycle_process_state import CycleProcessState


NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


class _WakeSource:
    def symbols_with_wake(self) -> set[str]:
        return set()


def _store(tmp_path: Path) -> LlmGateStore:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations(list(LLM_GATE_MIGRATIONS))
    return LlmGateStore(db)


def _quiet_gate(
    last_llm_at,
    *,
    state_key: str,
    now: datetime = NOW,
    held_symbols: set[str] | None = None,
    spy_sig: list[str] | None = None,
    last_wake_reasons=None,
    last_wake_fingerprints=None,
):
    return quiet_gate_decisions(
        symbols=["SPY", "QQQ"],
        now=now,
        state_key=state_key,
        last_llm_at=last_llm_at,
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, spy_sig or []], ["QQQ", False, []]],
        },
        regime_families={},
        active_families={},
        wake_source=_WakeSource(),
        triggers_by_symbol={},
        held_symbols=held_symbols or set(),
        runtime_data_source_by_sym={},
        last_wake_reasons=last_wake_reasons,
        last_wake_fingerprints=last_wake_fingerprints,
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


def test_record_atomically_round_trips_timestamp_reasons_and_fingerprints(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state_dir = str(tmp_path / "state")
    key = (state_dir, "SPY")
    at = NOW - timedelta(minutes=15)

    store.record(
        state_dir,
        "SPY",
        at,
        wake_reasons=("regime", "signal"),
        wake_fingerprints={
            "regime": "regime:us:up",
            "signal": "signal:1h:breakout_up",
        },
    )

    snapshot = store.load_state()
    assert snapshot.last_llm_at[key] == at
    assert snapshot.last_wake_reasons[key] == ("regime", "signal")
    assert snapshot.last_wake_fingerprints[key] == {
        "regime": "regime:us:up",
        "signal": "signal:1h:breakout_up",
    }


def test_record_with_fresh_probe_wake_survives_restart_as_one_lease(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "casys.db"
    state_dir = str(tmp_path)
    candidate = NOW + timedelta(minutes=15)
    db = StateDb(db_path)
    db.apply_migrations([SCHEDULER_MIGRATION, *LLM_GATE_MIGRATIONS])

    marker, fingerprints = LlmGateStore(db).record_with_fresh_probe_wake(
        state_dir,
        "SPY",
        NOW,
        now=NOW,
        candidate_wake=candidate,
        wake_fingerprints={"stale_review": "pending"},
    )

    assert marker == candidate.isoformat()
    assert fingerprints == {
        "stale_review": "pending",
        "fresh_probe_wake": candidate.isoformat(),
    }
    db.close()

    restarted_db = StateDb(db_path)
    restarted = LlmGateStore(restarted_db).load_state()
    assert restarted.last_wake_fingerprints[(state_dir, "SPY")] == fingerprints
    assert SqliteScheduler(restarted_db).next_wake("SPY") == candidate
    restarted_db.close()


def test_record_with_fresh_probe_wake_preserves_nearer_unowned_wake(
    tmp_path: Path,
) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([SCHEDULER_MIGRATION, *LLM_GATE_MIGRATIONS])
    scheduler = SqliteScheduler(db)
    nearer = NOW + timedelta(minutes=5)
    scheduler.set_symbol_next_wake("SPY", nearer.isoformat())

    marker, fingerprints = LlmGateStore(db).record_with_fresh_probe_wake(
        str(tmp_path),
        "SPY",
        NOW,
        now=NOW,
        candidate_wake=NOW + timedelta(minutes=15),
        wake_fingerprints={"stale_review": "pending"},
    )

    assert marker is None
    assert fingerprints == {"stale_review": "pending"}
    assert scheduler.next_wake("SPY") == nearer


def test_record_with_fresh_probe_wake_rolls_back_marker_on_wake_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "casys.db"
    state_dir = str(tmp_path)
    db = StateDb(db_path)
    db.apply_migrations([SCHEDULER_MIGRATION, *LLM_GATE_MIGRATIONS])
    with db.transaction() as cur:
        cur.execute(
            "CREATE TRIGGER fail_probe_wake BEFORE INSERT ON scheduler_symbol_wake "
            "BEGIN SELECT RAISE(ABORT, 'simulated_crash_boundary'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="simulated_crash_boundary"):
        LlmGateStore(db).record_with_fresh_probe_wake(
            state_dir,
            "SPY",
            NOW,
            now=NOW,
            candidate_wake=NOW + timedelta(minutes=15),
            wake_fingerprints={"stale_review": "pending"},
        )
    db.close()

    restarted_db = StateDb(db_path)
    assert LlmGateStore(restarted_db).load_state().last_wake_fingerprints == {}
    assert SqliteScheduler(restarted_db).has_symbol_wake("SPY") is False
    restarted_db.close()


def test_corrupt_context_drops_atomic_review_state_and_fails_open(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations(list(LLM_GATE_MIGRATIONS))
    store = LlmGateStore(db)
    state_dir = str(tmp_path / "state")
    key = (state_dir, "SPY")
    store.record(
        state_dir,
        "SPY",
        NOW,
        wake_reasons=("signal",),
        wake_fingerprints={"signal": "signal:1h:breakout_up"},
    )
    with db.transaction() as cur:
        cur.execute(
            "UPDATE llm_gate_last_seen SET wake_fingerprints_json=? WHERE state_dir=? AND symbol=?",
            ("[]", state_dir, "SPY"),
        )

    snapshot = store.load_state()

    assert key not in snapshot.last_llm_at
    assert key not in snapshot.last_wake_reasons
    assert key not in snapshot.last_wake_fingerprints


def test_legacy_row_without_context_forces_position_review(tmp_path: Path) -> None:
    from trader.infrastructure.state_db.migrations import (
        LLM_GATE_CONTEXT_MIGRATION,
        LLM_GATE_MIGRATION,
    )

    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])
    state_dir = str(tmp_path / "state")
    key = (state_dir, "SPY")
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO llm_gate_last_seen(state_dir, symbol, last_at) VALUES (?, ?, ?)",
            (state_dir, "SPY", NOW.isoformat()),
        )
    db.apply_migrations([LLM_GATE_CONTEXT_MIGRATION])

    snapshot = LlmGateStore(db).load_state()
    restarted = CycleProcessState(
        last_llm_at=snapshot.last_llm_at,
        last_wake_reasons=snapshot.last_wake_reasons,
        last_wake_fingerprints=snapshot.last_wake_fingerprints,
    )
    review = _quiet_gate(
        restarted.last_llm_at,
        state_key=state_dir,
        held_symbols={"SPY"},
        last_wake_reasons=restarted.last_wake_reasons,
        last_wake_fingerprints=restarted.last_wake_fingerprints,
    )

    assert key not in restarted.last_llm_at
    assert review.reasons["SPY"] == "position"


def test_restarts_preserve_active_absent_same_signal_transitions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state_dir = str(tmp_path / "state")
    key = (state_dir, "SPY")
    signal_fingerprint = "signal:1h:breakout_up"
    store.record(
        state_dir,
        "SPY",
        NOW - timedelta(minutes=30),
        wake_reasons=("signal",),
        wake_fingerprints={"signal": signal_fingerprint},
    )

    active_restart = store.load_state()
    active_state = CycleProcessState(
        last_llm_at=active_restart.last_llm_at,
        last_wake_reasons=active_restart.last_wake_reasons,
        last_wake_fingerprints=active_restart.last_wake_fingerprints,
    )
    absent = _quiet_gate(
        active_state.last_llm_at,
        state_key=state_dir,
        held_symbols={"SPY"},
        last_wake_reasons=active_state.last_wake_reasons,
        last_wake_fingerprints=active_state.last_wake_fingerprints,
    )
    assert absent.reasons["SPY"] == "signal"

    store.record(state_dir, "SPY", NOW, wake_reasons=(), wake_fingerprints={})
    absent_restart = store.load_state()
    absent_state = CycleProcessState(
        last_llm_at=absent_restart.last_llm_at,
        last_wake_reasons=absent_restart.last_wake_reasons,
        last_wake_fingerprints=absent_restart.last_wake_fingerprints,
    )
    same_again = _quiet_gate(
        absent_state.last_llm_at,
        state_key=state_dir,
        now=NOW + timedelta(minutes=30),
        held_symbols={"SPY"},
        spy_sig=["1h:breakout_up"],
        last_wake_reasons=absent_state.last_wake_reasons,
        last_wake_fingerprints=absent_state.last_wake_fingerprints,
    )

    assert same_again.reasons["SPY"] == "signal"
    assert absent_state.last_wake_reasons[key] == ()
    assert absent_state.last_wake_fingerprints[key] == {}


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


def test_persisted_review_debounces_open_position_across_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state_dir = str(tmp_path / "state")
    store.record(state_dir, "SPY", NOW - timedelta(minutes=30))

    restarted = CycleProcessState(last_llm_at=store.load_all())
    recent = _quiet_gate(
        restarted.last_llm_at,
        state_key=state_dir,
        held_symbols={"SPY"},
    )
    due = _quiet_gate(
        restarted.last_llm_at,
        state_key=state_dir,
        now=NOW + timedelta(minutes=30),
        held_symbols={"SPY"},
    )

    assert recent.gated_symbols == ["SPY"]
    assert recent.entries[0]["model_called"] is False
    assert due.kept_symbols == ["SPY", "QQQ"]
    assert due.reasons["SPY"] == "position"
