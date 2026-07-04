import json

from trader.commands import stats


def test_stats_command_prints_json_from_live_kpis(monkeypatch, capsys) -> None:
    captured: dict = {}

    def fake_compute_live_kpis(state_dir):
        captured["state_dir"] = state_dir
        return {
            "equity": 101_000.0,
            "cash": 99_000.0,
            "total_return": 0.01,
            "max_drawdown": 0.0,
            "period_win_rate": None,
            "volatility": None,
            "sharpe": None,
            "num_trades": 2,
            "n_positions": 1,
            "positions": [],
            "model_performance": [],
        }

    monkeypatch.setattr(stats, "compute_live_kpis", fake_compute_live_kpis)

    stats.main(["--json"])

    assert captured == {"state_dir": stats.DEFAULT_STATE_DIR}
    assert json.loads(capsys.readouterr().out) == {
        "equity": 101_000.0,
        "cash": 99_000.0,
        "total_return": 0.01,
        "max_drawdown": 0.0,
        "period_win_rate": None,
        "volatility": None,
        "sharpe": None,
        "num_trades": 2,
        "n_positions": 1,
        "positions": [],
        "model_performance": [],
    }


def test_stats_command_prints_text_from_live_kpis(monkeypatch, capsys) -> None:
    def fake_compute_live_kpis(state_dir):
        return {
            "equity": 101_000.0,
            "cash": 99_000.0,
            "total_return": 0.01,
            "max_drawdown": 0.0,
            "period_win_rate": None,
            "volatility": None,
            "sharpe": None,
            "num_trades": 2,
            "n_positions": 0,
            "positions": [],
            "model_performance": [],
        }

    monkeypatch.setattr(stats, "compute_live_kpis", fake_compute_live_kpis)

    stats.main([])

    output = capsys.readouterr().out
    assert "Live KPIs" in output
    assert "Équité courante : 101000.00" in output
    assert "Trades          : 2" in output
