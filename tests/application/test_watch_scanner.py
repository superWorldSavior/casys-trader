from __future__ import annotations

from datetime import datetime, timezone

from trader.market.market_data import Bar, MarketError
from trader.scheduling.scheduler import Scheduler


class _DataSource:
    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        return list(self.bars)


class _FailingDataSource:
    def __init__(self, exc: MarketError) -> None:
        self.exc = exc

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        raise self.exc


def _arm_return_watch(sched: Scheduler) -> None:
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "spy-watch",
            "symbol": "SPY",
            "created_at": "2026-06-05T12:00:00+00:00",
            "expires_at": "2026-06-05T13:00:00+00:00",
            "logic": "all",
            "on_trigger": "WAKE",
            "conditions": [
                {
                    "symbol": "SPY",
                    "indicator": "return",
                    "op": ">",
                    "value": 0.05,
                    "interval": "15m",
                    "lookback": "5d",
                    "window": 3,
                }
            ],
        },
    )


def test_scan_indicator_watches_triggers_and_wakes_symbol(tmp_path) -> None:
    from trader.application.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    _arm_return_watch(sched)
    data_source = _DataSource(
        [
            Bar(ts="t1", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
            Bar(ts="t2", open=103.0, high=104.0, low=102.0, close=103.0, volume=1000.0),
            Bar(ts="t3", open=110.0, high=111.0, low=109.0, close=110.0, volume=1000.0),
        ]
    )

    triggered = scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert data_source.calls == [("SPY", "5d", "15m")]
    assert sched.active_indicator_watches(now=now) == []
    assert sched.next_wake("SPY") == now


def test_scan_indicator_watches_ignores_non_connection_market_errors(tmp_path) -> None:
    from trader.application.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    _arm_return_watch(sched)
    warnings: list[tuple[object, ...]] = []

    triggered = scan_indicator_watches(
        ["SPY"],
        sched=sched,
        now=now,
        data_source=_FailingDataSource(MarketError("no_data", "SPY")),
        is_connection_market_error=lambda exc: False,
        log_warning=lambda *args: warnings.append(args),
    )

    assert triggered == []
    assert sched.active_indicator_watches(now=now) != []
    assert warnings == [("indicator_watch data unavailable %s/%s: %s", "SPY", "15m", "no_data")]


def test_scan_indicator_watches_reraises_connection_market_errors(tmp_path) -> None:
    import pytest

    from trader.application.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    _arm_return_watch(sched)

    with pytest.raises(MarketError, match="IB down"):
        scan_indicator_watches(
            ["SPY"],
            sched=sched,
            now=now,
            data_source=_FailingDataSource(MarketError("ib_connect_failed", "IB down")),
            is_connection_market_error=lambda exc: True,
        )
