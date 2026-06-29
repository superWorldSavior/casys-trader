# tests/test_fx_rates.py
import math
from pathlib import Path
import textwrap
import pytest
from trader import fx_rates


def _write_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "fx.yaml"
    p.write_text(textwrap.dedent("""
        TWD:
          yahoo: "TWD=X"
          invert: true
          fallback: 0.031
        EUR:
          yahoo: "EURUSD=X"
          invert: false
          fallback: 1.08
    """))
    return p


def test_usd_always_one(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    rates = fx_rates.rates_for_symbols(["MSFT"], fetcher=lambda s: None, config=cfg)
    assert rates["USD"] == 1.0
    assert set(rates) == {"USD"}, "univers 100% USD → une seule clé attendue"


def test_inverted_pair(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    # TWD=X close = 32 TWD/USD -> rate_usd = 1/32
    rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=lambda s: 32.0, config=cfg)
    assert rates["TWD"] == pytest.approx(1 / 32.0)


def test_direct_pair(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    rates = fx_rates.rates_for_symbols(["ACA.PA"], fetcher=lambda s: 1.08, config=cfg)
    assert rates["EUR"] == pytest.approx(1.08)


def test_fallback_on_fetch_failure(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    def boom(_):
        raise RuntimeError("yfinance down")
    rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=boom, config=cfg)
    assert rates["TWD"] == pytest.approx(0.031)


def test_fallback_on_none(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=lambda s: None, config=cfg)
    assert rates["TWD"] == pytest.approx(0.031)


def test_fallback_on_nan_close(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=lambda s: math.nan, config=cfg)
    assert rates["TWD"] == pytest.approx(0.031)


def test_unconfigured_currency_warns_and_falls_back(tmp_path, caplog):
    """Une devise absente de fx.yaml doit logguer un WARNING et utiliser 1.0 (pas lever)."""
    import logging
    p = tmp_path / "fx_no_twd.yaml"
    p.write_text(textwrap.dedent("""
        EUR:
          yahoo: "EURUSD=X"
          invert: false
          fallback: 1.08
    """))
    cfg = fx_rates.load_fx_config(p)

    # Assure propagation vers root (peut être False si setup_logging() a tourné)
    trader_lg = logging.getLogger("trader")
    orig_propagate = trader_lg.propagate
    trader_lg.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger="trader.fx_rates"):
            rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=lambda s: 32.0, config=cfg)
    finally:
        trader_lg.propagate = orig_propagate

    assert rates["TWD"] == 1.0, "devise non configurée → fallback 1.0"
    assert rates["USD"] == 1.0
    assert any("TWD" in m for m in caplog.messages), "WARNING attendu pour devise non configurée"


def test_per_currency_resilience(tmp_path):
    """Un mix : EUR live OK, TWD fallback statique, GBP non configuré → 1.0 + warning.
    Les trois coexistent ; une devise en erreur ne bloque pas les autres.
    """
    p = tmp_path / "fx_partial.yaml"
    p.write_text(textwrap.dedent("""
        TWD:
          yahoo: "TWD=X"
          invert: true
          fallback: 0.031
        EUR:
          yahoo: "EURUSD=X"
          invert: false
          fallback: 1.08
    """))
    cfg = fx_rates.load_fx_config(p)

    call_log: list[str] = []

    def selective_fetcher(pair: str) -> float | None:
        call_log.append(pair)
        if pair == "EURUSD=X":
            return 1.09   # live OK
        # TWD=X → None → fallback statique
        return None

    # Symbols : EUR (.PA), TWD (.TW), GBP (.L — non configuré)
    rates = fx_rates.rates_for_symbols(
        ["ACA.PA", "2379.TW", "AZN.L"],
        fetcher=selective_fetcher,
        config=cfg,
    )

    assert rates["USD"] == 1.0
    assert rates["EUR"] == pytest.approx(1.09)      # live
    assert rates["TWD"] == pytest.approx(0.031)     # fallback statique
    assert rates["GBP"] == 1.0                      # non configuré → 1.0 avec WARNING


def test_load_fx_config_rejects_missing_keys(tmp_path):
    """Un bloc YAML sans 'yahoo' ou sans 'fallback' doit lever ValueError à la lecture."""
    # Missing 'fallback'
    p_no_fallback = tmp_path / "fx_no_fallback.yaml"
    p_no_fallback.write_text(textwrap.dedent("""
        EUR:
          yahoo: "EURUSD=X"
          invert: false
    """))
    with pytest.raises(ValueError, match="fallback"):
        fx_rates.load_fx_config(p_no_fallback)

    # Missing 'yahoo'
    p_no_yahoo = tmp_path / "fx_no_yahoo.yaml"
    p_no_yahoo.write_text(textwrap.dedent("""
        EUR:
          invert: false
          fallback: 1.08
    """))
    with pytest.raises(ValueError, match="yahoo"):
        fx_rates.load_fx_config(p_no_yahoo)
