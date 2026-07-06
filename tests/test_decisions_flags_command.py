import json

from trader.runtime import cli


def test_decisions_flags_command_prints_json(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(cli.daemon, "STATE_DIR", state_dir)
    row = {
        "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
        "cycle_ts": "2026-06-08T12:15:21+00:00",
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "executed": True,
        "reason": "ok",
        "runtime": {
            "exit_plan_warnings": [
                {"code": "hard_stop_above_max_pct", "field": "max_pct", "distance_pct": 0.10}
            ]
        },
    }
    (state_dir / "decisions.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert cli.main(["decisions", "flags", "--json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out == [
        {
            "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
            "cycle_ts": "2026-06-08T12:15:21+00:00",
            "symbol": "SPY",
            "action": "BUY",
            "intent": "OPEN_LONG",
            "executed": True,
            "reason": "ok",
            "tool": "strategy_entry",
            "outcome": "executed",
            "source": "exit_plan_warnings",
            "code": "hard_stop_above_max_pct",
            "field": "max_pct",
            "distance_pct": 0.10,
        }
    ]


def test_decisions_flags_command_prints_text(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(cli.daemon, "STATE_DIR", state_dir)
    row = {
        "decision_id": "1",
        "cycle_ts": "2026-06-08T12:15:21+00:00",
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "executed": False,
        "reason": "risk:order_value_exceeded",
        "runtime": {
            "tool_calls": [
                {
                    "tool": "strategy_entry",
                    "outcome": "blocked",
                    "detail": {
                        "warnings": [
                            {
                                "code": "order_value_exceeded",
                                "field": "max_order_value",
                                "context": "order_value=12000 max_order_value=10000",
                            }
                        ]
                    },
                }
            ]
        },
    }
    (state_dir / "decisions.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert cli.main(["decisions", "flags"]) == 0

    out = capsys.readouterr().out
    assert "SPY strategy_entry outcome=blocked" in out
    assert "code=order_value_exceeded" in out
    assert "context=order_value=12000 max_order_value=10000" in out
