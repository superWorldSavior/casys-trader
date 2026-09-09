"""Replay unique après une vraie revue LLM stale, une fois le runtime frais."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trader.agent.client import Decision
from trader.application.cycle import schedule as cycle_schedule
from trader.application.cycle.infra_holds import quiet_gate_decisions
from trader.domain.planning import relevance_gate
from trader.market.market_data import Bar
from trader.market import market_data as market
from trader.planning.scheduler import Scheduler
from trader.runtime import daemon
from tests.conftest import write_runtime_config


def _daily_bars() -> list[Bar]:
    return [
        Bar(ts=day, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        for day in ("2026-06-08", "2026-06-09", "2026-06-10")
    ]


def _runtime_bars(ts: str, *, n: int = 32) -> list[Bar]:
    return [
        Bar(ts=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        for _ in range(n)
    ]


class _SwitchableSource:
    def __init__(self) -> None:
        self.runtime_ts = ""
        self.stale = True

    def get_bars(self, symbol, lookback, interval):
        del symbol, lookback
        if interval == "1d":
            return _daily_bars()
        if self.stale:
            return _runtime_bars(self.runtime_ts)
        return _runtime_bars(self.runtime_ts)


def test_run_cycle_replays_once_when_stale_llm_review_becomes_fresh_and_open(
    monkeypatch, tmp_path, patch_batch
) -> None:
    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    source = _SwitchableSource()
    source.stale = True
    source.runtime_ts = (now - timedelta(hours=3)).isoformat()
    calls: list[tuple[datetime, str]] = []
    process_state = daemon.CycleProcessState()

    def decide(**kwargs):
        calls.append((kwargs.get("now") or now, kwargs["symbol"]))
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.6,
            rationale="thèse swing en données stale",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5/medium",
        )

    patch_batch(decide)

    stale_report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=process_state,
    )
    assert [symbol for _, symbol in calls] == ["SPY"]
    assert stale_report["decisions"][0]["decision_source"] == "llm"
    key = (str(state_dir), "SPY")
    assert process_state.last_llm_at[key] == now
    assert process_state.last_wake_fingerprints[key].get("stale_review") == "pending"
    first_probe = now + timedelta(minutes=15)
    assert sched.next_wake("SPY") == first_probe
    assert process_state.last_wake_fingerprints[key].get("fresh_probe_wake") == (
        first_probe.isoformat()
    )
    assert cycle_schedule.select_due_symbols(
        ["SPY"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=first_probe - timedelta(seconds=1),
    ) == []
    assert cycle_schedule.select_due_symbols(
        ["SPY"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=first_probe,
    ) == ["SPY"]

    still_stale_at = first_probe
    source.runtime_ts = (still_stale_at - timedelta(hours=3)).isoformat()
    still_stale = daemon.run_cycle(
        dry_run=True,
        now=still_stale_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=process_state,
    )
    assert [symbol for _, symbol in calls] == ["SPY"]
    assert still_stale["decisions"][0]["decision_source"] == "infra"
    assert still_stale["decisions"][0]["model_called"] is False
    second_probe = now + timedelta(minutes=30)
    assert sched.next_wake("SPY") == second_probe
    assert process_state.last_wake_fingerprints[key].get("fresh_probe_wake") == (
        second_probe.isoformat()
    )
    assert cycle_schedule.select_due_symbols(
        ["SPY"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=second_probe - timedelta(seconds=1),
    ) == []

    fresh_at = second_probe
    source.stale = False
    source.runtime_ts = fresh_at.isoformat()
    fresh_report = daemon.run_cycle(
        dry_run=True,
        now=fresh_at,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=process_state,
    )
    assert [symbol for _, symbol in calls] == ["SPY", "SPY"]
    assert fresh_report["decisions"][0]["decision_source"] == "llm"
    assert process_state.last_wake_fingerprints[key].get("stale_review") is None
    assert process_state.last_wake_fingerprints[key].get("fresh_probe_wake") is None
    assert cycle_schedule.select_due_symbols(
        ["SPY"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=fresh_at + timedelta(minutes=5),
    ) == []

    next_poll = daemon.run_cycle(
        dry_run=True,
        now=fresh_at + timedelta(minutes=5),
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=process_state,
    )
    assert [symbol for _, symbol in calls] == ["SPY", "SPY"]
    assert next_poll["decisions"][0]["decision_source"] == "infra"
    assert next_poll["decisions"][0]["model_called"] is False


def test_initial_sqlite_replay_arm_commits_marker_and_wake_together(
    monkeypatch,
    tmp_path,
    patch_batch,
) -> None:
    from trader.infrastructure.state_db.connection import (
        close_all_state_dbs,
        open_state_db,
    )
    from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
    from trader.infrastructure.state_db.migrations import (
        LLM_GATE_MIGRATIONS,
        SCHEDULER_MIGRATION,
    )
    from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    source = _SwitchableSource()
    source.runtime_ts = (now - timedelta(hours=3)).isoformat()
    db = open_state_db(state_dir / "casys.db")
    db.apply_migrations([SCHEDULER_MIGRATION, *LLM_GATE_MIGRATIONS])
    sched = SqliteScheduler(db)
    atomic_calls: list[str] = []
    original_atomic_arm = LlmGateStore.record_with_fresh_probe_wake

    def traced_atomic_arm(self, state_key, symbol, at, **kwargs):
        atomic_calls.append(symbol)
        return original_atomic_arm(self, state_key, symbol, at, **kwargs)

    monkeypatch.setattr(
        LlmGateStore,
        "record_with_fresh_probe_wake",
        traced_atomic_arm,
    )

    def decide(**kwargs):
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.6,
            rationale="thèse swing analysable avant retour des données fraîches",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5/medium",
        )

    patch_batch(decide)
    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=daemon.CycleProcessState(),
    )

    expected_probe = now + timedelta(minutes=15)
    assert atomic_calls == ["SPY"]
    assert sched.next_wake("SPY") == expected_probe
    close_all_state_dbs()

    restarted_db = open_state_db(state_dir / "casys.db")
    snapshot = LlmGateStore(restarted_db).load_state()
    key = (str(state_dir), "SPY")
    assert snapshot.last_wake_fingerprints[key] == {
        "stale_review": "pending",
        "fresh_probe_wake": expected_probe.isoformat(),
    }
    assert SqliteScheduler(restarted_db).next_wake("SPY") == expected_probe
    close_all_state_dbs()


def test_run_cycle_does_not_replay_while_session_closed(
    monkeypatch, tmp_path, patch_batch
) -> None:
    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    weekday = datetime(2026, 6, 11, 19, 45, tzinfo=timezone.utc)
    source = _SwitchableSource()
    source.stale = True
    source.runtime_ts = (weekday - timedelta(hours=3)).isoformat()
    calls: list[str] = []
    process_state = daemon.CycleProcessState()

    def decide(**kwargs):
        calls.append(kwargs["symbol"])
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.6,
            rationale="analyse hors séance",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5/medium",
        )

    patch_batch(decide)
    daemon.run_cycle(
        dry_run=True,
        now=weekday,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=process_state,
    )
    assert calls == ["SPY"]

    after_hours = datetime(2026, 6, 11, 20, 15, tzinfo=timezone.utc)
    source.stale = False
    source.runtime_ts = after_hours.isoformat()
    closed = daemon.run_cycle(
        dry_run=True,
        now=after_hours,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=process_state,
    )
    assert calls == ["SPY"]
    assert closed["decisions"][0]["model_called"] is False
    assert closed["decisions"][0]["decision_source"] == "infra"
    assert sched.next_wake("SPY") == market.next_regular_session_open(
        after_hours,
        symbol="SPY",
    )


def test_fresh_probe_helper_uses_15m_open_and_next_open_when_closed(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    open_now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)

    assert daemon._arm_fresh_probe(
        sched,
        symbol="SPY",
        now=open_now,
    ) == (open_now + timedelta(minutes=15)).isoformat()

    closed_now = datetime(2026, 6, 11, 20, 15, tzinfo=timezone.utc)
    assert daemon._arm_fresh_probe(
        sched,
        symbol="SPY",
        now=closed_now,
    ) == market.next_regular_session_open(
        closed_now,
        symbol="SPY",
    ).isoformat()


def test_hot_review_reconcile_replaces_legacy_three_hour_wake(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    last_review = now - timedelta(minutes=20)
    owned_wake = (now + timedelta(hours=3)).isoformat()
    sched.set_symbol_next_wake("SPY", owned_wake)
    process_state = daemon.CycleProcessState()
    key = (str(tmp_path / "state"), "SPY")
    process_state.last_llm_at[key] = last_review
    process_state.last_wake_fingerprints[key] = {
        relevance_gate.HOT_REVIEW_WAKE_FINGERPRINT_KEY: owned_wake,
    }

    reconciled = daemon._reconcile_hot_wakes_before_due(
        sched,
        symbols=["SPY"],
        now=now,
        process_state=process_state,
        state_key=str(tmp_path / "state"),
        held_symbols=set(),
        hot_setup_symbols={"SPY"},
    )

    expected = last_review + timedelta(hours=1)
    assert reconciled == {"SPY": expected.isoformat()}
    assert sched.next_wake("SPY") == expected
    assert cycle_schedule.select_due_symbols(
        ["SPY"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=expected - timedelta(seconds=1),
    ) == []
    assert cycle_schedule.select_due_symbols(
        ["SPY"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=expected,
    ) == ["SPY"]
    assert process_state.last_wake_fingerprints[key]["hot_review_wake"] == (
        expected.isoformat()
    )


def test_pre_due_reconcile_clamps_calm_24h_wake_to_last_review_plus_4h(
    tmp_path,
) -> None:
    state_key = str(tmp_path / "state")
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    last_review = now - timedelta(minutes=30)
    owned_wake = (now + timedelta(hours=24)).isoformat()
    sched.set_symbol_next_wake("SPY", owned_wake)
    process_state = daemon.CycleProcessState()
    process_state.last_llm_at[(state_key, "SPY")] = last_review
    process_state.last_wake_fingerprints[(state_key, "SPY")] = {
        relevance_gate.HOT_REVIEW_WAKE_FINGERPRINT_KEY: owned_wake,
    }

    reconciled = daemon._reconcile_hot_wakes_before_due(
        sched,
        symbols=["SPY"],
        now=now,
        process_state=process_state,
        state_key=state_key,
        held_symbols=set(),
        hot_setup_symbols=set(),
    )

    expected = last_review + timedelta(hours=4)
    assert reconciled == {"SPY": expected.isoformat()}
    assert sched.next_wake("SPY") == expected


def test_mechanical_hot_wake_is_typed_and_not_an_agent_wake_after_close(tmp_path) -> None:
    state_key = str(tmp_path / "state")
    sched = Scheduler(tmp_path / "scheduler.json")
    last_review = datetime(2026, 6, 11, 19, 30, tzinfo=timezone.utc)
    wake_at = last_review + timedelta(hours=1)
    sched.set_symbol_next_wake("SPY", wake_at.isoformat())
    process_state = daemon.CycleProcessState()
    key = (state_key, "SPY")
    process_state.last_llm_at[key] = last_review

    daemon._persist_hot_schedule_markers(
        report={
            "decisions": [
                {
                    "symbol": "SPY",
                    "decision_source": "armed_plan",
                    "model_called": False,
                    "schedule_wake_source": "hot_review",
                    "schedule_effect": {"next_wake": wake_at.isoformat()},
                }
            ]
        },
        sched=sched,
        process_state=process_state,
        state_key=state_key,
        llm_gate_store=None,
    )

    assert process_state.last_wake_fingerprints[key]["hot_review_wake"] == (
        wake_at.isoformat()
    )
    gated = quiet_gate_decisions(
        symbols=["SPY"],
        now=wake_at,
        state_key=state_key,
        last_llm_at=process_state.last_llm_at,
        cockpit={},
        regime_families={},
        active_families={},
        wake_source=sched,
        triggers_by_symbol={},
        held_symbols={"SPY"},
        runtime_data_source_by_sym={"SPY": "yfinance"},
        last_wake_fingerprints=process_state.last_wake_fingerprints,
        execution_eligibility={
            "SPY": {"execution": {"enabled": False, "reason": "session_closed"}}
        },
    )

    assert gated.kept_symbols == []
    assert gated.gated_symbols == ["SPY"]


def test_pre_due_reconcile_preserves_future_stale_backoff_despite_old_last_llm(
    tmp_path,
) -> None:
    """A stale-data backoff is not cadence-owned; old last_llm_at must not pull it to now."""
    state_key = str(tmp_path / "state")
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)
    backoff_at = now + timedelta(minutes=45)
    sched.set_symbol_next_wake("AIR.PA", backoff_at.isoformat())
    process_state = daemon.CycleProcessState()
    key = (state_key, "AIR.PA")
    process_state.last_llm_at[key] = now - timedelta(hours=8)
    process_state.last_wake_fingerprints[key] = {
        relevance_gate.HOT_REVIEW_WAKE_FINGERPRINT_KEY: (
            now - timedelta(hours=4)
        ).isoformat(),
    }

    reconciled = daemon._reconcile_hot_wakes_before_due(
        sched,
        symbols=["AIR.PA"],
        now=now,
        process_state=process_state,
        state_key=state_key,
        held_symbols=set(),
        hot_setup_symbols=set(),
    )

    assert reconciled == {}
    assert sched.next_wake("AIR.PA") == backoff_at
    assert cycle_schedule.select_due_symbols(
        ["AIR.PA"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=now,
    ) == []
    assert cycle_schedule.select_due_symbols(
        ["AIR.PA"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=backoff_at,
    ) == ["AIR.PA"]


def test_pre_due_reconcile_defers_owned_closed_session_cadence_to_session_open(
    tmp_path,
) -> None:
    """Internal cadence alone must not keep a closed European symbol due overnight."""
    state_key = str(tmp_path / "state")
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)
    owned_wake = now.isoformat()
    sched.set_symbol_next_wake("AIR.PA", owned_wake)
    process_state = daemon.CycleProcessState()
    key = (state_key, "AIR.PA")
    process_state.last_llm_at[key] = now - timedelta(hours=6)
    process_state.last_wake_fingerprints[key] = {
        relevance_gate.HOT_REVIEW_WAKE_FINGERPRINT_KEY: owned_wake,
    }

    reconciled = daemon._reconcile_hot_wakes_before_due(
        sched,
        symbols=["AIR.PA"],
        now=now,
        process_state=process_state,
        state_key=state_key,
        held_symbols=set(),
        hot_setup_symbols=set(),
    )

    expected = market.next_regular_session_open(now, symbol="AIR.PA")
    assert market.session_snapshot("AIR.PA", now=now).get("open") is False
    assert reconciled == {"AIR.PA": expected.isoformat()}
    assert sched.next_wake("AIR.PA") == expected
    assert process_state.last_wake_fingerprints[key][
        relevance_gate.HOT_REVIEW_WAKE_FINGERPRINT_KEY
    ] == expected.isoformat()
    assert cycle_schedule.select_due_symbols(
        ["AIR.PA"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=now,
    ) == []
    assert cycle_schedule.select_due_symbols(
        ["AIR.PA"],
        sched=sched,
        once=False,
        bootstrap=False,
        now=expected,
    ) == ["AIR.PA"]


def test_pre_due_reconcile_preserves_agent_session_open_wake(tmp_path) -> None:
    state_key = str(tmp_path / "state")
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc)
    session_open_at = market.next_regular_session_open(now, symbol="AIR.PA")
    sched.set_symbol_next_wake("AIR.PA", session_open_at.isoformat())
    process_state = daemon.CycleProcessState()
    key = (state_key, "AIR.PA")
    process_state.last_llm_at[key] = now - timedelta(hours=6)

    reconciled = daemon._reconcile_hot_wakes_before_due(
        sched,
        symbols=["AIR.PA"],
        now=now,
        process_state=process_state,
        state_key=state_key,
        held_symbols=set(),
        hot_setup_symbols=set(),
    )

    assert reconciled == {}
    assert sched.next_wake("AIR.PA") == session_open_at
    assert relevance_gate.HOT_REVIEW_WAKE_FINGERPRINT_KEY not in (
        process_state.last_wake_fingerprints.get(key) or {}
    )


def test_pre_due_hot_reconcile_never_steals_pending_fresh_probe(tmp_path) -> None:
    state_key = str(tmp_path / "state")
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 11, 15, 30, tzinfo=timezone.utc)
    probe_at = now + timedelta(minutes=15)
    sched.set_symbol_next_wake("SPY", probe_at.isoformat())
    process_state = daemon.CycleProcessState()
    key = (state_key, "SPY")
    process_state.last_llm_at[key] = now - timedelta(hours=2)
    process_state.last_wake_fingerprints[key] = {
        "stale_review": "pending",
        "fresh_probe_wake": probe_at.isoformat(),
    }

    reconciled = daemon._reconcile_hot_wakes_before_due(
        sched,
        symbols=["SPY"],
        now=now,
        process_state=process_state,
        state_key=state_key,
        held_symbols={"SPY"},
        hot_setup_symbols=set(),
    )

    assert reconciled == {}
    assert sched.next_wake("SPY") == probe_at


def test_queue_deferred_fresh_replay_is_rearmed_instead_of_looping(
    monkeypatch,
    tmp_path,
) -> None:
    write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    now = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_symbol_next_wake("SPY", now.isoformat())
    process_state = daemon.CycleProcessState()
    key = (str(state_dir), "SPY")
    process_state.last_llm_at[key] = now - timedelta(minutes=15)
    process_state.last_wake_fingerprints[key] = {
        "stale_review": "pending",
        "fresh_probe_wake": now.isoformat(),
    }
    source = _SwitchableSource()
    source.stale = False
    source.runtime_ts = now.isoformat()

    def dispatch_decisions(request, **_kwargs):
        return daemon.decision_dispatch_runtime.DecisionDispatchResult(
            decisions_by_symbol={},
            model_calls_used=0,
            undecided_symbols={"SPY"},
            streamed_decision_symbols=set(),
            execution_state=request.execution_state,
        )

    monkeypatch.setattr(
        daemon.decision_dispatch_runtime,
        "dispatch_decisions",
        dispatch_decisions,
    )

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=source,
        process_state=process_state,
        queue_decide_enabled=True,
        task_ledger=object(),
    )

    next_probe = now + timedelta(minutes=15)
    assert report["decisions"] == []
    assert sched.next_wake("SPY") == next_probe
    assert process_state.last_wake_fingerprints[key]["fresh_probe_wake"] == (
        next_probe.isoformat()
    )
