import json
from datetime import datetime, timezone

from trader.reporting.ledger import decision_ledger
from trader.application.record.decision_recorder import DecisionRecorder


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
        recall_store=None,
        agent_trace_path=state_dir / "agent_trace.log",
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
                "tool": "set_next_wake",
                "args": {"minutes": 30},
                "outcome": "ok",
                "detail": {"requested": 30},
            }
        ],
        "next_wake_in_minutes": 30,
        "next_wake_requested": 30,
    })

    trace_lines = (state_dir / "agent_trace.log").read_text(encoding="utf-8").splitlines()
    assert trace_lines[0].startswith(
        "[agent] ts=2026-07-02T10:00:00+00:00 symbol=SPY source=llm "
        "model=acpx:gpt-5.5 action=HOLD intent=HOLD reason=hold "
        "executed=False tools=1 rounds=0"
    )
    assert trace_lines[1] == (
        "[agent-tool] ts=2026-07-02T10:00:00+00:00 symbol=SPY id=SPY:0 "
        'tool=set_next_wake outcome=applied args={"minutes":30} detail={"requested":30}'
    )
    assert not any("[agent]" in record.getMessage() for record in caplog.records)
    assert not any("[agent-tool]" in record.getMessage() for record in caplog.records)
