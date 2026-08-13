from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtest.data import HistoryStore
from scripts.learnings_outcome_bootstrap import _forward_return, score_learning
from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION
from trader.domain.market_data import Bar

UTC = timezone.utc


def _bar(ts: datetime, close: float) -> Bar:
    return Bar(
        ts=ts.isoformat(),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100.0,
    )


def _history(started: datetime, *, final: float) -> HistoryStore:
    return HistoryStore.from_bars(
        {
            "SPY": [
                _bar(started, 100.0),
                _bar(started + timedelta(hours=4), 101.0),
                _bar(started + timedelta(days=1), final),
            ]
        }
    )


def test_score_learning_uses_directional_benchmark_v2() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    learning = {
        "decision_id": "decision-long",
        "ts": started.isoformat(),
        "symbol": "SPY",
        "action": "HOLD",
        "note": "attendre",
    }
    decision = {
        **learning,
        "cycle_ts": started.isoformat(),
        "opportunity_side": "long",
    }

    result = score_learning(learning, decision, _history(started, final=103.0))

    assert result["benchmark_semantics_version"] == BENCHMARK_SEMANTICS_VERSION
    assert result["classify_1d"] == "missed"
    assert result["verdict"] == "LOSS"
    assert result["forward_return"] == pytest.approx(0.03)


def test_score_learning_directionless_material_move_is_unknown() -> None:
    started = datetime(2026, 7, 1, 10, tzinfo=UTC)
    learning = {
        "decision_id": "decision-directionless",
        "ts": started.isoformat(),
        "symbol": "SPY",
        "action": "HOLD",
        "note": "attendre",
    }
    decision = {**learning, "cycle_ts": started.isoformat()}

    result = score_learning(learning, decision, _history(started, final=103.0))

    assert result["classify_1d"] == "unknown"
    assert result["verdict"] == "UNKNOWN"
    assert result["forward_return"] == pytest.approx(0.03)


def test_bootstrap_forward_return_uses_first_bar_at_or_after_horizon() -> None:
    started = datetime(2026, 7, 3, 16, tzinfo=UTC)
    history = HistoryStore.from_bars(
        {
            "SPY": [
                _bar(started, 100.0),
                _bar(started + timedelta(days=3), 103.0),
            ]
        }
    )

    result = _forward_return(history, "SPY", started.isoformat(), timedelta(days=1))

    assert result == pytest.approx(0.03)

def test_bootstrap_forward_return_compares_offset_bars_as_utc_datetimes() -> None:
    started = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)
    market_tz = timezone(timedelta(hours=-4))
    history = HistoryStore.from_bars(
        {
            "SPY": [
                _bar(datetime(2026, 7, 1, 9, 29, tzinfo=market_tz), 100.0),
                _bar(datetime(2026, 7, 1, 9, 31, tzinfo=market_tz), 150.0),
                _bar(datetime(2026, 7, 2, 9, 29, tzinfo=market_tz), 102.0),
                _bar(datetime(2026, 7, 2, 9, 31, tzinfo=market_tz), 103.0),
                _bar(datetime(2026, 7, 2, 10, 0, tzinfo=market_tz), 104.0),
            ]
        }
    )

    result = _forward_return(history, "SPY", started.isoformat(), timedelta(days=1))

    assert result == pytest.approx(0.03)
