import json
from datetime import datetime, timezone

from trader.reporting.ledger import decision_ledger
from trader.application.decision_recorder import DecisionRecorder


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
