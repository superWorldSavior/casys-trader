"""Tests TDD — L5 : set_next_wake événementiel.

Couvre :
- Parsing {minutes} (compat)
- Parsing {on: event} (session_open, macro_event, pre_earnings, inconnu)
- Pas de minutes ni on → ValueError (compat)
- Résolution d'événement → timestamp absolu (_resolve_wake_event)
- _apply_decision_schedule avec next_wake_iso
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from trader.agent.protocol.parsing import _decision_from_symbol_calls
from trader.agent.protocol.types import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_wake_call(args: dict, symbol: str = "AAPL") -> Decision:
    """Construit une réponse minimale set_next_wake et la parse."""
    data = {
        "symbol": symbol,
        "confidence": 0.5,
        "rationale": "test",
        "decision_reason_code": "HOLD_NO_SIGNAL",
        "calls": [{"tool": "set_next_wake", "args": args}],
    }
    return _decision_from_symbol_calls(data, symbol)


def _now() -> datetime:
    return datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Parsing — {minutes} compat
# ---------------------------------------------------------------------------


def test_parse_minutes_compat_next_wake_in_minutes_set() -> None:
    """Compat : {minutes: 15} → next_wake_in_minutes = 15.0."""
    d = _parse_wake_call({"minutes": 15})
    assert d.next_wake_in_minutes == 15.0


def test_parse_minutes_compat_next_wake_event_none() -> None:
    """Compat : {minutes: 15} → next_wake_event = None."""
    d = _parse_wake_call({"minutes": 15})
    assert d.next_wake_event is None


# ---------------------------------------------------------------------------
# Parsing — {on: event}
# ---------------------------------------------------------------------------


def test_parse_on_session_open() -> None:
    """on:session_open → next_wake_event = 'session_open', next_wake_in_minutes = None."""
    d = _parse_wake_call({"on": "session_open"})
    assert d.next_wake_event == "session_open"
    assert d.next_wake_in_minutes is None


def test_parse_on_macro_event() -> None:
    """on:macro_event → next_wake_event = 'macro_event'."""
    d = _parse_wake_call({"on": "macro_event"})
    assert d.next_wake_event == "macro_event"
    assert d.next_wake_in_minutes is None


def test_parse_on_pre_earnings() -> None:
    """on:pre_earnings → next_wake_event = 'pre_earnings'."""
    d = _parse_wake_call({"on": "pre_earnings"})
    assert d.next_wake_event == "pre_earnings"
    assert d.next_wake_in_minutes is None


def test_parse_on_unknown_no_error() -> None:
    """on: inconnu → pas d'erreur au parsing, next_wake_event = valeur brute."""
    d = _parse_wake_call({"on": "some_future_event"})
    assert d.next_wake_event == "some_future_event"
    assert d.next_wake_in_minutes is None


def test_parse_no_minutes_no_on_raises() -> None:
    """Ni minutes ni on → ValueError (compat)."""
    with pytest.raises(ValueError, match="wake_minutes_required"):
        _parse_wake_call({})


# ---------------------------------------------------------------------------
# Résolution d'événement → timestamp absolu
# ---------------------------------------------------------------------------


def test_resolve_session_open_returns_next_session_iso() -> None:
    """session_open → appelle next_regular_session_open et retourne l'ISO."""
    from trader.runtime import cycle_scheduling

    now = _now()
    expected_iso = "2026-07-04T09:30:00+00:00"

    calls: list[tuple[str, str]] = []

    def fake_next_session(now_arg: datetime, *, symbol: str) -> datetime:
        calls.append(("next_regular_session_open", symbol))
        return datetime.fromisoformat(expected_iso)

    result = cycle_scheduling.resolve_wake_event(
        event="session_open",
        sym="AAPL",
        now=now,
        macro_next=[],
        next_regular_session_open=fake_next_session,
    )

    assert result == expected_iso
    assert calls == [("next_regular_session_open", "AAPL")]


def test_resolve_macro_event_with_data_returns_first_at() -> None:
    """macro_event + macro_next non vide → retourne l'at du premier événement."""
    from trader.runtime import cycle_scheduling

    macro_next = [
        {"event": "FOMC", "at": "2026-07-29T18:00:00Z", "in_h": 470.0},
        {"event": "CPI", "at": "2026-08-12T12:30:00Z", "in_h": 800.0},
    ]

    result = cycle_scheduling.resolve_wake_event(
        event="macro_event",
        sym="SPY",
        now=_now(),
        macro_next=macro_next,
        next_regular_session_open=lambda *a, **kw: None,
    )

    assert result == "2026-07-29T18:00:00Z"


def test_resolve_macro_event_empty_macro_next_returns_none() -> None:
    """macro_event + macro_next vide → None (fail-safe)."""
    from trader.runtime import cycle_scheduling

    result = cycle_scheduling.resolve_wake_event(
        event="macro_event",
        sym="SPY",
        now=_now(),
        macro_next=[],
        next_regular_session_open=lambda *a, **kw: None,
    )

    assert result is None


def test_resolve_pre_earnings_no_data_returns_none() -> None:
    """pre_earnings → None (aucune donnée earnings disponible encore)."""
    from trader.runtime import cycle_scheduling

    result = cycle_scheduling.resolve_wake_event(
        event="pre_earnings",
        sym="AAPL",
        now=_now(),
        macro_next=[],
        next_regular_session_open=lambda *a, **kw: None,
    )

    assert result is None


def test_resolve_unknown_event_returns_none() -> None:
    """Événement inconnu → None (fail-safe, pas d'erreur)."""
    from trader.runtime import cycle_scheduling

    result = cycle_scheduling.resolve_wake_event(
        event="unknown_future_event",
        sym="SPY",
        now=_now(),
        macro_next=[],
        next_regular_session_open=lambda *a, **kw: None,
    )

    assert result is None


def test_resolve_session_open_exception_returns_none() -> None:
    """Si next_regular_session_open lève → None (fail-safe)."""
    from trader.runtime import cycle_scheduling

    def broken_session(now_arg: datetime, *, symbol: str) -> datetime:
        raise RuntimeError("aucun calendrier")

    result = cycle_scheduling.resolve_wake_event(
        event="session_open",
        sym="AAPL",
        now=_now(),
        macro_next=[],
        next_regular_session_open=broken_session,
    )

    assert result is None


# ---------------------------------------------------------------------------
# _apply_decision_schedule avec next_wake_iso
# ---------------------------------------------------------------------------


def test_apply_decision_schedule_next_wake_iso_sets_absolute_wake(tmp_path) -> None:
    """next_wake_iso → set_symbol_next_wake (non relatif)."""
    from trader.runtime import cycle_scheduling
    from trader.planning.scheduler import Scheduler

    sched = Scheduler(tmp_path / "scheduler.json")
    now = _now()
    target_iso = "2026-07-03T12:30:00+00:00"

    cycle_scheduling.apply_decision_schedule(
        sched=sched,
        sym="AAPL",
        now=now,
        next_wake_in_minutes=None,
        next_wake_iso=target_iso,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry={},
    )

    recorded = sched.next_wake("AAPL")
    assert recorded is not None
    assert recorded.isoformat() == datetime.fromisoformat(target_iso).isoformat()


def test_apply_decision_schedule_minutes_still_works(tmp_path) -> None:
    """next_wake_iso=None + next_wake_in_minutes → comportement existant inchangé."""
    from trader.runtime import cycle_scheduling
    from trader.planning.scheduler import Scheduler

    sched = Scheduler(tmp_path / "scheduler.json")
    now = _now()

    cycle_scheduling.apply_decision_schedule(
        sched=sched,
        sym="SPY",
        now=now,
        next_wake_in_minutes=30.0,
        next_wake_iso=None,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry={},
    )

    recorded = sched.next_wake("SPY")
    assert recorded is not None
    expected = datetime(2026, 7, 3, 10, 30, tzinfo=timezone.utc)
    assert recorded == expected


def test_apply_decision_schedule_iso_takes_precedence_over_minutes(tmp_path) -> None:
    """Si next_wake_iso ET next_wake_in_minutes → next_wake_iso prime."""
    from trader.runtime import cycle_scheduling
    from trader.planning.scheduler import Scheduler

    sched = Scheduler(tmp_path / "scheduler.json")
    now = _now()
    target_iso = "2026-07-03T12:00:00+00:00"

    cycle_scheduling.apply_decision_schedule(
        sched=sched,
        sym="AAPL",
        now=now,
        next_wake_in_minutes=60.0,  # 11:00 UTC, ignoré car l'ISO prime
        next_wake_iso=target_iso,  # 12:00 UTC, sous la borne calme de 4 h
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry={},
    )

    recorded = sched.next_wake("AAPL")
    assert recorded == datetime.fromisoformat(target_iso)


def test_apply_decision_schedule_late_iso_is_clamped_to_calm_review(tmp_path) -> None:
    """Un wake événementiel lointain ne repousse pas la revue calme au-delà de 4 h."""
    from trader.runtime import cycle_scheduling
    from trader.planning.scheduler import Scheduler

    sched = Scheduler(tmp_path / "scheduler.json")
    now = _now()
    entry: dict = {}

    cycle_scheduling.apply_decision_schedule(
        sched=sched,
        sym="AAPL",
        now=now,
        next_wake_in_minutes=None,
        next_wake_iso="2026-07-04T09:30:00+00:00",
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry=entry,
    )

    assert sched.next_wake("AAPL") == now + timedelta(hours=4)
    assert entry["schedule_wake_source"] == "calm_review"


# ---------------------------------------------------------------------------
# Intégration parsing → Decision.next_wake_event
# ---------------------------------------------------------------------------


def test_decision_next_wake_event_field_exists() -> None:
    """Decision a bien un champ next_wake_event."""
    d = Decision(
        symbol="X", action="HOLD", quantity=0.0, confidence=0.0, rationale="r"
    )
    assert hasattr(d, "next_wake_event")
    assert d.next_wake_event is None


def test_parse_batch_on_session_open_via_symbol_calls() -> None:
    """Intégration : parse_batch parse {on:session_open} depuis decisions[].calls."""
    from trader.agent.protocol.parsing import parse_batch

    raw = json.dumps({
        "decisions": [{
            "symbol": "AAPL",
            "confidence": 0.6,
            "rationale": "breakout",
            "decision_reason_code": "ENTRY_SIGNAL",
            "calls": [
                {"tool": "set_next_wake", "args": {"on": "session_open"}},
            ],
        }]
    })

    result = parse_batch(raw, ["AAPL"], allow_context_request=False)
    d = result["AAPL"]
    assert isinstance(d, Decision)
    assert d.next_wake_event == "session_open"
    assert d.next_wake_in_minutes is None
