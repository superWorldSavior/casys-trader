from __future__ import annotations

from datetime import datetime, timezone

from trader.application.record.decision_watches import prepare_decision_indicator_watch
from trader.planning.indicator_watch import WATCH_REJECT_UNKNOWN_INDICATOR


NOW = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)


def _valid_raw_watch() -> dict:
    return {
        "ttl_minutes": 90,
        "logic": "all",
        "on_trigger": "WAKE",
        "conditions": [
            {"indicator": "return", "op": ">", "value": 0.01, "interval": "15m"},
        ],
    }


def test_prepare_decision_indicator_watch_returns_pending_watch_and_audit_updates() -> None:
    prepared = prepare_decision_indicator_watch(
        _valid_raw_watch(),
        symbol="SPY",
        now=NOW,
        scheduling_enabled=True,
    )

    assert prepared.pending_watch is not None
    assert prepared.pending_watch["symbol"] == "SPY"
    assert prepared.pending_watch["on_trigger"] == "WAKE"
    assert prepared.pending_watch["conditions"][0]["indicator"] == "return"
    assert prepared.rejections == []
    assert prepared.entry_updates == {"indicator_watch_rejections": []}


def test_prepare_decision_indicator_watch_keeps_audit_when_scheduler_is_absent() -> None:
    prepared = prepare_decision_indicator_watch(
        _valid_raw_watch(),
        symbol="SPY",
        now=NOW,
        scheduling_enabled=False,
    )

    assert prepared.pending_watch is None
    assert prepared.rejections == []
    assert prepared.entry_updates == {"indicator_watch_rejections": []}


def test_prepare_decision_indicator_watch_exposes_rejections_without_pending_watch() -> None:
    prepared = prepare_decision_indicator_watch(
        {"conditions": [{"indicator": "not_real", "op": ">", "value": 1.0}]},
        symbol="SPY",
        now=NOW,
        scheduling_enabled=True,
    )

    rejections = [{"reason": WATCH_REJECT_UNKNOWN_INDICATOR, "indicator": "not_real", "raw_value": None}]
    assert prepared.pending_watch is None
    assert prepared.rejections == rejections
    assert prepared.entry_updates == {"indicator_watch_rejections": rejections}


def test_prepare_decision_indicator_watch_ignores_empty_request() -> None:
    prepared = prepare_decision_indicator_watch(
        None,
        symbol="SPY",
        now=NOW,
        scheduling_enabled=True,
    )

    assert prepared.pending_watch is None
    assert prepared.rejections == []
    assert prepared.entry_updates == {}
