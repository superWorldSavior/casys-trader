from __future__ import annotations

from datetime import datetime, timezone

from trader.market.market_data import Bar, MarketError
from trader.planning.trade_plan import TradePlan, create_trade_plan
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


class _RoutedDataSource:
    def __init__(self, bars_by_key: dict[tuple[str, str, str], list[Bar]]) -> None:
        self.bars_by_key = bars_by_key
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        return list(self.bars_by_key.get((symbol, lookback, interval), []))


class _PlanStore:
    def __init__(self, plans: list[TradePlan]) -> None:
        self.plans = {plan.id: plan for plan in plans}
        self.upserts: list[TradePlan] = []

    def open_plans(self) -> list[TradePlan]:
        return list(self.plans.values())

    def upsert(self, plan: TradePlan) -> None:
        self.plans[plan.id] = plan
        self.upserts.append(plan)


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


def _exit_watch_plan(
    *,
    interval: str = "15m",
    last_triggered_at: str | None = None,
) -> TradePlan:
    exit_watch: dict[str, object] = {
        "ttl_minutes": 90,
        "cooldown_minutes": 15,
        "logic": "all",
        "conditions": [
            {
                "indicator": "return",
                "op": ">",
                "value": 0.05,
                "interval": interval,
                "lookback": "5d",
                "window": 3,
            }
        ],
    }
    if last_triggered_at is not None:
        exit_watch["last_triggered_at"] = last_triggered_at
    return create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "exit_watch": exit_watch,
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


def test_scan_exit_watches_reuses_runtime_bars_and_persists_cooldown() -> None:
    from trader.application.watch_scanner import scan_exit_watches

    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    plan_store = _PlanStore([_exit_watch_plan()])
    bars = [
        Bar(ts="t1", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="t2", open=103.0, high=104.0, low=102.0, close=103.0, volume=1000.0),
        Bar(ts="t3", open=110.0, high=111.0, low=109.0, close=110.0, volume=1000.0),
    ]
    data_source = _DataSource([])

    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={"SPY": bars},
        symbols=["SPY"],
        now=now,
        dry_run=False,
        bars_interval="15m",
        data_source=data_source,
    )

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert triggered[0]["source"] == "exit_watch"
    assert triggered[0]["plan_id"] == plan_store.upserts[0].id
    assert triggered[0]["on_trigger"] == "WAKE"
    assert data_source.calls == []
    assert plan_store.upserts[0].exit_watch["last_triggered_at"] == now.isoformat()


def test_scan_exit_watches_dry_run_does_not_persist_cooldown() -> None:
    from trader.application.watch_scanner import scan_exit_watches

    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    plan_store = _PlanStore([_exit_watch_plan()])
    bars = [
        Bar(ts="t1", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="t2", open=103.0, high=104.0, low=102.0, close=103.0, volume=1000.0),
        Bar(ts="t3", open=110.0, high=111.0, low=109.0, close=110.0, volume=1000.0),
    ]

    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={"SPY": bars},
        symbols=["SPY"],
        now=now,
        dry_run=True,
        bars_interval="15m",
        data_source=_DataSource([]),
    )

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert plan_store.upserts == []


def test_scan_exit_watches_skips_cooldown_without_fetch() -> None:
    from trader.application.watch_scanner import scan_exit_watches

    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    plan_store = _PlanStore([_exit_watch_plan(last_triggered_at="2026-06-05T12:00:00+00:00")])
    data_source = _DataSource([])

    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={"SPY": []},
        symbols=["SPY"],
        now=now,
        dry_run=False,
        bars_interval="15m",
        data_source=data_source,
    )

    assert triggered == []
    assert data_source.calls == []
    assert plan_store.upserts == []


def test_scan_exit_watches_fetches_missing_timeframe() -> None:
    from trader.application.watch_scanner import scan_exit_watches

    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    plan_store = _PlanStore([_exit_watch_plan(interval="1h")])
    bars = [
        Bar(ts="t1", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="t2", open=103.0, high=104.0, low=102.0, close=103.0, volume=1000.0),
        Bar(ts="t3", open=110.0, high=111.0, low=109.0, close=110.0, volume=1000.0),
    ]
    data_source = _RoutedDataSource({("SPY", "5d", "1h"): bars})

    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={"SPY": []},
        symbols=["SPY"],
        now=now,
        dry_run=True,
        bars_interval="15m",
        data_source=data_source,
    )

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert data_source.calls == [("SPY", "5d", "1h")]


def test_scan_exit_watches_market_error_classification() -> None:
    import pytest

    from trader.application.watch_scanner import scan_exit_watches

    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    plan_store = _PlanStore([_exit_watch_plan(interval="1h")])
    warnings: list[tuple[object, ...]] = []

    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={"SPY": []},
        symbols=["SPY"],
        now=now,
        dry_run=False,
        bars_interval="15m",
        data_source=_FailingDataSource(MarketError("no_data", "SPY")),
        is_connection_market_error=lambda exc: False,
        log_warning=lambda *args: warnings.append(args),
    )

    assert triggered == []
    assert warnings == [("exit_watch data unavailable %s/%s: %s", "SPY", "1h", "no_data")]

    with pytest.raises(MarketError, match="IB down"):
        scan_exit_watches(
            plan_store=plan_store,
            bars_by_symbol={"SPY": []},
            symbols=["SPY"],
            now=now,
            dry_run=False,
            bars_interval="15m",
            data_source=_FailingDataSource(MarketError("ib_connect_failed", "IB down")),
            is_connection_market_error=lambda exc: True,
        )
