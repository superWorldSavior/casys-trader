"""Commission venue classification and first-bracket pricing invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trader.domain.contracts import Order
from trader.domain.market import fx
from trader.infrastructure.brokers.commission_models import IbkrCommissionModel


def _pool_symbols() -> tuple[str, ...]:
    root = Path(__file__).resolve().parents[1]
    payload = yaml.safe_load(
        (root / "config" / "pool.yaml").read_text(encoding="utf-8")
    )
    return tuple(str(symbol) for symbol in payload["symbols"])


def _one_symbol_per_configured_suffix() -> tuple[tuple[str, str], ...]:
    sample_by_suffix: dict[str, str] = {}
    for symbol in _pool_symbols():
        suffix = fx.mapped_suffix_for(symbol)
        if suffix is not None:
            sample_by_suffix.setdefault(suffix, symbol)
    return tuple(sorted(sample_by_suffix.items()))


@pytest.mark.parametrize(
    ("suffix", "symbol"),
    _one_symbol_per_configured_suffix(),
)
def test_every_configured_non_us_suffix_has_native_non_us_pricing(
    suffix: str,
    symbol: str,
) -> None:
    commission = IbkrCommissionModel().calculate(
        Order(symbol=symbol, side="BUY", quantity=100.0),
        price=100.0,
    )

    assert commission.currency == fx.SUFFIX_CCY[suffix]
    assert commission.model not in {
        "ibkr_us_stock_tiered",
        "ibkr_unknown",
        "ibkr_unpriced_venue",
    }
    assert commission.amount > 0.0


def test_pool_suffix_coverage_is_exhaustive_and_uses_canonical_fx_mapping() -> None:
    configured_suffixes = {
        suffix for suffix, _symbol in _one_symbol_per_configured_suffix()
    }

    assert configured_suffixes == {
        ".AS",
        ".BR",
        ".CO",
        ".DE",
        ".HE",
        ".L",
        ".LS",
        ".MC",
        ".MI",
        ".OL",
        ".PA",
        ".ST",
        ".SW",
        ".TW",
        ".TWO",
        ".VI",
    }
    assert configured_suffixes <= set(fx.SUFFIX_CCY)


@pytest.mark.parametrize(
    ("symbol", "currency", "minimum", "model"),
    [
        ("ASML.AS", "EUR", 1.25, "ibkr_europe_stock_tiered"),
        ("SAN.MC", "EUR", 3.0, "ibkr_spain_stock_fixed_smartrouting"),
        ("AZN.L", "GBP", 1.0, "ibkr_uk_stock_tiered"),
        ("RO.SW", "CHF", 1.5, "ibkr_switzerland_stock_tiered"),
        ("NOVO-B.CO", "DKK", 10.0, "ibkr_nordic_stock_tiered"),
        ("EQNR.OL", "NOK", 10.0, "ibkr_nordic_stock_tiered"),
        ("ERIC-B.ST", "SEK", 10.0, "ibkr_nordic_stock_tiered"),
        ("2330.TW", "TWD", 80.0, "ibkr_taiwan_stock_tiered"),
        ("6488.TWO", "TWD", 80.0, "ibkr_taiwan_stock_tiered"),
        ("2330.T", "TWD", 80.0, "ibkr_taiwan_stock_tiered"),
    ],
)
def test_market_tariff_minimums_are_charged_in_native_currency(
    symbol: str,
    currency: str,
    minimum: float,
    model: str,
) -> None:
    commission = IbkrCommissionModel().calculate(
        Order(symbol=symbol, side="BUY", quantity=1.0),
        price=1.0,
    )

    assert commission.amount == minimum
    assert commission.currency == currency
    assert commission.model == model


def test_european_tiered_commission_caps_are_native() -> None:
    model = IbkrCommissionModel()

    eur = model.calculate(Order("ASML.AS", "BUY", 10_000.0), 1_000.0)
    chf = model.calculate(Order("RO.SW", "BUY", 10_000.0), 1_000.0)

    assert (eur.amount, eur.currency) == (29.0, "EUR")
    assert (chf.amount, chf.currency) == (49.0, "CHF")


def test_known_exact_market_without_tariff_never_falls_back_to_us() -> None:
    commission = IbkrCommissionModel().calculate(
        Order("^TWII", "BUY", 1.0),
        25_000.0,
    )

    assert commission.amount == 0.0
    assert commission.currency == "TWD"
    assert commission.model == "ibkr_unpriced_venue"


def test_us_symbol_still_uses_us_schedule() -> None:
    commission = IbkrCommissionModel().calculate(
        Order("AAPL", "BUY", 100.0),
        200.0,
    )

    assert commission.currency == "USD"
    assert commission.model == "ibkr_us_stock_tiered"
