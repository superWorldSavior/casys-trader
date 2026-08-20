from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.market.market_data import Bar, MarketError
from trader.planning.trade_plan import TradePlan, create_trade_plan
from trader.planning.scheduler import Scheduler


_DAILY_WATCH_FRESHNESS_CASES = (
    pytest.param(
        "SPY",
        datetime(2026, 6, 14, 18, tzinfo=timezone.utc),
        "2026-06-12",
        id="weekend",
    ),
    pytest.param(
        "SPY",
        datetime(2026, 6, 15, 13, tzinfo=timezone.utc),
        "2026-06-12",
        id="pre_open",
    ),
    pytest.param(
        "2330.TW",
        datetime(2026, 2, 17, 2, tzinfo=timezone.utc),
        "2026-02-11T00:00:00+08:00",
        id="tw_holiday",
    ),
)


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


def _return_bars(last_ts: str, *, closes: tuple[float, ...] = (100.0, 103.0, 110.0)) -> list[Bar]:
    last = datetime.fromisoformat(last_ts)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        ts = last - timedelta(minutes=15 * (len(closes) - 1 - index))
        bars.append(
            Bar(
                ts=ts.isoformat(),
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1000.0,
            )
        )
    return bars


def _daily_return_bars(
    last_ts: str,
    *,
    closes: tuple[float, ...] = (100.0, 103.0, 110.0),
) -> list[Bar]:
    last = datetime.fromisoformat(last_ts)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return [
        Bar(
            ts=(last - timedelta(days=len(closes) - 1 - index)).isoformat(),
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000.0,
        )
        for index, close in enumerate(closes)
    ]


def _daily_bars_with_current_partial() -> list[Bar]:
    return [
        Bar(
            ts=f"{session_date}T00:00:00-04:00",
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000.0,
        )
        for session_date, close in (
            ("2026-06-10", 98.0),
            ("2026-06-11", 99.0),
            ("2026-06-12", 100.0),
            ("2026-06-15", 110.0),
        )
    ]


def _arm_return_watch(
    sched: Scheduler,
    *,
    symbol: str = "SPY",
    on_trigger: str = "WAKE",
    order: dict | None = None,
    interval: str = "15m",
    extra_conditions: list[dict] | None = None,
    created_at: str = "2026-06-05T12:00:00+00:00",
    expires_at: str = "2026-06-05T13:00:00+00:00",
) -> None:
    conditions: list[dict] = [
        {
            "symbol": symbol,
            "indicator": "return",
            "op": ">",
            "value": 0.05,
            "interval": interval,
            "lookback": "5d",
            "window": 3,
        }
    ]
    if extra_conditions:
        conditions.extend(extra_conditions)
    watch: dict[str, object] = {
        "id": f"{symbol.lower()}-watch",
        "symbol": symbol,
        "created_at": created_at,
        "expires_at": expires_at,
        "logic": "all",
        "on_trigger": on_trigger,
        "conditions": conditions,
    }
    if order is not None:
        watch["order"] = order
    sched.set_symbol_indicator_watch(symbol, watch)


def _exit_watch_plan(
    *,
    symbol: str = "SPY",
    interval: str = "15m",
    last_triggered_at: str | None = None,
    opened_at: str = "2026-06-05T12:00:00+00:00",
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
        symbol=symbol,
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at=opened_at,
        raw_exit_plan={
            "hard_stop": 95.0,
            "exit_watch": exit_watch,
        },
    )


def test_scan_indicator_watches_triggers_and_wakes_symbol(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    _arm_return_watch(sched)
    data_source = _DataSource(_return_bars("2026-06-05T12:00:00+00:00"))

    triggered = scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert data_source.calls == [("SPY", "5d", "15m")]
    assert sched.active_indicator_watches(now=now) == []
    assert sched.next_wake("SPY") == now


def test_scan_indicator_watches_keeps_execute_order_watch_after_trigger(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    _arm_return_watch(
        sched,
        on_trigger="EXECUTE_ORDER",
        order={
            "intent": "OPEN_LONG",
            "action": "BUY",
            "qty": 10.0,
            "confidence": 0.9,
            "exit_plan": {"hard_stop": {"type": "price", "price": 95.0}},
        },
    )
    data_source = _DataSource(_return_bars("2026-06-05T12:00:00+00:00"))

    triggered = scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert triggered[0]["on_trigger"] == "EXECUTE_ORDER"
    assert triggered[0]["watch_id"] == "spy-watch"
    assert sched.next_wake("SPY") == now
    active = sched.active_indicator_watches(now=now)
    assert [watch["id"] for watch in active] == ["spy-watch"]
    assert active[0]["on_trigger"] == "EXECUTE_ORDER"


def test_scan_indicator_watches_ignores_non_connection_market_errors(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

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
    from trader.application.cycle.watch_scanner import scan_indicator_watches

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


def test_scan_indicator_watches_stale_1h_does_not_match_even_if_15m_fresh(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 8, 13, 5, tzinfo=timezone.utc)
    _arm_return_watch(
        sched,
        interval="1h",
        created_at="2026-06-05T20:00:00+00:00",
        expires_at="2026-06-08T20:00:00+00:00",
    )
    data_source = _RoutedDataSource(
        {
            ("SPY", "5d", "1h"): _return_bars("2026-06-05T20:00:00+00:00"),
            ("SPY", "5d", "15m"): _return_bars("2026-06-08T13:00:00+00:00"),
        }
    )

    triggered = scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert triggered == []
    assert data_source.calls == [("SPY", "5d", "1h")]
    assert [watch["id"] for watch in sched.active_indicator_watches(now=now)] == ["spy-watch"]
    assert sched.next_wake("SPY") is None


def test_scan_indicator_watches_fresh_1h_still_matches(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 8, 13, 5, tzinfo=timezone.utc)
    _arm_return_watch(
        sched,
        interval="1h",
        created_at="2026-06-05T20:00:00+00:00",
        expires_at="2026-06-08T20:00:00+00:00",
    )
    data_source = _DataSource(_return_bars("2026-06-08T13:00:00+00:00"))

    triggered = scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert data_source.calls == [("SPY", "5d", "1h")]
    assert sched.active_indicator_watches(now=now) == []
    assert sched.next_wake("SPY") == now


@pytest.mark.parametrize(
    ("symbol", "now", "last_daily_ts"),
    _DAILY_WATCH_FRESHNESS_CASES,
)
def test_scan_indicator_watches_daily_uses_last_completed_session(
    tmp_path,
    symbol: str,
    now: datetime,
    last_daily_ts: str,
) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    _arm_return_watch(
        sched,
        symbol=symbol,
        interval="1d",
        created_at=(now - timedelta(days=30)).isoformat(),
        expires_at=(now + timedelta(days=1)).isoformat(),
    )
    data_source = _DataSource(_daily_return_bars(last_daily_ts))

    triggered = scan_indicator_watches(
        [symbol],
        sched=sched,
        now=now,
        data_source=data_source,
    )

    assert [event["symbol"] for event in triggered] == [symbol]
    assert data_source.calls == [(symbol, "5d", "1d")]
    assert sched.next_wake(symbol) == now


def test_scan_indicator_watches_daily_rejects_missing_completed_session(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 15, 22, tzinfo=timezone.utc)
    _arm_return_watch(
        sched,
        interval="1d",
        created_at=(now - timedelta(days=1)).isoformat(),
        expires_at=(now + timedelta(days=1)).isoformat(),
    )
    data_source = _DataSource(_daily_return_bars("2026-06-12"))

    triggered = scan_indicator_watches(
        ["SPY"],
        sched=sched,
        now=now,
        data_source=data_source,
    )

    assert triggered == []
    assert [watch["id"] for watch in sched.active_indicator_watches(now=now)] == ["spy-watch"]
    assert sched.next_wake("SPY") is None


def test_scan_indicator_execute_order_ignores_current_partial_daily_bar(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import (
        _usable_watch_bars,
        scan_indicator_watches,
    )

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 15, 15, tzinfo=timezone.utc)
    _arm_return_watch(
        sched,
        interval="1d",
        on_trigger="EXECUTE_ORDER",
        order={
            "intent": "OPEN_LONG",
            "action": "BUY",
            "qty": 10.0,
            "confidence": 0.9,
            "exit_plan": {"hard_stop": {"type": "price", "price": 95.0}},
        },
        created_at=(now - timedelta(minutes=30)).isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    bars = _daily_bars_with_current_partial()

    usable = _usable_watch_bars(
        bars,
        symbol="SPY",
        interval="1d",
        now=now,
        kind="indicator_watch",
    )
    triggered = scan_indicator_watches(
        ["SPY"],
        sched=sched,
        now=now,
        data_source=_DataSource(bars),
    )

    assert usable is not None
    assert bars[-1].close / bars[-3].close - 1.0 > 0.05
    assert usable[-1].close / usable[-3].close - 1.0 < 0.05
    assert [bar.ts[:10] for bar in usable] == ["2026-06-10", "2026-06-11", "2026-06-12"]
    assert triggered == []
    assert [watch["id"] for watch in sched.active_indicator_watches(now=now)] == ["spy-watch"]
    assert sched.next_wake("SPY") is None


def test_scan_indicator_watches_absent_bars_unchanged(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 8, 13, 5, tzinfo=timezone.utc)
    _arm_return_watch(
        sched,
        interval="1h",
        created_at="2026-06-05T20:00:00+00:00",
        expires_at="2026-06-08T20:00:00+00:00",
    )
    data_source = _DataSource([])

    triggered = scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert triggered == []
    assert data_source.calls == [("SPY", "5d", "1h")]
    assert [watch["id"] for watch in sched.active_indicator_watches(now=now)] == ["spy-watch"]
    assert sched.next_wake("SPY") is None


def test_scan_indicator_watches_stale_condition_keeps_watch_atomic(tmp_path) -> None:
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 8, 13, 5, tzinfo=timezone.utc)
    _arm_return_watch(
        sched,
        interval="15m",
        extra_conditions=[
            {
                "symbol": "SPY",
                "indicator": "return",
                "op": ">",
                "value": 0.05,
                "interval": "1h",
                "lookback": "5d",
                "window": 3,
            }
        ],
        created_at="2026-06-05T20:00:00+00:00",
        expires_at="2026-06-08T20:00:00+00:00",
    )
    data_source = _RoutedDataSource(
        {
            ("SPY", "5d", "15m"): _return_bars("2026-06-08T13:00:00+00:00"),
            ("SPY", "5d", "1h"): _return_bars("2026-06-05T20:00:00+00:00"),
        }
    )

    triggered = scan_indicator_watches(["SPY"], sched=sched, now=now, data_source=data_source)

    assert triggered == []
    assert set(data_source.calls) == {("SPY", "5d", "15m"), ("SPY", "5d", "1h")}
    assert [watch["id"] for watch in sched.active_indicator_watches(now=now)] == ["spy-watch"]


def test_scan_exit_watches_reuses_runtime_bars_and_persists_cooldown() -> None:
    from trader.application.cycle.watch_scanner import scan_exit_watches

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
    from trader.application.cycle.watch_scanner import scan_exit_watches

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
    from trader.application.cycle.watch_scanner import scan_exit_watches

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
    from trader.application.cycle.watch_scanner import scan_exit_watches

    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    plan_store = _PlanStore([_exit_watch_plan(interval="1h")])
    data_source = _RoutedDataSource({("SPY", "5d", "1h"): _return_bars("2026-06-05T12:00:00+00:00")})

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


@pytest.mark.parametrize(
    ("symbol", "now", "last_daily_ts"),
    _DAILY_WATCH_FRESHNESS_CASES,
)
def test_scan_exit_watches_daily_uses_last_completed_session(
    symbol: str,
    now: datetime,
    last_daily_ts: str,
) -> None:
    from trader.application.cycle.watch_scanner import scan_exit_watches

    plan_store = _PlanStore(
        [
            _exit_watch_plan(
                symbol=symbol,
                interval="1d",
                opened_at=(now - timedelta(minutes=30)).isoformat(),
            )
        ]
    )
    data_source = _DataSource(_daily_return_bars(last_daily_ts))

    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={symbol: []},
        symbols=[symbol],
        now=now,
        dry_run=True,
        bars_interval="15m",
        data_source=data_source,
    )

    assert [event["symbol"] for event in triggered] == [symbol]
    assert [event["source"] for event in triggered] == ["exit_watch"]
    assert data_source.calls == [(symbol, "6mo", "1d")]


def test_scan_exit_watches_reused_daily_bars_reject_missing_completed_session() -> None:
    from trader.application.cycle.watch_scanner import scan_exit_watches

    now = datetime(2026, 6, 15, 22, tzinfo=timezone.utc)
    plan_store = _PlanStore(
        [
            _exit_watch_plan(
                interval="1d",
                opened_at=(now - timedelta(minutes=30)).isoformat(),
            )
        ]
    )
    data_source = _DataSource([])

    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={"SPY": _daily_return_bars("2026-06-12")},
        symbols=["SPY"],
        now=now,
        dry_run=True,
        bars_interval="1d",
        data_source=data_source,
    )

    assert triggered == []
    assert data_source.calls == []


def test_scan_exit_watch_ignores_current_partial_daily_bar() -> None:
    from trader.application.cycle.watch_scanner import (
        _usable_watch_bars,
        scan_exit_watches,
    )

    now = datetime(2026, 6, 15, 15, tzinfo=timezone.utc)
    plan_store = _PlanStore(
        [
            _exit_watch_plan(
                interval="1d",
                opened_at=(now - timedelta(minutes=30)).isoformat(),
            )
        ]
    )
    bars = _daily_bars_with_current_partial()
    data_source = _DataSource(bars)

    usable = _usable_watch_bars(
        bars,
        symbol="SPY",
        interval="1d",
        now=now,
        kind="exit_watch",
    )
    triggered = scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={"SPY": []},
        symbols=["SPY"],
        now=now,
        dry_run=True,
        bars_interval="15m",
        data_source=data_source,
    )

    assert usable is not None
    assert bars[-1].close / bars[-3].close - 1.0 > 0.05
    assert usable[-1].close / usable[-3].close - 1.0 < 0.05
    assert [bar.ts[:10] for bar in usable] == ["2026-06-10", "2026-06-11", "2026-06-12"]
    assert triggered == []
    assert data_source.calls == [("SPY", "6mo", "1d")]


@pytest.mark.parametrize("kind", ("indicator_watch", "exit_watch"))
def test_daily_watch_bars_reject_invalid_timestamp(kind: str) -> None:
    from trader.application.cycle.watch_scanner import _usable_watch_bars

    bars = _daily_bars_with_current_partial()
    bars[1] = Bar(
        ts="invalid",
        open=99.0,
        high=100.0,
        low=98.0,
        close=99.0,
        volume=1000.0,
    )

    usable = _usable_watch_bars(
        bars,
        symbol="SPY",
        interval="1d",
        now=datetime(2026, 6, 15, 15, tzinfo=timezone.utc),
        kind=kind,
    )

    assert usable is None


def test_scan_exit_watches_market_error_classification() -> None:
    from trader.application.cycle.watch_scanner import scan_exit_watches

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
