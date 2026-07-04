from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trader.application.infra_holds import quiet_gate_decisions


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


def test_quiet_gate_decisions_keeps_agent_wake_and_position_symbols() -> None:
    result = quiet_gate_decisions(
        symbols=["SPY", "QQQ"],
        now=NOW,
        state_key="/tmp/state",
        last_llm_at={
            ("/tmp/state", "SPY"): NOW - timedelta(hours=1),
            ("/tmp/state", "QQQ"): NOW - timedelta(hours=1),
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
    assert result.entries == []


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
                ["SIGNAL", True, ["breakout"]],
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
    assert result.entries == []
