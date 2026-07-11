"""Application service for fail-safe exit-plan updates."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Protocol

from trader.domain.planning.exit_plan_spec import InvalidExitPlanError
from trader.domain.planning.trade_plan import apply_exit_update
from trader.domain.trade_plan import TradePlan


class TradePlanStoreLike(Protocol):
    def open_plans(self) -> list[TradePlan]: ...

    def upsert(self, plan: TradePlan) -> None: ...


@dataclass(frozen=True)
class ExitUpdateResult:
    applied: bool
    reason: str | None
    trace: dict
    warnings: list


@dataclass(frozen=True)
class ExitUpdateValidation:
    would_apply: bool
    reason: str | None
    warnings: list


def _current_decision_price(entry: dict) -> float | None:
    try:
        price = float(entry.get("price"))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _hard_stop_warnings(trace: dict) -> list:
    warnings = (trace.get("hard_stop") or {}).get("warnings")
    return copy.deepcopy(warnings) if warnings else []


def apply_exit_update_to_open_plan(
    *,
    plan_store: TradePlanStoreLike,
    symbol: str,
    exit_update: dict,
    bars: list | None,
    entry: dict,
    current_price: float | None = None,
) -> ExitUpdateResult:
    """Apply an exit_update request to the symbol's open plan and trace the result."""
    entry["exit_update"] = copy.deepcopy(exit_update)
    open_plans = [p for p in plan_store.open_plans() if p.symbol == symbol]
    if not open_plans:
        entry["exit_update_applied"] = False
        entry["exit_update_reason"] = "no_open_plan"
        return ExitUpdateResult(applied=False, reason="no_open_plan", trace={}, warnings=[])

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
        reason = f"resolve_failed:{exc}"
        entry["exit_update_applied"] = False
        entry["exit_update_reason"] = reason
        return ExitUpdateResult(applied=False, reason=reason, trace=copy.deepcopy(trace), warnings=[])

    if patched is plan:
        entry["exit_update_applied"] = False
        entry["exit_update_reason"] = "empty_update"
        return ExitUpdateResult(applied=False, reason="empty_update", trace=copy.deepcopy(trace), warnings=[])

    plan_store.upsert(patched)
    entry["exit_update_applied"] = True
    trace_copy = copy.deepcopy(trace)
    warnings = _hard_stop_warnings(trace)
    if trace:
        entry["exit_update_trace"] = trace_copy
        if warnings:
            entry["exit_update_warnings"] = copy.deepcopy(warnings)
    return ExitUpdateResult(applied=True, reason=None, trace=trace_copy, warnings=warnings)


def validate_exit_update(
    *,
    plan_store: TradePlanStoreLike,
    symbol: str,
    exit_update: dict,
    bars: list | None,
    current_price: float | None = None,
) -> ExitUpdateValidation:
    """Dry-run an exit_update request against the symbol's open plan."""
    open_plans = [p for p in plan_store.open_plans() if p.symbol == symbol]
    if not open_plans:
        return ExitUpdateValidation(would_apply=False, reason="no_open_plan", warnings=[])

    plan = open_plans[0]
    trace: dict = {}
    try:
        patched = apply_exit_update(
            plan,
            exit_update,
            bars=bars,
            reference_price=current_price,
            trace_out=trace,
        )
    except (InvalidExitPlanError, ValueError) as exc:
        return ExitUpdateValidation(
            would_apply=False,
            reason=f"resolve_failed:{exc}",
            warnings=_hard_stop_warnings(trace),
        )

    if patched is plan:
        return ExitUpdateValidation(would_apply=False, reason="empty_update", warnings=_hard_stop_warnings(trace))

    return ExitUpdateValidation(would_apply=True, reason=None, warnings=_hard_stop_warnings(trace))
