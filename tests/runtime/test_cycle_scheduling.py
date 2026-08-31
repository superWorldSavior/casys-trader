from __future__ import annotations

from datetime import datetime, timezone

from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler
from trader.planning.trade_plan import TradePlan, create_trade_plan
from trader.runtime import cycle_scheduling


class _DataSource:
    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        return list(self.bars)


def _arm_return_watch(sched: Scheduler, *, watch_id: str = "SPY:watch") -> None:
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": watch_id,
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


class _PlanStore:
    def __init__(self, plans: list[TradePlan]) -> None:
        self.plans = {plan.id: plan for plan in plans}
        self.upserts: list[TradePlan] = []

    def open_plans(self) -> list[TradePlan]:
        return list(self.plans.values())

    def upsert(self, plan: TradePlan) -> None:
        self.plans[plan.id] = plan
        self.upserts.append(plan)


def _exit_watch_plan() -> TradePlan:
    return create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "exit_watch": {
                "ttl_minutes": 90,
                "cooldown_minutes": 15,
                "logic": "all",
                "conditions": [
                    {
                        "indicator": "return",
                        "op": ">",
                        "value": 0.05,
                        "interval": "15m",
                        "lookback": "5d",
                        "window": 3,
                    }
                ],
            },
        },
    )


def test_scan_indicator_watches_emits_runtime_events_and_progress(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    _arm_return_watch(sched)
    events: list[tuple[str, dict]] = []
    progress: list[tuple[str, tuple[object, ...]]] = []

    triggered = cycle_scheduling.scan_indicator_watches(
        ["SPY"],
        sched=sched,
        now=now,
        data_source=_DataSource(
            [
                Bar(ts="2026-06-05T11:15:00+00:00", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
                Bar(ts="2026-06-05T11:30:00+00:00", open=103.0, high=104.0, low=102.0, close=103.0, volume=1000.0),
                Bar(ts="2026-06-05T11:45:00+00:00", open=110.0, high=111.0, low=109.0, close=110.0, volume=1000.0),
            ]
        ),
        is_connection_market_error=lambda exc: False,
        log_warning=lambda *args: None,
        append_event=lambda event, **payload: events.append((event, payload)),
        log_cycle_progress=lambda message, *args: progress.append((message, args)),
    )

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert events == [
        (
            "indicator_watch_triggered",
            {
                "symbol": "SPY",
                "watch_id": "SPY:watch",
                "on_trigger": "WAKE",
                "trigger_outbox_id": "SPY:watch",
            },
        )
    ]
    assert progress == [("[indicator_watch] triggered=%d symbols=%s", (1, ["SPY"]))]


def test_recovered_outbox_emits_missing_causal_trigger_event(tmp_path) -> None:
    """Crash après claim mais avant append_event: restart restitue l'événement."""
    from trader.application.cycle.watch_scanner import scan_indicator_watches

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    _arm_return_watch(sched)
    scan_indicator_watches(
        ["SPY"],
        sched=sched,
        now=now,
        data_source=_DataSource(
            [
                Bar(ts="2026-06-05T11:15:00+00:00", open=100, high=101, low=99, close=100, volume=1000),
                Bar(ts="2026-06-05T11:30:00+00:00", open=103, high=104, low=102, close=103, volume=1000),
                Bar(ts="2026-06-05T11:45:00+00:00", open=110, high=111, low=109, close=110, volume=1000),
            ]
        ),
    )

    restarted = Scheduler(tmp_path / "scheduler.json")
    pending = restarted.pending_indicator_triggers(now=now)
    events: list[tuple[str, dict]] = []
    emitted = cycle_scheduling.emit_recovered_indicator_trigger_events(
        pending,
        append_event=lambda event, **payload: events.append((event, payload)),
    )

    assert emitted == ["SPY:watch"]
    assert events == [
        (
            "indicator_watch_triggered",
            {
                "symbol": "SPY",
                "watch_id": "SPY:watch",
                "on_trigger": "WAKE",
                "trigger_outbox_id": "SPY:watch",
                "recovered_from_outbox": True,
            },
        )
    ]


def test_scan_exit_watches_emits_runtime_events_and_progress() -> None:
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    plan_store = _PlanStore([_exit_watch_plan()])
    events: list[tuple[str, dict]] = []
    progress: list[tuple[str, tuple[object, ...]]] = []

    triggered = cycle_scheduling.scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol={
            "SPY": [
                Bar(ts="2026-06-05T11:15:00+00:00", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
                Bar(ts="2026-06-05T11:30:00+00:00", open=103.0, high=104.0, low=102.0, close=103.0, volume=1000.0),
                Bar(ts="2026-06-05T11:45:00+00:00", open=110.0, high=111.0, low=109.0, close=110.0, volume=1000.0),
            ]
        },
        symbols=["SPY"],
        now=now,
        dry_run=False,
        bars_interval="15m",
        data_source=_DataSource([]),
        is_connection_market_error=lambda exc: False,
        log_warning=lambda *args: None,
        append_event=lambda event, **payload: events.append((event, payload)),
        log_cycle_progress=lambda message, *args: progress.append((message, args)),
    )

    assert [event["symbol"] for event in triggered] == ["SPY"]
    assert events == [
        (
            "exit_watch_triggered",
            {
                "symbol": "SPY",
                "plan_id": plan_store.upserts[0].id,
                "watch_id": triggered[0]["watch_id"],
            },
        )
    ]
    assert progress == [("[exit_watch] triggered=%d symbols=%s", (1, ["SPY"]))]


def test_expire_indicator_watches_emits_regular_and_armed_events(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 13, 1, tzinfo=timezone.utc)
    events: list[tuple[str, dict]] = []
    info: list[tuple[object, ...]] = []

    _arm_return_watch(sched, watch_id="SPY:regular")
    sched.set_symbol_indicator_watch(
        "QQQ",
        {
            "id": "QQQ:armed",
            "symbol": "QQQ",
            "created_at": "2026-06-05T12:00:00+00:00",
            "expires_at": "2026-06-05T13:00:00+00:00",
            "logic": "all",
            "on_trigger": "EXECUTE_ORDER",
            "conditions": [],
        },
    )

    expired = cycle_scheduling.expire_indicator_watches(
        sched,
        now=now,
        append_event=lambda event, **payload: events.append((event, payload)),
        log_info=lambda *args: info.append(args),
    )

    assert [item["id"] for item in expired] == ["SPY:regular", "QQQ:armed"]
    assert cycle_scheduling.wake_reasons_from_expired_watches(expired, now=now) == [
        {
            "symbol": "SPY",
            "reason": "watch_expired",
            "watch_id": "SPY:regular",
            "on_trigger": "WAKE",
            "expires_at": "2026-06-05T13:00:00+00:00",
            "observed_at": now.isoformat(),
        },
        {
            "symbol": "QQQ",
            "reason": "armed_plan_expired",
            "watch_id": "QQQ:armed",
            "on_trigger": "EXECUTE_ORDER",
            "expires_at": "2026-06-05T13:00:00+00:00",
            "observed_at": now.isoformat(),
        },
    ]
    assert events == [
        (
            "indicator_watch_expired",
            {
                "symbol": "SPY",
                "watch_id": "SPY:regular",
                "on_trigger": "WAKE",
                "expires_at": "2026-06-05T13:00:00+00:00",
            },
        ),
        (
            "armed_plan_expired",
            {
                "symbol": "QQQ",
                "watch_id": "QQQ:armed",
                "on_trigger": "EXECUTE_ORDER",
                "expires_at": "2026-06-05T13:00:00+00:00",
            },
        ),
    ]
    assert len(info) == 2
