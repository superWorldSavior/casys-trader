import math

import pytest

from trader.risk import RiskGate, RiskLimits


def _gate(*, max_risk_per_trade_pct: float = 0.01) -> RiskGate:
    return RiskGate(
        RiskLimits(
            max_position_value=20_000.0,
            max_gross_exposure=100_000.0,
            max_order_value=10_000.0,
            max_orders_per_cycle=5,
            min_equity=50_000.0,
            max_risk_per_trade_pct=max_risk_per_trade_pct,
        )
    )


@pytest.mark.parametrize(
    ("equity", "entry_price", "stop_price", "expected_qty"),
    [
        (100_000.0, 100.0, 99.0, 1_000.0),
        (100_000.0, 100.0, 90.0, 100.0),
        (50_000.0, 100.0, 90.0, 50.0),
    ],
)
def test_max_quantity_at_risk_borne_la_taille_par_distance_au_stop(
    equity: float,
    entry_price: float,
    stop_price: float,
    expected_qty: float,
) -> None:
    gate = _gate()

    qty = gate.max_quantity_at_risk(equity, entry_price, stop_price)

    distance = abs(entry_price - stop_price)
    assert qty == pytest.approx(expected_qty)
    assert qty * distance <= gate.limits.max_risk_per_trade_pct * equity


@pytest.mark.parametrize(
    ("equity", "entry_price", "stop_price"),
    [
        (100_000.0, 100.0, 100.0),
        (0.0, 100.0, 90.0),
        (-1.0, 100.0, 90.0),
        (100_000.0, math.inf, 90.0),
        (100_000.0, 100.0, math.nan),
    ],
)
def test_max_quantity_at_risk_renvoie_zero_aux_bornes_non_exploitables(
    equity: float,
    entry_price: float,
    stop_price: float,
) -> None:
    assert _gate().max_quantity_at_risk(equity, entry_price, stop_price) == 0.0


def test_max_quantity_at_risk_arrondit_vers_le_bas_pour_respecter_linvariant() -> None:
    gate = _gate()
    entry_price = 100.0
    stop_price = 99.97
    distance = abs(entry_price - stop_price)

    qty = gate.max_quantity_at_risk(100_000.0, entry_price, stop_price)

    assert qty * distance <= gate.limits.max_risk_per_trade_pct * 100_000.0
    assert math.nextafter(qty, math.inf) * distance > gate.limits.max_risk_per_trade_pct * 100_000.0


def test_risk_limits_from_dict_defaut_a_un_pourcent() -> None:
    limits = RiskLimits.from_dict(
        {
            "max_position_value": 20_000.0,
            "max_gross_exposure": 100_000.0,
            "max_order_value": 10_000.0,
            "max_orders_per_cycle": 5,
            "min_equity": 50_000.0,
        }
    )

    assert limits.max_risk_per_trade_pct == 0.01
