"""Application service that prepares the decision scope for one cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from trader.application.cycle import infra_holds
from trader.application.exit import armed_plans


class ArmedPlanResolver(Protocol):
    def __call__(
        self,
        *,
        indicator_triggers: list[dict],
        symbols_to_decide: list[str],
        prices: Mapping[str, float],
        stale_market_data: Mapping[str, object],
        positions: Mapping[str, armed_plans.PositionLike],
        cockpit: dict,
        tradable_bars_by_symbol: dict[str, list],
        reference_volatility_for_symbol: armed_plans.ReferenceVolatilityProvider,
    ) -> armed_plans.ArmedPlanResolution: ...


class QuietGate(Protocol):
    def __call__(
        self,
        *,
        symbols: list[str],
        now: datetime,
        state_key: str,
        last_llm_at: Mapping[tuple[str, str], datetime],
        cockpit: dict,
        regime_families: Mapping[str, dict],
        active_families: Mapping[str, list[str]],
        wake_source: infra_holds.SymbolWakeSource | None,
        triggers_by_symbol: Mapping[str, list],
        held_symbols: set[str],
        runtime_data_source_by_sym: Mapping[str, object],
    ) -> infra_holds.QuietGateResult: ...


@dataclass(frozen=True)
class DecisionScopeRequest:
    symbols_to_decide: list[str]
    prices: Mapping[str, float]
    stale_market_data: Mapping[str, object]
    execution_eligibility: Mapping[str, dict]
    held_symbols: set[str]
    indicator_triggers: list[dict]
    positions: Mapping[str, armed_plans.PositionLike]
    cockpit: dict
    tradable_bars_by_symbol: dict[str, list]
    daily_bars_by_symbol: dict[str, list]
    now: datetime
    state_key: str
    last_llm_at: Mapping[tuple[str, str], datetime]
    regime_families: Mapping[str, dict]
    active_families: Mapping[str, list[str]]
    wake_source: infra_holds.SymbolWakeSource | None
    triggers_by_symbol: Mapping[str, list]
    runtime_data_source_by_sym: Mapping[str, object]
    runtime_interval: str
    daily_interval: str
    reference_volatility_for_symbol: armed_plans.ReferenceVolatilityProvider


@dataclass(frozen=True)
class DecisionScope:
    decidable: list[str]
    armed_resolution: armed_plans.ArmedPlanResolution
    quiet_gate: infra_holds.QuietGateResult
    analysis_bars_by_symbol: dict[str, list]
    analysis_timeframe_by_symbol: dict[str, str]
    analysis_symbols: list[str]


def prepare_decision_scope(
    request: DecisionScopeRequest,
    *,
    armed_plan_resolver: ArmedPlanResolver = armed_plans.resolve_armed_plan_triggers,
    quiet_gate: QuietGate = infra_holds.quiet_gate_decisions,
) -> DecisionScope:
    """Resolve deterministic plans, relevance gating and analysis-bar coverage."""

    def analysis_eligible(symbol: str) -> bool:
        planning = (request.execution_eligibility.get(symbol) or {}).get(
            "planning"
        ) or {}
        return bool(planning.get("enabled")) or symbol in request.held_symbols

    initially_decidable = [
        symbol
        for symbol in request.symbols_to_decide
        if symbol in request.prices
        and (
            symbol not in request.stale_market_data
            or analysis_eligible(symbol)
        )
    ]

    armed_resolution = armed_plan_resolver(
        indicator_triggers=request.indicator_triggers,
        symbols_to_decide=request.symbols_to_decide,
        prices=request.prices,
        stale_market_data=request.stale_market_data,
        positions=request.positions,
        cockpit=request.cockpit,
        tradable_bars_by_symbol=request.tradable_bars_by_symbol,
        reference_volatility_for_symbol=request.reference_volatility_for_symbol,
    )
    llm_candidates = [
        symbol
        for symbol in initially_decidable
        if symbol not in armed_resolution.decisions
    ]
    quiet_gate_result = quiet_gate(
        symbols=llm_candidates,
        now=request.now,
        state_key=request.state_key,
        last_llm_at=request.last_llm_at,
        cockpit=request.cockpit,
        regime_families=request.regime_families,
        active_families=request.active_families,
        wake_source=request.wake_source,
        triggers_by_symbol=request.triggers_by_symbol,
        held_symbols=request.held_symbols,
        runtime_data_source_by_sym=request.runtime_data_source_by_sym,
    )
    decidable = quiet_gate_result.kept_symbols

    analysis_bars_by_symbol = dict(request.tradable_bars_by_symbol)
    for symbol in decidable:
        if (
            symbol not in analysis_bars_by_symbol
            and symbol in request.daily_bars_by_symbol
        ):
            analysis_bars_by_symbol[symbol] = request.daily_bars_by_symbol[symbol]
    analysis_timeframe_by_symbol = {
        symbol: (
            request.runtime_interval
            if symbol in request.tradable_bars_by_symbol
            else request.daily_interval
        )
        for symbol in analysis_bars_by_symbol
    }

    return DecisionScope(
        decidable=decidable,
        armed_resolution=armed_resolution,
        quiet_gate=quiet_gate_result,
        analysis_bars_by_symbol=analysis_bars_by_symbol,
        analysis_timeframe_by_symbol=analysis_timeframe_by_symbol,
        analysis_symbols=sorted(analysis_bars_by_symbol),
    )


__all__ = ["DecisionScope", "DecisionScopeRequest", "prepare_decision_scope"]
