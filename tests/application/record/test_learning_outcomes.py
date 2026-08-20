from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.application.record.learning_outcomes import (
    realised_entry_outcomes,
    realised_verdict,
    score_outcome,
)
from trader.domain.market_data import Bar
from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    compute_round_trips,
)

UTC = timezone.utc


def _outcomes(rows: list[dict]) -> dict[str, float]:
    try:
        trips = compute_round_trips(fills=rows)
    except ValueError:
        return {}
    return realised_entry_outcomes(aggregate_position_cycles(trips))


def _fill(
    ts: str,
    symbol: str,
    action: str,
    quantity: float,
    price: float,
    decision_id: str,
    *,
    commission: object = 0.0,
    fx_rate: object = 1.0,
    commission_model: object = "ibkr_us_stock_tiered",
    commission_currency: object = "USD",
) -> dict:
    return {
        "ts": ts,
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "price": price,
        "decision_id": decision_id,
        "commission": commission,
        "commission_model": commission_model,
        "commission_currency": commission_currency,
        "fx_rate": fx_rate,
    }


def test_realised_long_waits_for_all_partial_exits_and_deducts_commissions() -> None:
    opening = _fill("2026-07-01", "SPY", "BUY", 10, 100, "long", commission=1.0)
    first_exit = _fill("2026-07-02", "SPY", "SELL", 4, 110, "exit-1", commission=0.4)

    assert _outcomes([opening, first_exit]) == {}

    result = _outcomes(
        [
            opening,
            first_exit,
            _fill("2026-07-03", "SPY", "SELL", 6, 105, "exit-2", commission=0.6),
        ]
    )

    assert result["long"] == pytest.approx(0.068)


def test_realised_short_uses_directional_net_return() -> None:
    result = _outcomes(
        [
            _fill("2026-07-01", "SPY", "SELL", 10, 100, "short", commission=1.0),
            _fill("2026-07-02", "SPY", "BUY", 10, 90, "cover", commission=1.0),
        ]
    )

    assert result["short"] == pytest.approx(0.098)


def test_flip_allocates_fill_and_commission_between_close_and_new_lot() -> None:
    result = _outcomes(
        [
            _fill("2026-07-01", "SPY", "BUY", 4, 100, "long", commission=0.4),
            _fill("2026-07-02", "SPY", "SELL", 10, 110, "flip-short", commission=1.0),
            _fill("2026-07-03", "SPY", "BUY", 6, 100, "cover", commission=0.6),
        ]
    )

    assert result["long"] == pytest.approx(0.098)
    assert result["flip-short"] == pytest.approx(58.8 / 660.0)


def test_interleaved_symbols_keep_independent_fifo_lots() -> None:
    result = _outcomes(
        [
            _fill("2026-07-01T10:00:00", "SPY", "BUY", 2, 100, "spy"),
            _fill("2026-07-01T10:01:00", "QQQ", "SELL", 3, 200, "qqq"),
            _fill("2026-07-02T10:00:00", "SPY", "SELL", 2, 110, "spy-exit"),
            _fill("2026-07-02T10:01:00", "QQQ", "BUY", 3, 180, "qqq-exit"),
        ]
    )

    assert result == {"qqq": pytest.approx(0.1), "spy": pytest.approx(0.1)}


def test_realised_return_uses_each_fill_fx_and_native_commission() -> None:
    result = _outcomes(
        [
            _fill(
                "2026-07-01",
                "AIR.PA",
                "BUY",
                10,
                100,
                "eur-entry",
                commission=1.0,
                fx_rate=1.1,
                commission_model="ibkr_europe_stock_tiered",
                commission_currency="EUR",
            ),
            _fill(
                "2026-07-02",
                "AIR.PA",
                "SELL",
                10,
                110,
                "eur-exit",
                commission=1.0,
                fx_rate=1.2,
                commission_model="ibkr_europe_stock_tiered",
                commission_currency="EUR",
            ),
        ]
    )

    # Entry deployment: 100 * 10 * 1.1 = 1,100 USD.
    # Gross: 1,320 - 1,100 = 220 USD. Fees: 1.1 + 1.2 = 2.3 USD.
    assert result["eur-entry"] == pytest.approx(217.7 / 1_100.0)


def test_scale_in_decisions_share_the_completed_cycle_net_return() -> None:
    result = _outcomes(
        [
            _fill("2026-07-01", "SPY", "BUY", 5, 100, "entry-a", commission=0.5),
            _fill("2026-07-02", "SPY", "BUY", 5, 120, "entry-b", commission=0.5),
            _fill("2026-07-03", "SPY", "SELL", 10, 121, "exit", commission=1.0),
        ]
    )

    expected = 108.0 / 1_100.0
    assert result == {
        "entry-a": pytest.approx(expected),
        "entry-b": pytest.approx(expected),
    }


def test_explicit_zero_commission_with_valid_contract_is_proven() -> None:
    result = _outcomes(
        [
            _fill("2026-07-01", "SPY", "BUY", 1, 100, "entry"),
            _fill("2026-07-02", "SPY", "SELL", 1, 110, "exit"),
        ]
    )

    assert result == {"entry": pytest.approx(0.1)}


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"commission": None},
        {"commission": float("nan")},
        {"commission": -0.01},
        {"commission_model": "none"},
        {"commission_model": "ibkr_unknown"},
        {"commission_currency": None},
        {"commission_currency": "EUR"},
        {"fx_rate": None},
        {"fx_rate": 0.0},
        {"fx_rate": float("nan")},
    ],
)
def test_unproven_entry_fee_or_fx_keeps_learning_pending(
    invalid_fields: dict[str, object],
) -> None:
    entry = _fill("2026-07-01", "SPY", "BUY", 1, 100, "entry")
    entry.update(invalid_fields)

    assert _outcomes(
        [entry, _fill("2026-07-02", "SPY", "SELL", 1, 110, "exit")]
    ) == {}


def test_unproven_exit_leg_keeps_the_whole_cycle_pending() -> None:
    exit_fill = _fill("2026-07-02", "SPY", "SELL", 1, 110, "exit")
    exit_fill["commission"] = None

    assert _outcomes(
        [_fill("2026-07-01", "SPY", "BUY", 1, 100, "entry"), exit_fill]
    ) == {}


def test_non_finite_or_invalid_fills_are_ignored() -> None:
    assert _outcomes(
        [
            _fill("2026-07-01", "SPY", "BUY", float("inf"), 100, "infinite"),
            _fill("2026-07-01", "QQQ", "BUY", 1, float("nan"), "nan"),
            {"symbol": "SPY", "action": "BUY", "quantity": "invalid", "price": 100},
        ]
    ) == {}


def test_realised_verdict_uses_shared_significant_return_band() -> None:
    assert realised_verdict(0.006) == ("WIN", 1.0)
    assert realised_verdict(-0.006) == ("LOSS", -1.0)
    assert realised_verdict(0.005) == ("WIN", 1.0)
    assert realised_verdict(-0.005) == ("LOSS", -1.0)
    assert realised_verdict(0.001) == ("NEUTRAL", 0.0)


def _bar(ts: datetime, close: float) -> Bar:
    return Bar(
        ts=ts.isoformat(),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100.0,
    )


def _bars(start: datetime, *, initial: float = 100.0, final: float = 102.0) -> list[Bar]:
    return [
        _bar(start - timedelta(hours=1), initial),
        _bar(start, initial),
        _bar(start + timedelta(hours=4), (initial + final) / 2),
        _bar(start + timedelta(days=1), final),
    ]


def test_score_outcome_uses_one_day_then_maps_reward() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    result = score_outcome(
        {"ts": started.isoformat(), "symbol": "SPY", "action": "BUY"},
        _bars(started, final=103.0),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": "WIN",
        "reward": 1.0,
        "forward_return": pytest.approx(0.03),
    }

@pytest.mark.parametrize(
    ("quantity", "final", "verdict", "forward_return"),
    [
        (10.0, 103.0, "WIN", 0.03),
        (-10.0, 97.0, "WIN", -0.03),
    ],
)
def test_score_outcome_hold_reflects_portfolio_exposure(
    quantity: float,
    final: float,
    verdict: str,
    forward_return: float,
) -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "portfolio_snapshot": {
                "holdings": [{"symbol": "SPY", "quantity": quantity}],
            },
        },
        _bars(started, final=final),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": verdict,
        "reward": 1.0 if verdict == "WIN" else -1.0,
        "forward_return": pytest.approx(forward_return),
    }


@pytest.mark.parametrize(
    "snapshot_fields",
    [
        {},
        {"portfolio_snapshot": {}},
        {"portfolio_snapshot": {"holdings": [{}]}},
        {"portfolio_snapshot": {"holdings": [{"symbol": "SPY", "quantity": "invalid"}]}},
    ],
)
def test_score_outcome_directionless_hold_material_move_is_unknown(
    snapshot_fields: dict,
) -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            **snapshot_fields,
        },
        _bars(started, final=103.0),
        now=started + timedelta(days=4),
    )

    assert result == {
        "verdict": "UNKNOWN",
        "reward": None,
        "forward_return": pytest.approx(0.03),
    }


def test_score_outcome_directionless_hold_quiet_market_is_neutral() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "portfolio_snapshot": {"holdings": []},
        },
        _bars(started, final=100.3),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": "NEUTRAL",
        "reward": 0.0,
        "forward_return": pytest.approx(0.003),
    }


@pytest.mark.parametrize(
    ("opportunity_side", "final", "verdict", "reward"),
    [
        ("long", 103.0, "LOSS", -1.0),
        ("long", 97.0, "WIN", 1.0),
        ("short", 97.0, "LOSS", -1.0),
        ("short", 103.0, "WIN", 1.0),
    ],
)
def test_score_outcome_directional_hold_scores_missed_or_prudence(
    opportunity_side: str,
    final: float,
    verdict: str,
    reward: float,
) -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            "opportunity_side": opportunity_side,
        },
        _bars(started, final=final),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": verdict,
        "reward": reward,
        "forward_return": pytest.approx(final / 100.0 - 1.0),
    }


@pytest.mark.parametrize(
    "decision_fields",
    [
        {"decision_source": "infra"},
        {"decision_reason_code": "DATA_STALE"},
        {"decision_reason_code": "MARKET_CLOSED"},
        {"runtime": {"indicator_watch_order": {"action": "BUY"}}},
    ],
)
def test_score_outcome_unscoreable_hold_is_unknown(decision_fields: dict) -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    result = score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            **decision_fields,
        },
        _bars(started, final=103.0),
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": "UNKNOWN",
        "reward": None,
        "forward_return": pytest.approx(0.03),
    }


def test_score_outcome_uses_first_bar_at_or_after_horizon() -> None:
    started = datetime(2026, 7, 3, 16, tzinfo=UTC)
    bars = [
        _bar(started, 100.0),
        _bar(started + timedelta(days=3), 103.0),
    ]

    result = score_outcome(
        {"cycle_ts": started.isoformat(), "symbol": "SPY", "action": "BUY"},
        bars,
        now=started + timedelta(days=4),
    )

    assert result == {
        "verdict": "WIN",
        "reward": 1.0,
        "forward_return": pytest.approx(0.03),
    }


def test_score_outcome_compares_offset_bars_as_utc_datetimes() -> None:
    started = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    market_tz = timezone(timedelta(hours=-4))
    bars = [
        _bar(datetime(2026, 7, 1, 9, 29, tzinfo=market_tz), 100.0),
        # Après la décision : cette barre ne doit pas devenir le prix d'entrée.
        _bar(datetime(2026, 7, 1, 9, 31, tzinfo=market_tz), 150.0),
        # Juste avant l'horizon 1d.
        _bar(datetime(2026, 7, 2, 9, 29, tzinfo=market_tz), 102.0),
        # Première barre à/après l'horizon 1d.
        _bar(datetime(2026, 7, 2, 9, 31, tzinfo=market_tz), 103.0),
        _bar(datetime(2026, 7, 2, 10, 0, tzinfo=market_tz), 104.0),
    ]

    result = score_outcome(
        {"cycle_ts": started.isoformat(), "symbol": "SPY", "action": "BUY"},
        bars,
        now=started + timedelta(days=2),
    )

    assert result == {
        "verdict": "WIN",
        "reward": 1.0,
        "forward_return": pytest.approx(0.03),
    }
