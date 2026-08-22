from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trader.agent.client import Decision
from trader.application.execute.cycle_decision import (
    DecisionExecutionContext,
    DecisionExecutionState,
    execute_one_cycle_decision,
)
from trader.application.record.decision_ledger_rows import (
    build_decision_row,
    decision_row_brain_trace,
)
from trader.application.record.decision_recorder import DecisionRecorder
from trader.application.record.learning_outcomes import (
    outcome_for_row,
    realised_entry_outcome_records,
    score_outcome,
)
from trader.domain.market_data import Bar
from trader.infrastructure.state_db.learnings_store import LearningsStore
from trader.reporting.ledger import decision_ledger
from tests.plan_store_fakes import MemoryTradePlanStore

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _report(decision: dict) -> dict:
    return {
        "ts": "2026-08-21T12:00:00+00:00",
        "dry_run": True,
        "symbols_due": ["SPY"],
        "prices": {"SPY": 100.0},
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
        "stale_market_data": {},
        "model_calls_used": 1,
        "decisions": [decision],
    }


def _llm_decision(**overrides) -> dict:
    payload = {
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.62,
        "rationale": "range sans catalyseur",
        "intent": "HOLD",
        "llm_provider": "grok",
        "llm_model": "grok-4.6",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "task_id": 11214,
        "process_instance_id": "process-1",
        "attempt_id": "attempt-1",
        "mandate_ref": {"mandate_id": "universe-mandate:eu-1", "venue": "EU"},
        "observation_ts": "2026-08-21T11:45:00+00:00",
        "observation_source": "analysis_bar_as_of",
    }
    payload.update(overrides)
    return payload


def _bar(ts: datetime, close: float) -> Bar:
    return Bar(ts=ts.isoformat(), open=close, high=close, low=close, close=close, volume=100.0)


def _bars(start: datetime, *, initial: float = 100.0, final: float = 102.0) -> list[Bar]:
    return [
        _bar(start - timedelta(hours=1), initial),
        _bar(start, initial),
        _bar(start + timedelta(hours=4), (initial + final) / 2),
        _bar(start + timedelta(days=1), final),
    ]


def _cycle_context(**overrides):
    records: list[dict] = overrides.pop("records", [])
    payload = dict(
        now=NOW,
        min_wake_minutes=None,
        max_wake_minutes=None,
        macro_next=None,
        broker=SimpleNamespace(positions=lambda: {}),
        plan_store=MemoryTradePlanStore(),
        gate=SimpleNamespace(),
        sched=None,
        prices={"SPY": 100.0},
        execution_eligibility={},
        tradable_bars_by_symbol={},
        data_age_by_symbol={},
        runtime_data_source_by_sym={"SPY": "yfinance"},
        armed_plan_ids={},
        armed_plan_orders={},
        armed_reference_volatilities={},
        held_symbols=set(),
        cockpit={},
        runtime_interval="15m",
        starting_equity=100_000.0,
        require_hard_stop=False,
        dry_run=True,
        queue_execute_enabled=False,
        execute_ledger=None,
        record_decision=records.append,
        rate_for_symbol=lambda _symbol: 1.0,
        decision_id_for_symbol=lambda symbol: f"decision:{symbol}",
        process_identity_for_symbol=lambda _symbol: {
            "process_instance_id": "process-1",
            "attempt_id": "attempt-1",
            "runtime_run_id": "runtime-1",
        },
    )
    payload.update(overrides)
    return DecisionExecutionContext(**payload), records


class _Learnings:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **kwargs):
        self.rows.append(kwargs)
        return True


def test_build_decision_row_propagates_episode_identity_without_inventing_snapshots() -> None:
    decision = _llm_decision()
    row = build_decision_row(_report(decision), decision, sequence=0)

    assert row["schema_version"] == 1
    assert row["brain_trace"] == {
        "episode_id": "trader-episode:task:11214:process:process-1",
        "task_id": "11214",
        "process_instance_id": "process-1",
        "attempt_id": "attempt-1",
        "decision_id": row["decision_id"],
        "mandate_id": "universe-mandate:eu-1",
        "observation": {
            "status": "available",
            "ts": "2026-08-21T11:45:00+00:00",
            "source": "analysis_bar_as_of",
        },
        "post_effect_snapshot": {"status": "unavailable", "snapshot_id": None},
    }
    assert "queue_task_id" not in row["brain_trace"]


def test_legacy_decision_row_without_brain_trace_stays_readable() -> None:
    old_row = {
        "schema_version": 1,
        "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
        "symbol": "SPY",
        "action": "HOLD",
        "decision": {"symbol": "SPY"},
    }

    assert decision_row_brain_trace(old_row) is None
    assert old_row["schema_version"] == 1
    assert "brain_trace" not in old_row


def test_infra_hold_is_excluded_from_brain_episode_identity() -> None:
    decision = _llm_decision(
        decision_source="infra",
        model_called=False,
        llm_provider=None,
        llm_model=None,
        rationale="stale_market_data",
        reason="stale_market_data",
    )
    row = build_decision_row(_report(decision), decision, sequence=0)

    assert row["decision_source"] == "infra"
    assert row["brain_trace"]["episode_id"] is None
    assert row["brain_trace"]["task_id"] == "11214"
    assert row["brain_trace"]["process_instance_id"] == "process-1"


def test_missing_or_ambiguous_provenance_fails_closed() -> None:
    missing_decision = _llm_decision(task_id=None, observation_ts=None)
    missing = build_decision_row(_report(missing_decision), missing_decision, sequence=0)
    assert missing["brain_trace"]["episode_id"] is None
    assert missing["brain_trace"]["task_id"] is None
    assert missing["brain_trace"]["observation"]["status"] == "unavailable"

    ambiguous = _llm_decision()
    report = _report(ambiguous)
    report["process_instance_id"] = "process-other"
    row = build_decision_row(report, ambiguous, sequence=0)
    assert row["brain_trace"]["episode_id"] is None
    assert row["brain_trace"]["process_instance_id"] is None


def test_cycle_decision_copies_available_task_and_observation_without_inventing() -> None:
    analysis_bar = _bar(NOW - timedelta(minutes=15), 99.5)
    ctx, records = _cycle_context(
        decide_task_id_by_symbol={"SPY": 11214},
        analysis_bars_by_symbol={"SPY": [analysis_bar]},
    )

    execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision(
            symbol="SPY",
            action="HOLD",
            quantity=0.0,
            confidence=0.4,
            rationale="attente",
            intent="HOLD",
            llm_provider="grok",
            llm_model="grok-4.6",
        ),
        state=DecisionExecutionState(snap=SimpleNamespace(equity=100_000.0), gross=0.0),
        ctx=ctx,
    )

    assert records[0]["task_id"] == 11214
    assert records[0]["observation_ts"] == analysis_bar.ts
    assert "post_effect_snapshot_id" not in records[0]


def test_infra_hold_from_cycle_does_not_receive_a_brain_episode() -> None:
    ctx, records = _cycle_context(decide_task_id_by_symbol={"SPY": 11214})
    execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision.hold("SPY", "no_decision_in_batch"),
        state=DecisionExecutionState(snap=SimpleNamespace(equity=100_000.0), gross=0.0),
        ctx=ctx,
    )

    row = build_decision_row(_report(records[0]), records[0], sequence=0)
    assert row["decision_source"] == "infra"
    assert row["brain_trace"]["episode_id"] is None
    assert row["brain_trace"]["task_id"] == "11214"


def test_score_outcome_persists_exact_horizon_and_does_not_guess() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    one_day = score_outcome(
        {"ts": started.isoformat(), "symbol": "SPY", "action": "HOLD"},
        _bars(started, final=101.0),
        now=started + timedelta(days=2),
    )
    assert one_day is not None
    assert one_day["evaluation_basis"] == "counterfactual"
    assert one_day["horizon_used"] == "1d"
    assert one_day["evaluated_at"] == (started + timedelta(days=2)).isoformat()
    assert one_day["source_cycle_id"] is None
    assert one_day["source_cycle_status"] == "unavailable"

    four_hours = score_outcome(
        {"ts": started.isoformat(), "symbol": "SPY", "action": "HOLD"},
        [
            _bar(started - timedelta(hours=1), 100.0),
            _bar(started, 100.0),
            _bar(started + timedelta(hours=4), 101.0),
        ],
        now=started + timedelta(days=2),
    )
    assert four_hours is not None
    assert four_hours["horizon_used"] == "4h"
    assert four_hours["evaluation_basis"] == "counterfactual"


def test_realised_outcome_propagates_unique_cycle_and_fails_closed_when_ambiguous() -> None:
    unique = realised_entry_outcome_records(
        [
            {
                "commission_quality": {"status": "available"},
                "entry_notional_usd": 1000.0,
                "pnl": 50.0,
                "entry_decision_ids": ["decision-1"],
                "position_cycle_id": "SPY:1",
            }
        ]
    )
    assert unique["decision-1"]["net_return"] == 0.05
    assert unique["decision-1"]["source_cycle_id"] == "SPY:1"
    assert unique["decision-1"]["source_cycle_status"] == "available"

    ambiguous = realised_entry_outcome_records(
        [
            {
                "commission_quality": {"status": "available"},
                "entry_notional_usd": 1000.0,
                "pnl": 50.0,
                "entry_decision_ids": ["shared"],
                "position_cycle_id": "SPY:1",
            },
            {
                "commission_quality": {"status": "available"},
                "entry_notional_usd": 1000.0,
                "pnl": 80.0,
                "entry_decision_ids": ["shared"],
                "position_cycle_id": "SPY:2",
            },
        ]
    )
    assert "shared" not in ambiguous

    outcome = outcome_for_row(
        {
            "decision_id": "decision-1",
            "executed": True,
            "intent": "OPEN_LONG",
            "action": "BUY",
        },
        bars=[],
        now=NOW,
        realised_returns=unique,
    )
    assert outcome is not None
    assert outcome["evaluation_basis"] == "realized"
    assert outcome["horizon_used"] is None
    assert outcome["source_cycle_id"] == "SPY:1"
    assert outcome["source_cycle_status"] == "available"
    assert outcome["evaluated_at"] == NOW.isoformat()


def test_decision_recorder_propagates_identity_into_flair_candidate_notes(tmp_path) -> None:
    learnings = _Learnings()
    recorder = DecisionRecorder(
        report={
            "ts": NOW.isoformat(),
            "decisions": [],
            "model_calls_used": 0,
            "prices": {"SPY": 100.0},
            "symbols_due": ["SPY"],
            "portfolio": {"equity": 100000.0},
        },
        dry_run=True,
        symbols_total=1,
        max_model_calls_per_cycle=25,
        learnings_store=learnings,
        decision_ledger_store=decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl"),
        refresh_report_portfolio=lambda: None,
        write_current_report=lambda _payload: None,
        write_status=lambda _phase, **_payload: None,
        append_event=lambda _event, **_payload: None,
        news_snapshot=lambda _symbol, _now: {"coverage": "none"},
        macro_next=None,
        now=NOW,
    )
    recorder.record(
        {
            "symbol": "SPY",
            "action": "BUY",
            "qty": 1.0,
            "confidence": 0.8,
            "rationale": "breakout",
            "opportunity_side": "long",
            "intent": "OPEN_LONG",
            "executed": True,
            "reason": "ok",
            "decision_source": "llm",
            "model_called": True,
            "llm_provider": "grok",
            "llm_model": "grok-4.6",
            "task_id": 11214,
            "process_instance_id": "process-1",
            "attempt_id": "attempt-1",
            "mandate_ref": {"mandate_id": "universe-mandate:eu-1"},
        }
    )

    note = learnings.rows[0]
    assert note["task_id"] == "11214"
    assert note["process_instance_id"] == "process-1"
    assert note["attempt_id"] == "attempt-1"
    assert note["mandate_id"] == "universe-mandate:eu-1"
    assert note["episode_id"] == "trader-episode:task:11214:process:process-1"
    assert note["decision_id"]


def test_learnings_store_persists_trace_identity_and_flair_provenance(tmp_path) -> None:
    store = LearningsStore(tmp_path / "learnings.db")
    jsonl = tmp_path / "notes.jsonl"
    jsonl.write_text(
        '{"ts":"2026-07-01T10:00:00+00:00","symbol":"SPY","note":"breakout",'
        '"action":"BUY","intent":"OPEN_LONG","executed":true,'
        '"decision_id":"decision-1","task_id":"11214","process_instance_id":"process-1",'
        '"attempt_id":"attempt-1","mandate_id":"universe-mandate:eu-1",'
        '"episode_id":"trader-episode:task:11214:process:process-1"}\n',
        encoding="utf-8",
    )
    assert store.ingest_jsonl(jsonl, source="runtime")["inserted"] == 1
    pending = store.pending_outcome_notes()[0]
    assert pending["episode_id"] == "trader-episode:task:11214:process:process-1"
    assert pending["task_id"] == "11214"
    assert pending["mandate_id"] == "universe-mandate:eu-1"

    assert (
        store.update_note_outcomes(
            [
                {
                    "id": pending["id"],
                    "verdict": "WIN",
                    "forward_return": 0.05,
                    "evaluation_basis": "realized",
                    "horizon_used": None,
                    "evaluated_at": "2026-07-10T00:00:00+00:00",
                    "source_cycle_id": "SPY:1",
                }
            ]
        )
        == 1
    )
    scored = store._conn.execute(
        "SELECT evaluation_basis, horizon_used, evaluated_at, source_cycle_id FROM notes"
    ).fetchone()
    assert tuple(scored) == ("realized", None, "2026-07-10T00:00:00+00:00", "SPY:1")
