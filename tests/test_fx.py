# tests/test_fx.py
import math
import pytest
from trader import fx


def test_currency_for_taiwan():
    assert fx.currency_for("2379.TW") == "TWD"
    assert fx.currency_for("6488.TWO") == "TWD"


def test_currency_for_europe():
    assert fx.currency_for("ACA.PA") == "EUR"
    assert fx.currency_for("RHM.DE") == "EUR"


def test_currency_for_us_and_default():
    assert fx.currency_for("MSFT") == "USD"
    assert fx.currency_for("EURUSD=X") == "USD"


def test_currency_for_exact_match_symbols():
    assert fx.currency_for("^FCHI") == "EUR"
    assert fx.currency_for("^TWII") == "TWD"


def test_currency_for_gbp_suffix():
    assert fx.currency_for("AZN.L") == "GBP"


def test_currency_for_chf_suffix():
    assert fx.currency_for("NESN.SW") == "CHF"


def test_currency_for_dot_t_intentionally_usd():
    # .T est intentionnellement non mappé (suffixe ambigu — voir commentaire dans fx.py)
    assert fx.currency_for("AZN.T") == "USD"


def test_to_usd_identity_for_usd():
    assert fx.to_usd(123.45, "USD", 999.0) == 123.45


def test_to_usd_linear():
    assert fx.to_usd(1000.0, "TWD", 0.031) == pytest.approx(31.0)


def test_to_usd_rejects_bad_rate_for_non_usd():
    for bad in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(ValueError):
            fx.to_usd(100.0, "TWD", bad)
