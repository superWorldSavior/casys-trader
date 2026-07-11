from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from trader.application.record.learning_outcomes import (
    realised_entry_outcomes,
    realised_verdict,
    score_outcome,
)
from trader.domain.market_data import Bar

UTC = timezone.utc


@dataclass(frozen=True)
class InMemoryPerformanceRows:
    rows: list[dict]

    def read_rows(self) -> list[dict]:
        return self.rows


def _outcomes(rows: list[dict]) -> dict[str, float]:
    return realised_entry_outcomes(InMemoryPerformanceRows(rows))


def _fill(
    ts: str,
    symbol: str,
    action: str,
    quantity: float,
    price: float,
    decision_id: str,
    *,
    commission: float = 0.0,
) -> dict:
    return {
        "ts": ts,
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "price": price,
        "decision_id": decision_id,
        "commission": commission,
        "fx_rate": 1.0,
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
        (0.0, 103.0, "LOSS", 0.03),
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
def test_score_outcome_hold_without_reliable_snapshot_stays_pending(snapshot_fields: dict) -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)

    assert score_outcome(
        {
            "cycle_ts": started.isoformat(),
            "symbol": "SPY",
            "action": "HOLD",
            **snapshot_fields,
        },
        _bars(started, final=103.0),
        now=started + timedelta(days=4),
    ) is None
