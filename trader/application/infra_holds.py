"""Application helpers for infrastructure-authored HOLD decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from trader.application import cycle_schedule
from trader.planning import relevance_gate


class SymbolWakeSource(Protocol):
    def symbols_with_wake(self) -> set[str]: ...


class SessionWakeClamp(Protocol):
    def __call__(self, wake_minutes: float, *, now: datetime, symbol: str) -> float: ...


@dataclass(frozen=True)
class QuietGateResult:
    kept_symbols: list[str]
    gated_symbols: list[str]
    entries: list[dict]


@dataclass(frozen=True)
class InfraHoldEvent:
    name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class StaleMarketHoldResult:
    new_streak: int
    wake_minutes: float
    entry: dict | None
    event: InfraHoldEvent | None


def quiet_gate_decisions(
    *,
    symbols: list[str],
    now: datetime,
    state_key: str,
    last_llm_at: Mapping[tuple[str, str], datetime],
    cockpit: dict,
    regime_families: Mapping[str, dict],
    active_families: Mapping[str, list[str]],
    wake_source: SymbolWakeSource | None,
    triggers_by_symbol: Mapping[str, list],
    held_symbols: set[str],
    runtime_data_source_by_sym: Mapping[str, object],
) -> QuietGateResult:
    """Split decidable symbols into LLM-needed and quiet infra-HOLD entries."""
    activity = relevance_gate.cockpit_activity(cockpit)
    strong_families = {family for family, bias in regime_families.items() if (bias.get("frac") or 0.0) >= 0.70}
    family_of = {member: family for family, members in active_families.items() for member in members}
    agent_wakes = wake_source.symbols_with_wake() if wake_source is not None else set()

    gated_symbols: list[str] = []
    kept_symbols: list[str] = []
    entries: list[dict] = []
    for symbol in symbols:
        last_seen = last_llm_at.get((state_key, symbol))
        hours = None if last_seen is None else (now - last_seen).total_seconds() / 3600.0
        activity_for_symbol = activity.get(symbol) or {}
        needed, _gate_reason = relevance_gate.symbol_needs_llm(
            agent_requested_wake=symbol in agent_wakes,
            has_trigger=bool(triggers_by_symbol.get(symbol)),
            has_position=symbol in held_symbols,
            family_regime_strong=family_of.get(symbol) in strong_families,
            stretched=activity_for_symbol.get("stretched"),
            sig=activity_for_symbol.get("sig"),
            hours_since_last_llm=hours,
        )
        if needed:
            kept_symbols.append(symbol)
            continue

        gated_symbols.append(symbol)
        entries.append(
            {
                "symbol": symbol,
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
                "data_source": runtime_data_source_by_sym.get(symbol),
            }
        )

    return QuietGateResult(
        kept_symbols=kept_symbols,
        gated_symbols=gated_symbols,
        entries=entries,
    )


def stale_market_hold_decision(
    *,
    symbol: str,
    stale_data: Mapping[str, Any],
    current_streak: int,
    default_wake_minutes: float,
    now: datetime,
    clamp_wake_to_session_open: SessionWakeClamp,
    stale_armed_plan: Mapping[str, Any] | None,
    runtime_data_source: object,
) -> StaleMarketHoldResult:
    """Build the infra HOLD or lightweight backoff event for non-decidable stale data."""
    wake_minutes = cycle_schedule.stale_backoff_wake_minutes(
        current_streak,
        default_wake_minutes=default_wake_minutes,
    )
    wake_minutes = clamp_wake_to_session_open(wake_minutes, now=now, symbol=symbol)
    new_streak = current_streak + 1

    should_record_stale = current_streak == 0 or stale_armed_plan is not None
    if should_record_stale:
        entry = {
            "symbol": symbol,
            **(
                {
                    "armed_plan_id": stale_armed_plan["id"],
                    "armed_plan_order": stale_armed_plan["order"],
                }
                if stale_armed_plan is not None
                else {}
            ),
            "action": "HOLD",
            "qty": 0.0,
            "confidence": 0.0,
            "rationale": "stale_market_data",
            "next_wake_in_minutes": wake_minutes,
            "intent": "HOLD",
            "trade_plan_created": False,
            "executed": False,
            "reason": "stale_market_data",
            "decision_reason_code": "DATA_STALE",
            "decision_source": "infra",
            "model_called": False,
            "stale_streak": new_streak,
            "data_source": runtime_data_source,
            **dict(stale_data),
        }
        return StaleMarketHoldResult(
            new_streak=new_streak,
            wake_minutes=wake_minutes,
            entry=entry,
            event=None,
        )

    return StaleMarketHoldResult(
        new_streak=new_streak,
        wake_minutes=wake_minutes,
        entry=None,
        event=InfraHoldEvent(
            "stale_backoff",
            {
                "symbol": symbol,
                "streak": new_streak,
                "next_wake_minutes": wake_minutes,
                "stale_reason": stale_data.get("stale_reason"),
                "data_age_minutes": stale_data.get("data_age_minutes"),
            },
        ),
    )
