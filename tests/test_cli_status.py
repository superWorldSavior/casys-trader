import json

from trader import cli, daemon


def test_cli_status_json_expose_les_fichiers_runtime(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    (state_dir / "daemon_status.json").write_text(
        json.dumps(
            {
                "phase": "deciding_symbol",
                "current_symbol": "SPY",
                "decisions_done": 1,
                "symbols_total": 2,
            }
        )
    )
    (state_dir / "broker.json").write_text(
        json.dumps(
            {
                "cash": 90000.0,
                "positions": {"SPY": {"symbol": "SPY", "quantity": 10.0, "avg_price": 100.0}},
                "fills": [],
            }
        )
    )
    (state_dir / "current_report.json").write_text(json.dumps({"decisions": [{"symbol": "SPY"}]}))

    assert cli.main(["status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["daemon_status"]["phase"] == "deciding_symbol"
    assert payload["broker"]["cash"] == 90000.0
    assert payload["current_report"]["decisions"][0]["symbol"] == "SPY"
