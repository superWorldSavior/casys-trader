"""Application service for fail-safe exit-plan updates."""

from __future__ import annotations

import copy
from typing import Protocol

from trader.planning.trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    apply_exit_update,
)


class TradePlanStoreLike(Protocol):
    def open_plans(self) -> list[TradePlan]: ...

    def upsert(self, plan: TradePlan) -> None: ...


def _current_decision_price(entry: dict) -> float | None:
    try:
        price = float(entry.get("price"))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def apply_exit_update_to_open_plan(
    *,
    plan_store: TradePlanStoreLike,
    symbol: str,
    exit_update: dict,
    bars: list | None,
    entry: dict,
    current_price: float | None = None,
) -> None:
    """Apply an exit_update request to the symbol's open plan and trace the result."""
    entry["exit_update"] = copy.deepcopy(exit_update)
    open_plans = [p for p in plan_store.open_plans() if p.symbol == symbol]
    if not open_plans:
        entry["exit_update_applied"] = False
        entry["exit_update_reason"] = "no_open_plan"
        return

    plan = open_plans[0]
    trace: dict = {}
    try:
        patched = apply_exit_update(
            plan,
            exit_update,
            bars=bars,
            reference_price=current_price if current_price is not None else _current_decision_price(entry),
            trace_out=trace,
        )
    except (InvalidExitPlanError, ValueError) as exc:
        entry["exit_update_applied"] = False
        entry["exit_update_reason"] = f"resolve_failed:{exc}"
        return

    if patched is plan:
        entry["exit_update_applied"] = False
        entry["exit_update_reason"] = "empty_update"
        return

    plan_store.upsert(patched)
    entry["exit_update_applied"] = True
    if trace:
        entry["exit_update_trace"] = copy.deepcopy(trace)
        hard_stop_warnings = (trace.get("hard_stop") or {}).get("warnings")
        if hard_stop_warnings:
            entry["exit_update_warnings"] = copy.deepcopy(hard_stop_warnings)
