"""Application service for fail-safe exit-plan amendments."""

from __future__ import annotations

import copy
from typing import Protocol

from trader.planning.trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    apply_amend_exit,
)


class TradePlanStoreLike(Protocol):
    def open_plans(self) -> list[TradePlan]: ...

    def upsert(self, plan: TradePlan) -> None: ...


def apply_amend_exit_to_open_plan(
    *,
    plan_store: TradePlanStoreLike,
    symbol: str,
    amend_exit: dict,
    bars: list | None,
    entry: dict,
) -> None:
    """Apply an amend_exit request to the symbol's open plan and trace the result."""
    entry["amend_exit"] = copy.deepcopy(amend_exit)
    open_plans = [p for p in plan_store.open_plans() if p.symbol == symbol]
    if not open_plans:
        entry["amend_exit_applied"] = False
        entry["amend_exit_reason"] = "no_open_plan"
        return

    plan = open_plans[0]
    trace: dict = {}
    try:
        patched = apply_amend_exit(plan, amend_exit, bars=bars, trace_out=trace)
    except (InvalidExitPlanError, ValueError) as exc:
        entry["amend_exit_applied"] = False
        entry["amend_exit_reason"] = f"resolve_failed:{exc}"
        return

    if patched is plan:
        entry["amend_exit_applied"] = False
        entry["amend_exit_reason"] = "empty_amend"
        return

    plan_store.upsert(patched)
    entry["amend_exit_applied"] = True
    if trace:
        entry["amend_exit_trace"] = copy.deepcopy(trace)
        hard_stop_warnings = (trace.get("hard_stop") or {}).get("warnings")
        if hard_stop_warnings:
            entry["amend_exit_warnings"] = copy.deepcopy(hard_stop_warnings)
