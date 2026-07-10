from __future__ import annotations

from datetime import datetime, timezone

from trader.agent.protocol.types import Decision
from trader.application.cycle.decision_scope import (
    DecisionScopeRequest,
    prepare_decision_scope,
)
from trader.application.cycle.infra_holds import QuietGateResult
from trader.application.exit.armed_plans import ArmedPlanResolution


NOW = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)


def _reference_volatility(
    _symbol: str,
    *,
    entry_price: float,
    cockpit: dict,
    tradable_bars_by_symbol: dict[str, list],
) -> float | None:
    del entry_price, cockpit, tradable_bars_by_symbol
    return None


def _request(**overrides) -> DecisionScopeRequest:
    payload = {
        "symbols_to_decide": ["FRESH", "STALE", "BLOCKED", "NO_PRICE"],
        "prices": {"FRESH": 100.0, "STALE": 90.0, "BLOCKED": 80.0},
        "stale_market_data": {
            "STALE": {"stale_reason": "runtime_stale"},
            "BLOCKED": {"stale_reason": "runtime_stale"},
        },
        "execution_eligibility": {
            "STALE": {"planning": {"enabled": True}},
            "BLOCKED": {"planning": {"enabled": False}},
        },
        "held_symbols": set(),
        "indicator_triggers": [],
        "positions": {},
        "cockpit": {},
        "tradable_bars_by_symbol": {"FRESH": ["fresh-runtime"]},
        "daily_bars_by_symbol": {
            "STALE": ["stale-daily"],
            "BLOCKED": ["blocked-daily"],
        },
        "now": NOW,
        "state_key": "/state",
        "last_llm_at": {},
        "regime_families": {},
        "active_families": {},
        "wake_source": None,
        "triggers_by_symbol": {},
        "runtime_data_source_by_sym": {"FRESH": "ib", "STALE": "ib"},
        "runtime_interval": "15m",
        "daily_interval": "1d",
        "reference_volatility_for_symbol": _reference_volatility,
    }
    payload.update(overrides)
    return DecisionScopeRequest(**payload)


def test_prepare_decision_scope_keeps_fresh_and_plannable_stale_symbols() -> None:
    result = prepare_decision_scope(_request())

    assert result.decidable == ["FRESH", "STALE"]
    assert result.armed_resolution.decisions == {}
    assert result.quiet_gate.gated_symbols == []
    assert result.analysis_bars_by_symbol == {
        "FRESH": ["fresh-runtime"],
        "STALE": ["stale-daily"],
    }
    assert result.analysis_timeframe_by_symbol == {
        "FRESH": "15m",
        "STALE": "1d",
    }
    assert result.analysis_symbols == ["FRESH", "STALE"]


def test_prepare_decision_scope_composes_armed_and_quiet_gates_before_bar_scope() -> None:
    armed_resolution = ArmedPlanResolution(
        decisions={"ARMED": Decision.hold("ARMED", "armed")},
        plan_ids={"ARMED": "plan-1"},
        plan_orders={},
        stale_plans={},
        reference_volatilities={},
        events=[],
        progress_logs=[],
    )
    captured: dict[str, object] = {}

    def resolve_armed(**kwargs):
        captured["armed_symbols"] = kwargs["symbols_to_decide"]
        captured["positions"] = kwargs["positions"]
        return armed_resolution

    def quiet_gate(**kwargs):
        captured["quiet_symbols"] = kwargs["symbols"]
        return QuietGateResult(
            kept_symbols=["FRESH", "STALE", "HELD"],
            gated_symbols=["QUIET"],
            entries=[{"symbol": "QUIET", "reason": "quiet_gate"}],
        )

    request = _request(
        symbols_to_decide=[
            "FRESH",
            "STALE",
            "HELD",
            "BLOCKED",
            "NO_PRICE",
            "ARMED",
            "QUIET",
        ],
        prices={
            "FRESH": 100.0,
            "STALE": 90.0,
            "HELD": 85.0,
            "BLOCKED": 80.0,
            "ARMED": 70.0,
            "QUIET": 60.0,
        },
        stale_market_data={
            "STALE": {},
            "HELD": {},
            "BLOCKED": {},
        },
        execution_eligibility={
            "STALE": {"planning": {"enabled": True}},
            "HELD": {"planning": {"enabled": False}},
            "BLOCKED": {"planning": {"enabled": False}},
        },
        held_symbols={"HELD"},
        positions={"HELD": object()},
        tradable_bars_by_symbol={
            "FRESH": ["fresh-runtime"],
            "ARMED": ["armed-runtime"],
            "QUIET": ["quiet-runtime"],
        },
        daily_bars_by_symbol={
            "STALE": ["stale-daily"],
            "HELD": ["held-daily"],
        },
    )

    result = prepare_decision_scope(
        request,
        armed_plan_resolver=resolve_armed,
        quiet_gate=quiet_gate,
    )

    assert captured == {
        "armed_symbols": request.symbols_to_decide,
        "positions": request.positions,
        "quiet_symbols": ["FRESH", "STALE", "HELD", "QUIET"],
    }
    assert result.decidable == ["FRESH", "STALE", "HELD"]
    assert result.armed_resolution is armed_resolution
    assert result.quiet_gate.gated_symbols == ["QUIET"]
    assert result.analysis_bars_by_symbol == {
        "FRESH": ["fresh-runtime"],
        "ARMED": ["armed-runtime"],
        "QUIET": ["quiet-runtime"],
        "STALE": ["stale-daily"],
        "HELD": ["held-daily"],
    }
    assert result.analysis_timeframe_by_symbol["ARMED"] == "15m"
    assert result.analysis_timeframe_by_symbol["STALE"] == "1d"
