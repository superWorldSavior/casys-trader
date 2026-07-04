import json
from datetime import datetime, timezone


def test_runtime_state_writer_writes_json_snapshots_and_status(tmp_path) -> None:
    from trader.runtime.state_writer import RuntimeStateWriter

    now = datetime(2026, 7, 4, 8, 30, tzinfo=timezone.utc)
    writer = RuntimeStateWriter(tmp_path, now_fn=lambda: now, pid_fn=lambda: 4242)

    writer.write_json_state("custom.json", {"message": "café"})
    writer.write_current_report({"phase": "live"})
    writer.write_status("deciding", current_symbol="SPY")

    custom_raw = (tmp_path / "custom.json").read_text(encoding="utf-8")
    assert custom_raw == '{\n  "message": "café"\n}'
    assert json.loads(custom_raw) == {"message": "café"}
    assert json.loads((tmp_path / "current_report.json").read_text(encoding="utf-8")) == {"phase": "live"}
    assert json.loads((tmp_path / "daemon_status.json").read_text(encoding="utf-8")) == {
        "ts": now.isoformat(),
        "phase": "deciding",
        "pid": 4242,
        "current_symbol": "SPY",
    }


def test_runtime_state_writer_appends_jsonl_ledgers(tmp_path) -> None:
    from trader.runtime.state_writer import RuntimeStateWriter

    now = datetime(2026, 7, 4, 8, 30, tzinfo=timezone.utc)
    writer = RuntimeStateWriter(tmp_path, now_fn=lambda: now, pid_fn=lambda: 4242)

    writer.append_event("cycle_started", symbol="SPY")
    writer.append_model_performance(symbol="SPY", fx_rate=1.23)
    writer.append_cycle_history(
        {
            "ts": "2026-07-04T08:31:00+00:00",
            "portfolio": {"equity": 101_000.0, "cash": 99_000.0},
            "decisions": [{"executed": True}, {"executed": False}],
            "planned_exits": [{"executed": True}, {"executed": False}],
        }
    )

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    model_perf = [json.loads(line) for line in (tmp_path / "model_performance.jsonl").read_text().splitlines()]
    history = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]

    assert events == [{"ts": now.isoformat(), "event": "cycle_started", "symbol": "SPY"}]
    assert model_perf == [{"symbol": "SPY", "fx_rate": 1.23}]
    assert history == [
        {
            "ts": "2026-07-04T08:31:00+00:00",
            "equity": 101_000.0,
            "cash": 99_000.0,
            "n_decisions": 2,
            "n_executed": 2,
            "n_planned_exits": 2,
        }
    ]


def test_runtime_state_writer_cycle_history_handles_missing_portfolio(tmp_path) -> None:
    from trader.runtime.state_writer import RuntimeStateWriter

    writer = RuntimeStateWriter(tmp_path)

    writer.append_cycle_history({"ts": "t", "decisions": [], "planned_exits": []})

    row = json.loads((tmp_path / "history.jsonl").read_text().splitlines()[0])
    assert row["equity"] is None
    assert row["cash"] is None
