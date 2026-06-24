# tests/test_fx_rates.py
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


def test_unconfigured_currency_raises(tmp_path):
    """Un symbole .TW avec une config sans TWD doit lever ValueError."""
    p = tmp_path / "fx_no_twd.yaml"
    p.write_text(textwrap.dedent("""
        EUR:
          yahoo: "EURUSD=X"
          invert: false
          fallback: 1.08
    """))
    cfg = fx_rates.load_fx_config(p)
    with pytest.raises(ValueError, match="TWD"):
        fx_rates.rates_for_symbols(["2379.TW"], fetcher=lambda s: 32.0, config=cfg)


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
