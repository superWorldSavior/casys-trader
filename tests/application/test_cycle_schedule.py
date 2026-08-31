from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trader.planning.scheduler import Scheduler, STALE_BACKOFF_MAX_MINUTES


def test_cycle_schedule_exposes_wake_policies_from_application_layer(tmp_path) -> None:
    from trader.application.cycle import schedule as cycle_schedule

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
    from trader.application.cycle import schedule as cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    events: list[tuple[str, dict]] = []
    entry: dict = {}
    watch = {
        "id": "AIR.PA:w1",
        "symbol": "AIR.PA",
        "created_at": "2026-07-02T10:00:00+00:00",
        "expires_at": "2026-07-02T13:00:00+00:00",
        "logic": "all",
        "on_trigger": "EXECUTE_ORDER",
        "rationale": "Entrer seulement après confirmation du retour horaire.",
        "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}],
        "order": {
            "intent": "OPEN_LONG",
            "action": "BUY",
            "qty": 2.0,
            "confidence": 0.72,
            "exit_plan": {"hard_stop": {"type": "price", "price": 155.0}},
        },
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
    assert entry["indicator_watch"] == watch
    assert entry["indicator_watch"] is not watch
    assert entry["indicator_watch"]["order"] is not watch["order"]
    assert sched.next_wake("AIR.PA") == datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc)
    assert entry["schedule_effect"] == {
        "status": "verified",
        "next_wake": "2026-07-02T13:00:00+00:00",
        "active_watch_ids": ["AIR.PA:w1"],
    }


def test_cycle_schedule_bounds_active_watch_to_one_hour_in_open_session(tmp_path) -> None:
    from trader.application.cycle import schedule as cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    watch = {
        "id": "AIR.PA:w-hot",
        "symbol": "AIR.PA",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=3)).isoformat(),
        "logic": "all",
        "on_trigger": "WAKE",
        "conditions": [
            {"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}
        ],
    }
    entry: dict = {}

    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym="AIR.PA",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=watch,
        entry=entry,
        session_open=True,
    )

    assert sched.next_wake("AIR.PA") == now + timedelta(hours=1)
    assert entry["schedule_wake_source"] == "hot_review"
    assert entry["hot_review_deadline"] == (now + timedelta(hours=1)).isoformat()


def test_cycle_schedule_records_non_armed_watch_supersession(tmp_path) -> None:
    from trader.application.cycle import schedule as cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    old_watch = {
        "id": "AIR.PA:old",
        "symbol": "AIR.PA",
        "created_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "on_trigger": "WAKE",
    }
    new_watch = {
        "id": "AIR.PA:new",
        "symbol": "AIR.PA",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=3)).isoformat(),
        "on_trigger": "WAKE",
    }
    sched.set_symbol_indicator_watch("AIR.PA", old_watch)
    events: list[tuple[str, dict]] = []
    entry: dict = {}

    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym="AIR.PA",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=new_watch,
        entry=entry,
        append_event=lambda event, **payload: events.append((event, payload)),
    )

    assert events[0] == (
        "indicator_watch_superseded",
        {
            "symbol": "AIR.PA",
            "watch_id": "AIR.PA:old",
            "superseded_by_watch_id": "AIR.PA:new",
        },
    )
    assert events[1][0] == "indicator_watch_created"
    assert entry["indicator_watch_superseded_ids"] == ["AIR.PA:old"]


def test_cycle_schedule_bounds_open_position_to_one_hour(tmp_path) -> None:
    from trader.application.cycle import schedule as cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    entry: dict = {}

    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym="SPY",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry=entry,
        has_position=True,
        session_open=True,
    )

    assert sched.next_wake("SPY") == now + timedelta(hours=1)
    assert entry["schedule_wake_source"] == "hot_review"


def test_cycle_schedule_keeps_nearer_explicit_15m_wake(tmp_path) -> None:
    from trader.application.cycle import schedule as cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    entry: dict = {}

    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym="SPY",
        now=now,
        next_wake_in_minutes=15.0,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry=entry,
        has_position=True,
        session_open=True,
    )

    assert sched.next_wake("SPY") == now + timedelta(minutes=15)
    assert entry["schedule_wake_source"] == "agent"


def test_cycle_schedule_clamps_late_agent_wake_to_four_hour_calm_review(tmp_path) -> None:
    from trader.application.cycle import schedule as cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    entry: dict = {}

    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym="SPY",
        now=now,
        next_wake_in_minutes=24 * 60,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry=entry,
    )

    assert sched.next_wake("SPY") == now + timedelta(hours=4)
    assert entry["schedule_wake_source"] == "calm_review"


def test_cycle_schedule_keeps_missing_scheduler_evidence_explicit() -> None:
    from trader.application.cycle import schedule as cycle_schedule

    entry: dict = {}
    cycle_schedule.apply_decision_schedule(
        sched=None,
        sym="SPY",
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry=entry,
    )

    assert entry["schedule_effect"] == {
        "status": "not_applicable",
        "reason": "scheduler_unavailable",
    }


def test_schedule_receipt_is_not_verified_when_readback_diverges() -> None:
    from trader.application.cycle import schedule as cycle_schedule

    class _DivergentScheduler:
        def next_wake(self, _symbol):
            return datetime(2026, 7, 2, 11, 0, tzinfo=timezone.utc)

        def active_indicator_watches(self, *, now):
            return []

        def has_symbol_wake(self, _symbol):
            return True

    effect = cycle_schedule.read_schedule_effect(
        _DivergentScheduler(),
        sym="SPY",
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        expected_next_wake="2026-07-02T12:00:00+00:00",
    )

    assert effect["status"] == "mismatch"
    assert effect["reason"] == "schedule_receipt_mismatch"


def test_schedule_receipt_verifies_calm_deadline_as_symbol_override(tmp_path) -> None:
    from trader.application.cycle import schedule as cycle_schedule

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    sched.set_next_wake("2026-07-02T14:00:00+00:00")
    sched.set_symbol_next_wake("SPY", "2026-07-02T12:00:00+00:00")
    entry: dict = {}

    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym="SPY",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry=entry,
    )

    assert sched.has_symbol_wake("SPY") is True
    assert sched.next_wake("SPY") == now + timedelta(hours=4)
    assert entry["schedule_effect"] == {
        "status": "verified",
        "next_wake": "2026-07-02T14:00:00+00:00",
        "active_watch_ids": [],
    }
