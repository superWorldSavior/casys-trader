from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.application.cycle.infra_holds import quiet_gate_decisions, stale_market_hold_decision


NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


class WakeSourceStub:
    def __init__(self, symbols: set[str]) -> None:
        self._symbols = symbols

    def symbols_with_wake(self) -> set[str]:
        return set(self._symbols)


def test_quiet_gate_decisions_builds_infra_hold_entry_for_calm_symbol() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        cockpit={"cols": ["s", "st", "sig"], "rows": [["SPY", False, []]]},
        regime_families={"us": {"frac": 0.2}},
        active_families={"us": ["SPY"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={"SPY": "yfinance"},
    )

    assert result.kept_symbols == []
    assert result.gated_symbols == ["SPY"]
    assert result.reasons == {}
    assert result.entries == [
        {
            "symbol": "SPY",
            "action": "HOLD",
            "qty": 0.0,
            "confidence": 0.0,
            "rationale": "quiet_gate",
            "next_wake_in_minutes": None,
            "intent": "HOLD",
            "trade_plan_created": False,
            "executed": False,
            "reason": "quiet_gate",
            "decision_reason_code": "NO_EDGE",
            "decision_source": "infra",
            "model_called": False,
            "data_source": "yfinance",
        }
    ]


def test_quiet_gate_decisions_keeps_agent_wake_and_due_position_symbols() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY", "QQQ"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={
            ("/tmp/state", "SPY"): NOW - timedelta(hours=1),
            ("/tmp/state", "QQQ"): NOW - timedelta(hours=2),
        },
        cockpit={"cols": ["s", "st", "sig"], "rows": [["SPY", False, []], ["QQQ", False, []]]},
        regime_families={},
        active_families={},
        wake_source=WakeSourceStub({"SPY"}),
        triggers_by_symbol={},
        held_symbols={"QQQ"},
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["SPY", "QQQ"]
    assert result.gated_symbols == []
    assert result.reasons == {"SPY": "agent_wake", "QQQ": "position"}
    assert result.entries == []


def test_quiet_gate_decisions_gates_recent_routine_position_review() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(minutes=30)},
        cockpit={},
        regime_families={},
        active_families={},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols={"SPY"},
        runtime_data_source_by_sym={"SPY": "ib"},
    )

    assert result.kept_symbols == []
    assert result.gated_symbols == ["SPY"]
    assert result.reasons == {}
    assert result.entries[0]["reason"] == "quiet_gate"
    assert result.entries[0]["relevance_gate_reason"] == "position_debounce"
    assert result.entries[0]["model_called"] is False


def test_quiet_gate_decisions_keeps_periodic_review_on_first_seen_symbol() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={},
        cockpit={},
        regime_families={},
        active_families={},
        wake_source=None,
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["SPY"]
    assert result.gated_symbols == []
    assert result.reasons == {"SPY": "periodic_review"}
    assert result.entries == []


def test_quiet_gate_decisions_keeps_trigger_regime_and_signal_symbols() -> None:
    last_seen = {
        ("/tmp/state", "TRIGGER"): NOW - timedelta(hours=1),
        ("/tmp/state", "REGIME"): NOW - timedelta(hours=1),
        ("/tmp/state", "SIGNAL"): NOW - timedelta(hours=1),
    }

    result = quiet_gate_decisions(
        symbols=["TRIGGER", "REGIME", "SIGNAL"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at=last_seen,
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [
                ["TRIGGER", False, []],
                ["REGIME", False, []],
                ["SIGNAL", False, ["1h:breakout_up"]],
            ],
        },
        regime_families={"theme": {"frac": 0.72}},
        active_families={"theme": ["REGIME"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={"TRIGGER": [{"watch_id": "w1"}]},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["TRIGGER", "REGIME", "SIGNAL"]
    assert result.gated_symbols == []
    assert result.reasons == {
        "TRIGGER": "trigger",
        "REGIME": "regime",
        "SIGNAL": "signal",
    }
    assert result.persistent_reasons == {
        "TRIGGER": (),
        "REGIME": ("regime",),
        "SIGNAL": ("signal",),
    }
    assert result.entries == []


def test_quiet_gate_decisions_gates_15m_only_cockpit_row_if_recently_seen() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        cockpit={
            "cols": ["s", "st", "htf", "aligned", "sig"],
            "rows": [["SPY", True, "range", False, ["15m:stretched_up"]]],
        },
        regime_families={},
        active_families={},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={"SPY": "yfinance"},
    )

    assert result.kept_symbols == []
    assert result.gated_symbols == ["SPY"]
    assert result.reasons == {}
    assert result.entries[0]["reason"] == "quiet_gate"


def test_quiet_gate_debounces_persistent_regime_when_last_wake_reason_matches() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        last_wake_reasons={("/tmp/state", "SPY"): ("regime",)},
        last_wake_fingerprints={
            ("/tmp/state", "SPY"): {"regime": "regime:us:up"}
        },
        cockpit={"cols": ["s", "st", "sig"], "rows": [["SPY", False, []]]},
        regime_families={"us": {"dir": "up", "frac": 0.80}},
        active_families={"us": ["SPY"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == []
    assert result.gated_symbols == ["SPY"]
    assert result.reasons == {}
    assert result.persistent_reasons == {}


def test_quiet_gate_decisions_keeps_stretched_when_aligned() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        cockpit={
            "cols": ["s", "st", "aligned", "sig"],
            "rows": [["SPY", True, True, []]],
        },
        regime_families={},
        active_families={},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["SPY"]
    assert result.gated_symbols == []
    assert result.reasons == {"SPY": "signal"}


def test_quiet_gate_debounces_persistent_regime_within_two_hours() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        last_wake_reasons={("/tmp/state", "SPY"): ("regime",)},
        last_wake_fingerprints={
            ("/tmp/state", "SPY"): {"regime": "regime:us:up"}
        },
        cockpit={"cols": ["s", "st", "sig"], "rows": [["SPY", False, []]]},
        regime_families={"us": {"dir": "up", "frac": 0.8}},
        active_families={"us": ["SPY"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == []
    assert result.gated_symbols == ["SPY"]
    assert result.reasons == {}
    assert result.persistent_reasons == {}


def test_quiet_gate_debounces_persistent_htf_signal_within_two_hours() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        last_wake_reasons={("/tmp/state", "SPY"): ("signal",)},
        last_wake_fingerprints={
            ("/tmp/state", "SPY"): {"signal": "signal:1h:breakout_up"}
        },
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, ["1h:breakout_up"]]],
        },
        regime_families={},
        active_families={},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == []
    assert result.gated_symbols == ["SPY"]
    assert result.reasons == {}


def test_quiet_gate_new_htf_signal_after_regime_wake_still_keeps() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        last_wake_reasons={("/tmp/state", "SPY"): ("regime",)},
        last_wake_fingerprints={
            ("/tmp/state", "SPY"): {"regime": "regime:us:up"}
        },
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, ["4h:breakout_up"]]],
        },
        regime_families={"us": {"dir": "up", "frac": 0.8}},
        active_families={"us": ["SPY"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["SPY"]
    assert result.gated_symbols == []
    assert result.reasons == {"SPY": "signal"}
    assert result.persistent_reasons == {"SPY": ("regime", "signal")}
    assert result.persistent_fingerprints == {
        "SPY": {
            "regime": "regime:us:up",
            "signal": "signal:4h:breakout_up",
        }
    }


def test_quiet_gate_both_persistent_reasons_stay_quiet() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        last_wake_reasons={("/tmp/state", "SPY"): ("regime", "signal")},
        last_wake_fingerprints={
            ("/tmp/state", "SPY"): {
                "regime": "regime:us:up",
                "signal": "signal:4h:breakout_up",
            }
        },
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, ["4h:breakout_up"]]],
        },
        regime_families={"us": {"dir": "up", "frac": 0.8}},
        active_families={"us": ["SPY"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == []
    assert result.gated_symbols == ["SPY"]
    assert result.reasons == {}
    assert result.persistent_reasons == {}


def test_quiet_gate_records_both_persistent_reasons_on_regime_keep() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={("/tmp/state", "SPY"): NOW - timedelta(hours=1)},
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, ["4h:breakout_up"]]],
        },
        regime_families={"us": {"dir": "up", "frac": 0.8}},
        active_families={"us": ["SPY"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols=set(),
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["SPY"]
    assert result.reasons == {"SPY": "regime"}
    assert result.persistent_reasons == {"SPY": ("regime", "signal")}
    assert result.persistent_fingerprints == {
        "SPY": {
            "regime": "regime:us:up",
            "signal": "signal:4h:breakout_up",
        }
    }


def test_quiet_gate_position_reveille_sur_inversion_signal_htf() -> None:
    key = ("/tmp/state", "SPY")
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={key: NOW - timedelta(minutes=30)},
        last_wake_reasons={key: ("signal",)},
        last_wake_fingerprints={key: {"signal": "signal:1h:breakout_up"}},
        cockpit={
            "cols": ["s", "st", "sig"],
            "rows": [["SPY", False, ["1h:breakout_down"]]],
        },
        regime_families={},
        active_families={},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols={"SPY"},
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["SPY"]
    assert result.reasons == {"SPY": "signal"}


def test_quiet_gate_position_reveille_sur_inversion_regime() -> None:
    key = ("/tmp/state", "SPY")
    result = quiet_gate_decisions(
        symbols=["SPY"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={key: NOW - timedelta(minutes=30)},
        last_wake_reasons={key: ("regime",)},
        last_wake_fingerprints={key: {"regime": "regime:us:up"}},
        cockpit={"cols": ["s", "st", "sig"], "rows": [["SPY", False, []]]},
        regime_families={"us": {"dir": "down", "frac": 0.8}},
        active_families={"us": ["SPY"]},
        wake_source=WakeSourceStub(set()),
        triggers_by_symbol={},
        held_symbols={"SPY"},
        runtime_data_source_by_sym={},
    )

    assert result.kept_symbols == ["SPY"]
    assert result.reasons == {"SPY": "regime"}


def test_stale_market_hold_decision_records_first_stale_entry() -> None:
    result = stale_market_hold_decision(
        symbol="SPY",
        stale_data={
            "last_bar_ts": "2026-07-04T11:00:00+00:00",
            "stale_reason": "too_old",
            "data_age_minutes": 60.0,
        },
        current_streak=0,
        default_wake_minutes=30.0,
        now=NOW,
        clamp_wake_to_session_open=lambda minutes, *, now, symbol: minutes,
        stale_armed_plan=None,
        runtime_data_source="yfinance",
    )

    assert result.new_streak == 1
    assert result.wake_minutes == pytest.approx(30.0)
    assert result.event is None
    assert result.entry == {
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.0,
        "rationale": "stale_market_data",
        "next_wake_in_minutes": 30.0,
        "intent": "HOLD",
        "trade_plan_created": False,
        "executed": False,
        "reason": "stale_market_data",
        "decision_reason_code": "DATA_STALE",
        "decision_source": "infra",
        "model_called": False,
        "stale_streak": 1,
        "data_source": "yfinance",
        "last_bar_ts": "2026-07-04T11:00:00+00:00",
        "stale_reason": "too_old",
        "data_age_minutes": 60.0,
    }


def test_stale_market_hold_decision_emits_backoff_event_after_first_stale() -> None:
    result = stale_market_hold_decision(
        symbol="SPY",
        stale_data={"stale_reason": "too_old", "data_age_minutes": 90.0},
        current_streak=2,
        default_wake_minutes=30.0,
        now=NOW,
        clamp_wake_to_session_open=lambda minutes, *, now, symbol: minutes,
        stale_armed_plan=None,
        runtime_data_source=None,
    )

    assert result.new_streak == 3
    assert result.wake_minutes == pytest.approx(120.0)
    assert result.entry is None
    assert result.event is not None
    assert result.event.name == "stale_backoff"
    assert result.event.payload == {
        "symbol": "SPY",
        "streak": 3,
        "next_wake_minutes": 120.0,
        "stale_reason": "too_old",
        "data_age_minutes": 90.0,
    }


def test_stale_market_hold_decision_records_armed_stale_even_during_backoff() -> None:
    result = stale_market_hold_decision(
        symbol="SPY",
        stale_data={"stale_reason": "runtime_old"},
        current_streak=2,
        default_wake_minutes=30.0,
        now=NOW,
        clamp_wake_to_session_open=lambda minutes, *, now, symbol: 45.0,
        stale_armed_plan={"id": "SPY:abc123", "order": {"action": "BUY"}},
        runtime_data_source="ib",
    )

    assert result.new_streak == 3
    assert result.wake_minutes == pytest.approx(45.0)
    assert result.event is None
    assert result.entry is not None
    assert result.entry["armed_plan_id"] == "SPY:abc123"
    assert result.entry["armed_plan_order"] == {"action": "BUY"}
    assert result.entry["stale_streak"] == 3
    assert result.entry["data_source"] == "ib"
