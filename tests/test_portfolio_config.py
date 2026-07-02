from pathlib import Path

from trader.config.portfolio import load_starting_cash


def test_reads_portfolio_yaml(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "portfolio.yaml").write_text("starting_cash: 250000\n")

    assert load_starting_cash(cfg_dir) == 250000.0


def test_fallback_to_universe_then_default(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()

    assert load_starting_cash(cfg_dir) == 100000.0

    (cfg_dir / "universe.yaml").write_text("starting_cash: 50000\nsymbols: [SPY]\n")

    assert load_starting_cash(cfg_dir) == 50000.0
