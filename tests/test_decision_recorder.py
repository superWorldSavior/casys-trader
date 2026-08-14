import json
from datetime import datetime, timezone

import pytest

from trader.reporting.ledger import decision_ledger
from trader.application.record.decision_recorder import DecisionRecorder
from trader.runtime.agent_trace_runtime import build_agent_trace_appender


class FakeLearnings:
    def __init__(self):
        self.rows = []

    def append(self, **kwargs):
        self.rows.append(kwargs)
        return {"appended": True, "note": kwargs["note"]}


class FakeRecallStore:
    def __init__(self):
        self.calls = []

    def record_recall(self, *, decision_id, note_ids):
        self.calls.append({"decision_id": decision_id, "note_ids": note_ids})


def _learning_recorder(tmp_path, *, learnings, learning_ingester=None) -> DecisionRecorder:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    report = {
        "ts": "2026-07-02T10:00:00+00:00",
        "decisions": [],
        "model_calls_used": 0,
        "prices": {"SPY": 100.0},
        "symbols_due": ["SPY"],
        "portfolio": {"equity": 100000.0},
    }
    return DecisionRecorder(
        report=report,
        dry_run=True,
        symbols_total=1,
        max_model_calls_per_cycle=25,
        learnings_store=learnings,
        decision_ledger_store=decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl"),
        refresh_report_portfolio=lambda: None,
        write_current_report=lambda _payload: None,
        write_status=lambda _phase, **_payload: None,
        append_event=lambda _event, **_payload: None,
        news_snapshot=lambda _symbol, _now: {"coverage": "none"},
        macro_next=None,
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        learning_ingester=learning_ingester,
    )


def test_decision_recorder_appends_report_ledger_status_and_event(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    report = {
        "ts": "2026-07-02T10:00:00+00:00",
        "decisions": [],
        "model_calls_used": 0,
        "prices": {"SPY": 100.0},
        "symbols_due": ["SPY"],
        "portfolio": {"equity": 100000.0},
    }
    statuses = []
    events = []

    recorder = DecisionRecorder(
        report=report,
        dry_run=True,
        symbols_total=1,
        max_model_calls_per_cycle=25,
        learnings_store=FakeLearnings(),
        decision_ledger_store=decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl"),
        refresh_report_portfolio=lambda: None,
        write_current_report=lambda payload: (state_dir / "current_report.json").write_text(json.dumps(payload)),
        write_status=lambda phase, **payload: statuses.append({"phase": phase, **payload}),
        append_event=lambda event, **payload: events.append({"event": event, **payload}),
        news_snapshot=lambda symbol, now: {"coverage": "none"},
        macro_next={"event": "cpi"},
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        recall_store=None,
    )

    recorder.record({
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.0,
        "rationale": "quiet_gate",
        "intent": "HOLD",
        "executed": False,
        "reason": "quiet_gate",
    })

    assert report["decisions"][0]["news"]["macro_next"] == {"event": "cpi"}
    assert statuses[-1]["phase"] == "decision_recorded"
    assert events[-1]["event"] == "decision_recorded"
    rows = [json.loads(line) for line in (state_dir / "decisions.jsonl").read_text().splitlines()]
    assert rows[0]["decision_id"] == "2026-07-02T10:00:00+00:00|0|SPY"


def test_decision_recorder_reads_model_calls_used_when_recording(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    report = {
        "ts": "2026-07-02T10:00:00+00:00",
        "decisions": [],
        "model_calls_used": 0,
        "prices": {"SPY": 100.0},
        "symbols_due": ["SPY"],
        "portfolio": {"equity": 100000.0},
    }
    current_model_calls = {"value": 0}
    statuses = []

    recorder = DecisionRecorder(
        report=report,
        dry_run=True,
        symbols_total=1,
        max_model_calls_per_cycle=25,
        learnings_store=FakeLearnings(),
        decision_ledger_store=decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl"),
        refresh_report_portfolio=lambda: None,
        write_current_report=lambda payload: (state_dir / "current_report.json").write_text(json.dumps(payload)),
        write_status=lambda phase, **payload: statuses.append({"phase": phase, **payload}),
        append_event=lambda event, **payload: None,
        news_snapshot=lambda symbol, now: {"coverage": "none"},
        macro_next=None,
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        recall_store=None,
        model_calls_used_getter=lambda: current_model_calls["value"],
    )

    current_model_calls["value"] = 7
    recorder.record({
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.0,
        "rationale": "batch",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
    })

    persisted_report = json.loads((state_dir / "current_report.json").read_text())
    rows = [json.loads(line) for line in (state_dir / "decisions.jsonl").read_text().splitlines()]

    assert report["model_calls_used"] == 7
    assert persisted_report["model_calls_used"] == 7
    assert rows[0]["market_snapshot"]["model_calls_used"] == 7
    assert statuses[-1]["model_calls_used"] == 7


def test_decision_recorder_adds_active_brief_ref_to_news_payload(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    report = {
        "ts": "2026-07-02T10:00:00+00:00",
        "decisions": [],
        "model_calls_used": 0,
        "prices": {"SPY": 100.0},
        "symbols_due": ["SPY"],
        "portfolio": {"equity": 100000.0},
    }
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    brief_ref = {
        "date": "2026-07-02",
        "venue": "US",
        "brief_id": "2026-07-02T09:00:00+00:00|US",
        "as_of": "2026-07-02T09:00:00+00:00",
    }

    recorder = DecisionRecorder(
        report=report,
        dry_run=True,
        symbols_total=1,
        max_model_calls_per_cycle=25,
        learnings_store=FakeLearnings(),
        decision_ledger_store=decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl"),
        refresh_report_portfolio=lambda: None,
        write_current_report=lambda payload: (state_dir / "current_report.json").write_text(json.dumps(payload)),
        write_status=lambda phase, **payload: None,
        append_event=lambda event, **payload: None,
        news_snapshot=lambda symbol, now: {"coverage": "none"},
        macro_next=[],
        now=now,
        brief_ref_provider=lambda symbol, now: brief_ref,
    )

    recorder.record({
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.0,
        "rationale": "wait",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
    })

    rows = [json.loads(line) for line in (state_dir / "decisions.jsonl").read_text().splitlines()]
    assert report["decisions"][0]["news"]["brief_ref"] == brief_ref
    assert rows[0]["news"]["brief_ref"] == brief_ref


def test_decision_recorder_writes_agent_trace_to_separate_file(tmp_path, caplog):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    report = {
        "ts": "2026-07-02T10:00:00+00:00",
        "decisions": [],
        "model_calls_used": 0,
        "prices": {"SPY": 100.0},
        "symbols_due": ["SPY"],
        "portfolio": {"equity": 100000.0},
    }

    recall_store = FakeRecallStore()
    recorder = DecisionRecorder(
        report=report,
        dry_run=True,
        symbols_total=1,
        max_model_calls_per_cycle=25,
        learnings_store=FakeLearnings(),
        decision_ledger_store=decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl"),
        refresh_report_portfolio=lambda: None,
        write_current_report=lambda payload: (state_dir / "current_report.json").write_text(json.dumps(payload)),
        write_status=lambda phase, **payload: None,
        append_event=lambda event, **payload: None,
        news_snapshot=lambda symbol, now: {"coverage": "none"},
        macro_next=None,
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        recall_store=recall_store,
        agent_trace_appender=build_agent_trace_appender(state_dir / "agent_trace.log"),
    )

    recorder.record({
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.0,
        "rationale": "wait",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "llm_provider": "acpx",
        "llm_model": "gpt-5.5",
        "tool_rounds": 0,
        "tool_calls": [
            {
                "id": "SPY:0",
                "tool": "recall_learnings",
                "args": {"symbol": "SPY", "query": "breakout"},
                "outcome": "ok",
                "detail": {"note_ids": [42, 43]},
            }
        ],
        "applied_learning_ids": ["rule-breakout"],
        "next_wake_in_minutes": 30,
        "next_wake_requested": 30,
    })

    trace_lines = (state_dir / "agent_trace.log").read_text(encoding="utf-8").splitlines()
    assert trace_lines[0].startswith(
        "[agent] ts=2026-07-02T10:00:00+00:00 symbol=SPY source=llm "
        "model=acpx:gpt-5.5 action=HOLD intent=HOLD reason=hold "
        "executed=False tools=1 rounds=0"
    )
    assert trace_lines[1].startswith(
        "[agent-learning] ts=2026-07-02T10:00:00+00:00 symbol=SPY "
        'kind=global_rules rule_ids=["rule-breakout"]'
    )
    assert trace_lines[2] == (
        "[agent-tool] ts=2026-07-02T10:00:00+00:00 symbol=SPY id=SPY:0 "
        'tool=recall_learnings outcome=ok args={"query":"breakout","symbol":"SPY"} detail={"note_ids":[42,43]}'
    )
    assert recall_store.calls == [
        {
            "decision_id": "2026-07-02T10:00:00+00:00|0|SPY",
            "note_ids": [42, 43],
        }
    ]
    assert not any("[agent]" in record.getMessage() for record in caplog.records)
    assert not any("[agent-tool]" in record.getMessage() for record in caplog.records)


def test_decision_recorder_captures_real_llm_rationale_without_explicit_learning(tmp_path) -> None:
    learnings = FakeLearnings()
    ingests: list[str] = []
    recorder = _learning_recorder(
        tmp_path,
        learnings=learnings,
        learning_ingester=lambda: ingests.append("ingested"),
    )
    entry = {
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.52,
        "rationale": "Le momentum reste insuffisant pour une entrée propre.",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "llm_error": None,
    }

    recorder.record(entry)

    # D6: HOLD LLM authentique sans opportunity_side ni annotation → pas d'ingest.
    assert learnings.rows == []
    assert "learning_recorded" not in entry
    assert ingests == []


def test_decision_recorder_keeps_record_learning_as_annotation_of_llm_rationale(tmp_path) -> None:
    learnings = FakeLearnings()
    recorder = _learning_recorder(tmp_path, learnings=learnings)
    entry = {
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.52,
        "rationale": "Le momentum reste insuffisant pour une entrée propre.",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "llm_error": None,
        "learning": "Attendre une clôture au-dessus de la résistance.",
    }

    recorder.record(entry)

    assert len(learnings.rows) == 1
    recorded = learnings.rows[0]
    assert recorded["note"] == (
        "Le momentum reste insuffisant pour une entrée propre.\n"
        "[annotation explicite] Attendre une clôture au-dessus de la résistance."
    )
    assert recorded["learning_annotation"] == "Attendre une clôture au-dessus de la résistance."
    assert entry["learning"] == "Attendre une clôture au-dessus de la résistance."


@pytest.mark.parametrize(
    "updates",
    [
        {"decision_source": "infra", "model_called": False},
        {"decision_source": "armed_plan", "model_called": False},
        {"decision_source": "llm", "model_called": False},
        {"decision_source": "infra", "model_called": True},
        {"llm_error": "bad_output"},
        {"rationale": "codex_bad_output: invalid contract"},
        {"rationale": "   "},
    ],
)
def test_decision_recorder_excludes_non_llm_or_synthetic_rationales(tmp_path, updates: dict) -> None:
    learnings = FakeLearnings()
    recorder = _learning_recorder(tmp_path, learnings=learnings)
    entry = {
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.52,
        "rationale": "Le momentum reste insuffisant pour une entrée propre.",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "llm_error": None,
        "learning": "Cette annotation ne doit pas contourner le filtre.",
        **updates,
    }

    recorder.record(entry)

    assert learnings.rows == []


def test_decision_recorder_ingests_hold_llm_with_opportunity_side_long(tmp_path) -> None:
    learnings = FakeLearnings()
    recorder = _learning_recorder(tmp_path, learnings=learnings)
    entry = {
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.52,
        "rationale": "Thèse long refusée : le pullback n'est pas assez profond.",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "llm_error": None,
        "opportunity_side": "long",
    }

    recorder.record(entry)

    assert len(learnings.rows) == 1
    recorded = learnings.rows[0]
    assert recorded["note"] == "Thèse long refusée : le pullback n'est pas assez profond."
    assert recorded["rationale"] == "Thèse long refusée : le pullback n'est pas assez profond."
    assert recorded["decision_id"] == "2026-07-02T10:00:00+00:00|0|SPY"
    assert "learning_annotation" not in recorded
    assert entry["learning_recorded"] == {"appended": True, "note": recorded["note"]}


def test_decision_recorder_ingests_hold_llm_with_explicit_learning(tmp_path) -> None:
    learnings = FakeLearnings()
    recorder = _learning_recorder(tmp_path, learnings=learnings)
    entry = {
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.52,
        "rationale": "Le momentum reste insuffisant pour une entrée propre.",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "decision_source": "llm",
        "model_called": True,
        "llm_error": None,
        "learning": "Attendre une clôture au-dessus de la résistance.",
    }

    recorder.record(entry)

    assert len(learnings.rows) == 1
    assert learnings.rows[0]["note"] == (
        "Le momentum reste insuffisant pour une entrée propre.\n"
        "[annotation explicite] Attendre une clôture au-dessus de la résistance."
    )


def test_decision_recorder_ingests_buy_llm_without_opportunity_side(tmp_path) -> None:
    learnings = FakeLearnings()
    recorder = _learning_recorder(tmp_path, learnings=learnings)
    entry = {
        "symbol": "SPY",
        "action": "BUY",
        "qty": 1.0,
        "confidence": 0.71,
        "rationale": "Cassure propre au-dessus de la résistance.",
        "intent": "OPEN_LONG",
        "executed": True,
        "reason": "breakout",
        "decision_source": "llm",
        "model_called": True,
        "llm_error": None,
    }

    recorder.record(entry)

    assert len(learnings.rows) == 1
    recorded = learnings.rows[0]
    assert recorded["note"] == "Cassure propre au-dessus de la résistance."
    assert recorded["rationale"] == "Cassure propre au-dessus de la résistance."
    assert recorded["action"] == "BUY"
    assert recorded["intent"] == "OPEN_LONG"
    assert "learning_annotation" not in recorded
