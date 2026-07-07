from __future__ import annotations

from datetime import datetime, timezone

from trader.market.market_data import Bar


def test_build_execution_eligibility_separates_planning_from_stale_execution() -> None:
    from trader.application.cycle.execution_eligibility import build_execution_eligibility

    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)

    eligibility = build_execution_eligibility(
        ["STALE"],
        stale_market_data={"STALE": {"last_bar_ts": "2026-06-12T20:00:00+00:00", "data_age_minutes": 4000.0}},
        prices={"STALE": 100.0},
        daily_bars_by_symbol={"STALE": [Bar(ts="2026-06-12", open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]},
        data_age_by_symbol={"STALE": 4000.0},
        now=now,
        runtime_interval="15m",
    )

    assert eligibility["STALE"]["execution"]["enabled"] is False
    assert eligibility["STALE"]["execution"]["reason"] == "runtime_stale"
    assert eligibility["STALE"]["planning"]["enabled"] is True
    assert eligibility["STALE"]["planning"]["reason"] == "daily_context_fresh"


def test_execution_blocked_reason_fails_closed_only_when_requested() -> None:
    from trader.application.cycle.execution_eligibility import execution_blocked_reason

    eligibility = {
        "OK": {"execution": {"enabled": True, "reason": "tradable"}},
        "CLOSED": {"execution": {"enabled": False, "reason": "session_closed"}},
    }

    assert execution_blocked_reason(eligibility, "OK") is None
    assert execution_blocked_reason(eligibility, "CLOSED") == "execution:session_closed"
    assert execution_blocked_reason(eligibility, "UNKNOWN") is None
    assert execution_blocked_reason(eligibility, "UNKNOWN", fail_closed=True) == "execution:unclassified"
