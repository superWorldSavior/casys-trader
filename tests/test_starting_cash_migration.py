from pathlib import Path

from trader.reporting import stats as stats_mod


def test_stats_reads_starting_cash_from_portfolio_without_broker(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "universe.yaml").write_text("symbols: [SPY]\n")
    (cfg_dir / "portfolio.yaml").write_text("starting_cash: 333000\n")

    report = stats_mod.compute_live_kpis(state_dir)

    assert report["cash"] == 333000.0
