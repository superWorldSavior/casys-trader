from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.domain.execution.fill_accounting import POSITION_EPSILON
from trader.execution.broker import Order
from trader.infrastructure.state_db.broker_store import SqliteBroker
from trader.infrastructure.state_db.connection import open_state_db
from trader.infrastructure.state_db.migrations import import_broker_from_json
from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    compute_round_trips,
)


def test_round_trip_pnl_uses_sqlite_when_projection_is_missing_or_unreliable(
    tmp_path: Path,
) -> None:
    db = open_state_db(tmp_path / "casys.db")
    import_broker_from_json(
        db,
        tmp_path / "_absent_broker.json",
        starting_cash=100_000.0,
    )
    broker = SqliteBroker(db)
    broker.submit(
        Order("SPY", "BUY", 10.0),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 10.0),
        110.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )

    matching_rows = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 10.0,
            "price": 100.0,
            "confidence": 0.8,
        },
        {
            "ts": "2026-01-01T11:00:00+00:00",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 10.0,
            "price": 110.0,
            "exit_reason": "llm_exit",
        },
    ]
    projection_variants = (
        None,
        matching_rows * 2,
        [{**matching_rows[1], "price": 9_999.0, "quantity": 999.0}],
    )

    projection_path = tmp_path / "model_performance.jsonl"
    for projection_rows in projection_variants:
        if projection_rows is None:
            assert not projection_path.exists()
        else:
            projection_path.write_text(
                "\n".join(json.dumps(row) for row in projection_rows),
                encoding="utf-8",
            )

        trips = compute_round_trips(tmp_path)

        assert len(trips) == 1
        assert trips[0]["quantity"] == 10.0
        assert trips[0]["entry_price"] == 100.0
        assert trips[0]["exit_price"] == 110.0
        assert trips[0]["gross_pnl"] == pytest.approx(100.0)
        assert trips[0]["commission"] is None
        assert trips[0]["pnl"] is None
        assert trips[0]["commission_quality"] == {
            "status": "unavailable",
            "reason": "commission_not_modeled",
            "models": ["none"],
            "reasons": ["commission_not_modeled"],
        }

    projection_path.write_bytes(b"\xff")
    invalid_utf8 = compute_round_trips(tmp_path)
    assert len(invalid_utf8) == 1
    assert invalid_utf8[0]["gross_pnl"] == pytest.approx(100.0)
    assert invalid_utf8[0]["pnl"] is None

    projection_path.unlink()
    projection_path.mkdir()
    unreadable_directory = compute_round_trips(tmp_path)
    assert len(unreadable_directory) == 1
    assert unreadable_directory[0]["gross_pnl"] == pytest.approx(100.0)
    assert unreadable_directory[0]["pnl"] is None


@pytest.mark.parametrize(
    (
        "symbol",
        "commission_model",
        "entry_fx_rate",
        "exit_fx_rate",
        "commission_currency",
        "expected_fees",
    ),
    [
        ("SPY", "ibkr_us_stock_tiered", 1.0, 1.0, "USD", 5.0),
        (
            "SAP.DE",
            "ibkr_europe_stock_tiered",
            1.10,
            1.20,
            "EUR",
            2.0 * 1.10 + 3.0 * 1.20,
        ),
    ],
)
def test_non_usd_round_trip_converts_each_commission_in_its_own_currency(
    tmp_path: Path,
    symbol: str,
    commission_model: str,
    entry_fx_rate: float,
    exit_fx_rate: float,
    commission_currency: str,
    expected_fees: float,
) -> None:
    trips = compute_round_trips(
        tmp_path,
        fills=[
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": symbol,
                "side": "BUY",
                "quantity": 10.0,
                "price": 100.0,
                "fx_rate": entry_fx_rate,
                "commission": 2.0,
                "commission_currency": commission_currency,
                "commission_model": commission_model,
            },
            {
                "ts": "2026-01-01T11:00:00+00:00",
                "symbol": symbol,
                "side": "SELL",
                "quantity": 10.0,
                "price": 110.0,
                "fx_rate": exit_fx_rate,
                "commission": 3.0,
                "commission_currency": commission_currency,
                "commission_model": commission_model,
            },
        ],
    )

    assert len(trips) == 1
    # Cash-flow truth: USD exit proceeds - USD entry cost.  A changing EURUSD
    # rate is part of realised P&L and cannot be replaced by the exit rate.
    expected_gross = (
        110.0 * 10.0 * exit_fx_rate
        - 100.0 * 10.0 * entry_fx_rate
    )
    expected_pnl = expected_gross - expected_fees
    assert trips[0]["gross_pnl"] == pytest.approx(expected_gross)
    assert trips[0]["commission"] == pytest.approx(expected_fees)
    assert trips[0]["pnl"] == pytest.approx(expected_pnl)

    starting_cash = 5_000.0
    ending_cash = (
        starting_cash
        - 100.0 * 10.0 * entry_fx_rate
        - (2.0 if commission_currency == "USD" else 2.0 * entry_fx_rate)
        + 110.0 * 10.0 * exit_fx_rate
        - (3.0 if commission_currency == "USD" else 3.0 * exit_fx_rate)
    )
    assert trips[0]["pnl"] == pytest.approx(ending_cash - starting_cash)


def test_scaled_entry_keeps_per_fill_usd_basis_and_commission_conversion(
    tmp_path: Path,
) -> None:
    trips = compute_round_trips(
        tmp_path,
        fills=[
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "BUY",
                "quantity": 5.0,
                "price": 100.0,
                "fx_rate": 1.10,
                "commission": 1.0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
            {
                "ts": "2026-01-01T10:30:00+00:00",
                "symbol": "SAP.DE",
                "side": "BUY",
                "quantity": 5.0,
                "price": 100.0,
                "fx_rate": 1.20,
                "commission": 1.0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
            {
                "ts": "2026-01-01T11:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "SELL",
                "quantity": 10.0,
                "price": 110.0,
                "fx_rate": 1.30,
                "commission": 2.0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
        ],
    )

    assert len(trips) == 1
    assert trips[0]["gross_pnl"] == pytest.approx(
        110.0 * 10.0 * 1.30 - 100.0 * 5.0 * 1.10 - 100.0 * 5.0 * 1.20
    )
    assert trips[0]["commission"] == pytest.approx(
        1.0 * 1.10 + 1.0 * 1.20 + 2.0 * 1.30
    )
    assert trips[0]["pnl"] == pytest.approx(275.1)


def test_incompatible_commission_currency_preserves_gross_but_not_net(
    tmp_path: Path,
) -> None:
    trips = compute_round_trips(
        tmp_path,
        fills=[
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "BUY",
                "quantity": 1.0,
                "price": 100.0,
                "fx_rate": 1.10,
                "commission": 10.0,
                "commission_currency": "TWD",
                "commission_model": "ibkr_europe_stock_tiered",
            },
            {
                "ts": "2026-01-01T11:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "SELL",
                "quantity": 1.0,
                "price": 110.0,
                "fx_rate": 1.20,
                "commission": 10.0,
                "commission_currency": "TWD",
                "commission_model": "ibkr_europe_stock_tiered",
            },
        ],
    )

    assert trips[0]["gross_pnl"] == pytest.approx(22.0)
    assert trips[0]["commission"] is None
    assert trips[0]["pnl"] is None
    assert trips[0]["commission_quality"]["reason"] == (
        "commission_currency_incompatible"
    )


def test_canonical_non_usd_fill_without_fx_rate_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="fill fx rate manquant ou invalide"):
        compute_round_trips(
            tmp_path,
            fills=[
                {
                    "ts": "2026-01-01T10:00:00+00:00",
                    "symbol": "SAP.DE",
                    "side": "BUY",
                    "quantity": 1.0,
                    "price": 100.0,
                    "commission": 0.0,
                    "commission_currency": "USD",
                }
            ],
        )


def test_canonical_fill_without_commission_amount_preserves_only_gross(
    tmp_path: Path,
) -> None:
    trips = compute_round_trips(
        tmp_path,
        fills=[
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SPY",
                "side": "BUY",
                "quantity": 1.0,
                "price": 100.0,
                "fx_rate": 1.0,
                "commission": None,
                "commission_currency": "USD",
                "commission_model": "ibkr_us_stock_tiered",
            },
            {
                "ts": "2026-01-01T11:00:00+00:00",
                "symbol": "SPY",
                "side": "SELL",
                "quantity": 1.0,
                "price": 110.0,
                "fx_rate": 1.0,
                "commission": 0.35,
                "commission_currency": "USD",
                "commission_model": "ibkr_us_stock_tiered",
            },
        ],
    )

    assert trips[0]["gross_pnl"] == pytest.approx(10.0)
    assert trips[0]["commission"] is None
    assert trips[0]["pnl"] is None
    assert trips[0]["commission_quality"]["reason"] == (
        "commission_not_recorded"
    )


def test_explicit_fills_without_state_are_intrinsically_pure(
    tmp_path: Path,
) -> None:
    (tmp_path / "model_performance.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SPY",
                "action": "BUY",
                "quantity": 1.0,
                "price": 100.0,
                "decision_id": "projection-parasite",
                "confidence": 0.99,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    trips = compute_round_trips(
        fills=[
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SPY",
                "side": "BUY",
                "quantity": 1.0,
                "price": 100.0,
                "fx_rate": 1.0,
                "commission": 0.0,
                "commission_currency": "USD",
                "commission_model": "ibkr_us_stock_tiered",
            },
            {
                "ts": "2026-01-01T11:00:00+00:00",
                "symbol": "SPY",
                "side": "SELL",
                "quantity": 1.0,
                "price": 110.0,
                "fx_rate": 1.0,
                "commission": 0.0,
                "commission_currency": "USD",
                "commission_model": "ibkr_us_stock_tiered",
            },
        ],
    )

    assert trips[0]["entry_decision_ids"] == []
    assert trips[0]["entry_confidence"] is None


def test_cycle_without_explicit_commission_quality_never_invents_zero_net() -> None:
    cycles = aggregate_position_cycles(
        [
            {
                "symbol": "SPY",
                "side": "LONG",
                "quantity": 1.0,
                "entry_price": 100.0,
                "exit_price": 110.0,
                "gross_pnl": 10.0,
                "commission": None,
                "pnl": None,
                "entry_ts": "2026-01-01T10:00:00+00:00",
                "exit_ts": "2026-01-01T11:00:00+00:00",
                "position_cycle_id": "SPY:1",
                "position_cycle_closed": True,
            }
        ]
    )

    assert cycles[0]["gross_pnl"] == pytest.approx(10.0)
    assert cycles[0]["commission"] is None
    assert cycles[0]["pnl"] is None
    assert cycles[0]["commission_quality"] == {
        "status": "unavailable",
        "reason": "commission_quality_missing",
        "models": [],
        "reasons": ["commission_quality_missing"],
    }


def test_short_partial_exits_allocate_usd_basis_and_reconcile_cash(
    tmp_path: Path,
) -> None:
    trips = compute_round_trips(
        tmp_path,
        fills=[
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "SELL",
                "quantity": 10.0,
                "price": 100.0,
                "fx_rate": 1.20,
                "commission": 2.0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
            {
                "ts": "2026-01-01T11:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "BUY",
                "quantity": 4.0,
                "price": 90.0,
                "fx_rate": 1.10,
                "commission": 0.4,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
            {
                "ts": "2026-01-01T12:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "BUY",
                "quantity": 6.0,
                "price": 95.0,
                "fx_rate": 1.00,
                "commission": 0.6,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
        ],
    )

    assert len(trips) == 2
    assert [trip["gross_pnl"] for trip in trips] == pytest.approx([84.0, 150.0])
    assert [trip["commission"] for trip in trips] == pytest.approx([1.4, 2.04])
    assert trips[0]["position_cycle_closed"] is False
    assert trips[1]["position_cycle_closed"] is True
    expected_cash_delta = 1_200.0 - 2.4 - 396.0 - 0.44 - 570.0 - 0.6
    assert sum(trip["pnl"] for trip in trips) == pytest.approx(
        expected_cash_delta
    )


def test_reversal_splits_usd_basis_and_commission_between_two_cycles(
    tmp_path: Path,
) -> None:
    trips = compute_round_trips(
        tmp_path,
        fills=[
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "BUY",
                "quantity": 10.0,
                "price": 100.0,
                "fx_rate": 1.10,
                "commission": 3.0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
            {
                "ts": "2026-01-01T11:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "SELL",
                "quantity": 15.0,
                "price": 110.0,
                "fx_rate": 1.20,
                "commission": 6.0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
            {
                "ts": "2026-01-01T12:00:00+00:00",
                "symbol": "SAP.DE",
                "side": "BUY",
                "quantity": 5.0,
                "price": 100.0,
                "fx_rate": 1.00,
                "commission": 1.0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
            },
        ],
    )

    assert len(trips) == 2
    assert [trip["side"] for trip in trips] == ["LONG", "SHORT"]
    assert [trip["gross_pnl"] for trip in trips] == pytest.approx([220.0, 160.0])
    assert [trip["commission"] for trip in trips] == pytest.approx([8.1, 3.4])
    assert trips[0]["position_cycle_id"] != trips[1]["position_cycle_id"]
    expected_cash_delta = -1_100.0 - 3.3 + 1_980.0 - 7.2 - 500.0 - 1.0
    assert sum(trip["pnl"] for trip in trips) == pytest.approx(
        expected_cash_delta
    )


def test_broker_flat_dust_residual_closes_cycle_and_keeps_later_round_trip_independent() -> None:
    residual = 9.2e-9
    assert 1e-9 < residual < POSITION_EPSILON

    opened = 10.0
    dust_sell = opened - residual
    later_qty = 8.0

    def fill(*, ts: str, side: str, quantity: float, price: float) -> dict[str, object]:
        return {
            "ts": ts,
            "symbol": "3081.TWO",
            "side": side,
            "quantity": quantity,
            "price": price,
            "fx_rate": 1.0,
            "commission": 0.0,
            "commission_currency": "TWD",
            "commission_model": "ibkr_taiwan_stock_tiered",
        }

    trips = compute_round_trips(
        fills=[
            fill(
                ts="2026-01-01T10:00:00+00:00",
                side="BUY",
                quantity=opened,
                price=100.0,
            ),
            fill(
                ts="2026-01-01T11:00:00+00:00",
                side="SELL",
                quantity=dust_sell,
                price=110.0,
            ),
            fill(
                ts="2026-02-01T10:00:00+00:00",
                side="BUY",
                quantity=later_qty,
                price=100.0,
            ),
            fill(
                ts="2026-02-01T11:00:00+00:00",
                side="SELL",
                quantity=later_qty,
                price=80.0,
            ),
        ],
    )
    cycles = aggregate_position_cycles(trips)

    assert len(trips) == 2
    assert trips[0]["position_cycle_closed"] is True
    assert trips[1]["position_cycle_closed"] is True
    assert trips[0]["position_cycle_id"] != trips[1]["position_cycle_id"]
    assert trips[1]["quantity"] == pytest.approx(later_qty)
    assert trips[1]["gross_pnl"] == pytest.approx((80.0 - 100.0) * later_qty)

    assert len(cycles) == 2
    assert [cycle["position_cycle_closed"] for cycle in cycles] == [True, True]
    assert cycles[0]["position_cycle_id"] != cycles[1]["position_cycle_id"]
    assert cycles[1]["quantity"] == pytest.approx(later_qty)
    assert cycles[1]["gross_pnl"] == pytest.approx(-160.0)
    assert cycles[0]["gross_pnl"] == pytest.approx((110.0 - 100.0) * dust_sell)


def test_broker_flat_dust_cleanup_keeps_its_cycle_economics() -> None:
    residual = 9.99999993922529e-09
    partial_exit = 3.33333333
    assert 1e-9 < residual < POSITION_EPSILON

    def fill(
        *, ts: str, side: str, quantity: float, price: float
    ) -> dict[str, object]:
        return {
            "ts": ts,
            "symbol": "SPY",
            "side": side,
            "quantity": quantity,
            "price": price,
            "fx_rate": 1.0,
            "commission": 0.35,
            "commission_currency": "USD",
            "commission_model": "ibkr_us_stock_tiered",
        }

    trips = compute_round_trips(
        fills=[
            fill(
                ts="2026-01-01T10:00:00+00:00",
                side="BUY",
                quantity=10.0,
                price=100.0,
            ),
            *[
                fill(
                    ts=f"2026-01-01T{hour}:00:00+00:00",
                    side="SELL",
                    quantity=partial_exit,
                    price=110.0,
                )
                for hour in (11, 12, 13)
            ],
            fill(
                ts="2026-01-01T14:00:00+00:00",
                side="SELL",
                quantity=residual,
                price=110.0,
            ),
            fill(
                ts="2026-01-02T10:00:00+00:00",
                side="BUY",
                quantity=8.0,
                price=100.0,
            ),
            fill(
                ts="2026-01-02T11:00:00+00:00",
                side="SELL",
                quantity=8.0,
                price=80.0,
            ),
        ],
    )
    cycles = aggregate_position_cycles(trips)

    assert len(cycles) == 2
    first, second = cycles
    assert first["exit_leg_count"] == 4
    assert first["quantity"] == pytest.approx(10.0)
    assert first["gross_pnl"] == pytest.approx(100.0)
    assert first["commission"] == pytest.approx(1.75)
    assert first["pnl"] == pytest.approx(98.25)
    assert first["position_cycle_id"] != second["position_cycle_id"]
