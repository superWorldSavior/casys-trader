from __future__ import annotations

from datetime import datetime, timezone

from trader.planning.scheduler import Scheduler, STALE_BACKOFF_MAX_MINUTES


def test_cycle_schedule_exposes_wake_policies_from_application_layer(tmp_path) -> None:
    from trader.application.cycle import cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    sched.set_next_wake("2026-06-05T12:30:00+00:00")

    assert cycle_schedule.bounded_wake_minutes(260.0) == 260.0
    assert cycle_schedule.bounded_wake_minutes(260.0, maximum=240.0) == 240.0
    assert (
        cycle_schedule.stale_backoff_wake_minutes(
            streak=10,
            default_wake_minutes=30.0,
        )
        == STALE_BACKOFF_MAX_MINUTES
    )
    assert (
        cycle_schedule.select_due_symbols(
            ["SPY", "QQQ"],
            sched=sched,
            once=False,
            bootstrap=False,
            now=now,
        )
        == []
    )


def test_cycle_schedule_applies_watch_then_expiry_wake(tmp_path) -> None:
    from trader.application.cycle import cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    events: list[tuple[str, dict]] = []
    entry: dict = {}
    watch = {
        "id": "AIR.PA:w1",
        "symbol": "AIR.PA",
        "expires_at": "2026-07-02T13:00:00+00:00",
        "logic": "all",
        "on_trigger": "EXECUTE_ORDER",
        "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}],
    }

    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym="AIR.PA",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=watch,
        entry=entry,
        append_event=lambda event, **payload: events.append((event, payload)),
    )

    assert events == [
        (
            "armed_plan_created",
            {
                "symbol": "AIR.PA",
                "watch_id": "AIR.PA:w1",
                "on_trigger": "EXECUTE_ORDER",
                "expires_at": "2026-07-02T13:00:00+00:00",
            },
        )
    ]
    assert entry["indicator_watch_created"] is True
    assert sched.next_wake("AIR.PA") == datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc)
