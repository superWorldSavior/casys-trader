import json

from trader.commands import attribution


ATTRIBUTION_PAYLOAD = {
    "n_closed_trades": 2,
    "realized_pnl": 12.5,
    "realized_gross_pnl": 14.0,
    "total_commissions": 1.5,
    "win_rate": 0.5,
    "avg_pnl": 6.25,
    "avg_holding_minutes": 45.0,
    "by_confidence": [{"bucket": "0.7-0.85", "n": 2, "win_rate": 0.5, "total_pnl": 12.5}],
    "by_exit_reason": [{"reason": "take_profit", "n": 1, "total_pnl": 20.0}],
    "recent_trips": [],
    "regime": {},
}


def test_attribution_command_prints_json_from_report(monkeypatch, capsys) -> None:
    captured: dict = {}

    def fake_compute_attribution(state_dir):
        captured["state_dir"] = state_dir
        return ATTRIBUTION_PAYLOAD

    monkeypatch.setattr(attribution, "compute_attribution", fake_compute_attribution)

    attribution.main(["--json"])

    assert captured == {"state_dir": attribution.DEFAULT_STATE_DIR}
    assert json.loads(capsys.readouterr().out) == ATTRIBUTION_PAYLOAD


def test_attribution_command_prints_text_from_report(monkeypatch, capsys) -> None:
    monkeypatch.setattr(attribution, "compute_attribution", lambda state_dir: ATTRIBUTION_PAYLOAD)

    attribution.main([])

    output = capsys.readouterr().out
    assert "Attribution décision->résultat" in output
    assert "Trades clôturés : 2" in output
    assert "P&L réalisé     : 12.50" in output
    assert "Calibration confidence:" in output
    assert "Par raison de sortie:" in output
