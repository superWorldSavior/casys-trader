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


@pytest.mark.parametrize(
    ("quantity", "price"),
    [
        (float("nan"), 200.0),
        (float("inf"), 200.0),
        (float("-inf"), 200.0),
        (50.0, float("nan")),
        (50.0, float("inf")),
        (50.0, float("-inf")),
    ],
)
def test_non_finite_order_inputs_fail_closed(
    quantity: float,
    price: float,
) -> None:
    commission = IbkrCommissionModel().calculate(
        Order("AAPL", "BUY", quantity),
        price,
    )

    assert commission.amount == 0.0
    assert commission.model == "ibkr_invalid_order"


@pytest.mark.parametrize(
    ("quantity", "price", "expected"),
    [
        (0.5, 200.0, 1.0),  # 1% of USD 100 notionnel
        (0.05, 15.0, 0.01),  # 1% of USD 0.75 falls below the USD 0.01 floor
    ],
)
def test_us_fractional_orders_use_the_dedicated_ibkr_schedule(
    quantity: float,
    price: float,
    expected: float,
) -> None:
    commission = IbkrCommissionModel().calculate(
        Order("AAPL", "BUY", quantity),
        price,
    )

    assert commission.amount == pytest.approx(expected)
    assert commission.currency == "USD"
    assert commission.model == "ibkr_us_fractional_stock"


def test_us_mixed_order_prices_only_its_fractional_component_as_fractional() -> None:
    model = IbkrCommissionModel()

    whole = model.calculate(Order("AAPL", "BUY", 50.0), 200.0)
    mixed = model.calculate(Order("AAPL", "BUY", 50.0001), 200.0)

    # The USD 0.01 fractional minimum is added to the USD 0.35 whole-share
    # component.  The full USD 10,000.02 notional must never be charged at 1%.
    assert whole.amount == pytest.approx(0.35)
    assert mixed.amount == pytest.approx(0.36)
    assert mixed.amount - whole.amount == pytest.approx(0.01)
    assert mixed.model == "ibkr_us_stock_tiered_mixed_fractional"


@pytest.mark.parametrize("quantity", [249.9999999999, 250.0000000001])
def test_us_binary_float_noise_around_integer_does_not_create_fractional_leg(
    quantity: float,
) -> None:
    model = IbkrCommissionModel()

    expected = model.calculate(Order("AAPL", "BUY", 250.0), 200.0)
    noisy = model.calculate(Order("AAPL", "BUY", quantity), 200.0)

    assert noisy.amount == pytest.approx(expected.amount)
    assert noisy.model == "ibkr_us_stock_tiered"


def test_us_mixed_order_131_319_is_sum_of_whole_and_fractional_components() -> None:
    model = IbkrCommissionModel()
    price = 76.15

    mixed = model.calculate(Order("AAPL", "BUY", 131.319), price)
    whole = model.calculate(Order("AAPL", "BUY", 131.0), price)
    fractional = model.calculate(Order("AAPL", "BUY", 0.319), price)

    assert mixed.amount == pytest.approx(whole.amount + fractional.amount)
    assert mixed.amount < 1.0


def test_spain_fractional_order_uses_published_fractional_minimum() -> None:
    commission = IbkrCommissionModel().calculate(
        Order("SAN.MC", "BUY", 0.5),
        10.0,
    )

    assert commission.amount == pytest.approx(1.25)
    assert commission.currency == "EUR"
    assert commission.model == "ibkr_spain_stock_fixed_smartrouting_fractional"


def test_spain_mixed_order_keeps_whole_share_minimum() -> None:
    model = IbkrCommissionModel()

    whole = model.calculate(Order("SAN.MC", "BUY", 50.0), 10.0)
    mixed = model.calculate(Order("SAN.MC", "BUY", 50.0001), 10.0)

    assert whole.amount == pytest.approx(3.0)
    assert mixed.amount == pytest.approx(3.0)
    assert mixed.model == "ibkr_spain_stock_fixed_smartrouting"
