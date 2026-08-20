import json
from pathlib import Path

from trader.commands import attribution
from trader.reporting.attribution import render_text


def test_attribution_command_utilise_le_state_du_repo() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert attribution.DEFAULT_STATE_DIR == repo_root / "state"


ATTRIBUTION_PAYLOAD = {
    "n_closed_trades": 2,
    "realized_pnl": 12.5,
    "realized_gross_pnl": 14.0,
    "total_commissions": 1.5,
    "commission_quality": {"status": "available"},
    "win_rate": 0.5,
    "avg_pnl": 6.25,
    "avg_holding_minutes": 45.0,
    "by_confidence": [
        {
            "bucket": "0.7-0.85",
            "n": 2,
            "win_rate": 0.5,
            "total_gross_pnl": 14.0,
            "total_pnl": 12.5,
            "commission_quality": {"status": "available"},
        }
    ],
    "by_exit_reason": [
        {
            "reason": "take_profit",
            "n": 1,
            "total_gross_pnl": 21.0,
            "total_pnl": 20.0,
            "commission_quality": {"status": "available"},
        }
    ],
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
    assert "P&L net courtage: 12.50" in output
    assert "Calibration confidence:" in output
    assert "Par raison de sortie:" in output


def test_attribution_text_incomplete_never_reports_net_zero() -> None:
    payload = {
        **ATTRIBUTION_PAYLOAD,
        "realized_pnl": None,
        "total_commissions": None,
        "win_rate": None,
        "avg_pnl": None,
        "commission_quality": {
            "status": "unavailable",
            "counts": {"total": 2, "available": 1, "unavailable": 1},
            "reasons": ["commission_not_modeled"],
        },
        "by_confidence": [],
        "by_exit_reason": [],
    }

    output = render_text(payload)

    assert "P&L brut USD    : 14.00" in output
    assert "P&L net courtage: n/a" in output
    assert "Commissions     : n/a" in output
    assert "1/2 complets" in output
    assert "P&L net courtage: 0.00" not in output
