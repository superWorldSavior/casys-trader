# tests/test_fx.py
import math
import pytest
from trader.market import fx


def test_currency_for_taiwan():
    assert fx.currency_for("2379.TW") == "TWD"
    assert fx.currency_for("6488.TWO") == "TWD"


def test_currency_for_europe():
    assert fx.currency_for("ACA.PA") == "EUR"
    assert fx.currency_for("RHM.DE") == "EUR"


def test_currency_for_us_and_default():
    assert fx.currency_for("MSFT") == "USD"
    assert fx.currency_for("EURUSD=X") == "USD"


def test_known_currency_distinguishes_mapped_market_from_usd_default():
    assert fx.known_currency_for("ASML.AS") == "EUR"
    assert fx.mapped_suffix_for("asml.as") == ".AS"
    assert fx.known_currency_for("MSFT") is None
    assert fx.mapped_suffix_for("MSFT") is None


def test_currency_for_exact_match_symbols():
    assert fx.currency_for("^FCHI") == "EUR"
    assert fx.currency_for("^TWII") == "TWD"


def test_currency_for_gbp_suffix():
    assert fx.currency_for("AZN.L") == "GBP"


def test_currency_for_chf_suffix():
    assert fx.currency_for("NESN.SW") == "CHF"


def test_currency_for_dot_t_is_taiwan():
    # Dans ce pool, .T = titres taïwanais (pas Tokyo) — confirmé par Erwan.
    assert fx.currency_for("2330.T") == "TWD"
    # Pas de collision : .TW/.TWO restent TWD aussi.
    assert fx.currency_for("2379.TW") == "TWD"
    assert fx.currency_for("6488.TWO") == "TWD"


def test_to_usd_identity_for_usd():
    assert fx.to_usd(123.45, "USD", 999.0) == 123.45


def test_to_usd_linear():
    assert fx.to_usd(1000.0, "TWD", 0.031) == pytest.approx(31.0)


def test_to_usd_rejects_bad_rate_for_non_usd():
    for bad in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(ValueError):
            fx.to_usd(100.0, "TWD", bad)


def test_currency_for_european_venues():
    assert fx.currency_for("KBC.BR") == "EUR"      # Bruxelles
    assert fx.currency_for("FORTUM.HE") == "EUR"   # Helsinki
    assert fx.currency_for("EDP.LS") == "EUR"      # Lisbonne
    assert fx.currency_for("AMS.MC") == "EUR"      # Madrid
    assert fx.currency_for("EBS.VI") == "EUR"      # Vienne
    assert fx.currency_for("B.CO") == "DKK"        # Copenhague
    assert fx.currency_for("EQNR.OL") == "NOK"     # Oslo
    assert fx.currency_for("A.ST") == "SEK"        # Stockholm


def test_every_mapped_currency_is_configured_in_fx_yaml():
    """Invariant : toute devise produite par currency_for (hors USD) doit avoir
    une entrée dans config/fx.yaml — sinon la clé est omise (fail-closed, plus de 1.0)."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config/fx.yaml").read_text())
    mapped = set(fx.SUFFIX_CCY.values()) | set(fx.SYMBOL_CCY.values())
    missing = {c for c in mapped if c != "USD" and c not in cfg}
    assert not missing, f"devises mappées absentes de fx.yaml: {missing}"
