"""Application service for deterministic planned exits."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Protocol

from trader.planning.protocols import TradePlanStoreLike
from trader.market.execution_eligibility import (
    execution_blocked_reason as default_execution_blocked_reason,
)
from trader.application.exit.exit_bars import exit_bar_extremes
from trader.application.execute.order_admission import clamp_exit_quantity as default_clamp_exit_quantity
from trader.execution import portfolio
from trader.domain.contracts import Order
from trader.planning.exit_engine import ExitEvaluation, evaluate_plan
from trader.planning.trade_plan import TradePlan

ModelPerformanceAppender = Callable[..., None]
RateFn = Callable[[str], float]
EvaluatePlanFn = Callable[..., ExitEvaluation]
ClampExitQuantityFn = Callable[..., tuple[float, str | None]]
ExecutionBlockedReasonFn = Callable[..., str | None]
PlanSnapshotFn = Callable[[TradePlan], dict]


class BrokerLike(Protocol):
    def positions(self) -> dict: ...

    def submit(
        self,
        order: Order,
        price: float,
        ts: str,
        *,
        dry_run: bool = False,
        fx_rate: float = 1.0,
    ): ...


def _noop_model_performance(**_payload: object) -> None:
    return None


def plan_snapshot(plan: TradePlan) -> dict:
    """Minimal JSON-serialisable snapshot of a plan at exit time."""
    return {
        "id": plan.id,
        "symbol": plan.symbol,
        "side": plan.side,
        "entry_price": plan.entry_price,
        "reference_volatility": plan.reference_volatility,
        "quantity": plan.quantity,
        "remaining_quantity": plan.remaining_quantity,
        "hard_stop_price": plan.hard_stop_price,
        "take_profits": [
            {"name": tp.name, "price": tp.price, "fraction": tp.fraction}
            for tp in plan.take_profits
        ],
        "trailing_stop": (
            {
                "trail_type": plan.trailing_stop.trail_type,
                "trail_value": plan.trailing_stop.trail_value,
                "enabled_after": plan.trailing_stop.enabled_after,
                "trail_floored": plan.trailing_stop.trail_floored,
            }
            if plan.trailing_stop is not None
            else None
        ),
        "max_hold_minutes": plan.max_hold_minutes,
        "filled_take_profits": list(plan.filled_take_profits),
        "high_watermark": plan.high_watermark,
        "low_watermark": plan.low_watermark,
    }


def apply_planned_exits(
    *,
    broker: BrokerLike,
    plan_store: TradePlanStoreLike,
    prices: dict[str, float],
    bars_by_symbol: dict[str, list] | None = None,
    bars_intervals_by_symbol: dict[str, str] | None = None,
    valuation_prices: dict[str, float] | None = None,
    now: datetime,
    dry_run: bool,
    starting_equity: float,
    execution_eligibility: dict[str, dict] | None = None,
    rate_fn: RateFn | None = None,
    runtime_interval: str,
    exit_check_interval: str,
    exit_check_window_bars: int,
    append_model_performance: ModelPerformanceAppender | None = None,
    evaluate_plan_fn: EvaluatePlanFn | None = None,
    clamp_exit_quantity_fn: ClampExitQuantityFn | None = None,
    execution_blocked_reason_fn: ExecutionBlockedReasonFn | None = None,
    plan_snapshot_fn: PlanSnapshotFn | None = None,
) -> list[dict]:
    entries: list[dict] = []
    performance_appender = append_model_performance or _noop_model_performance
    intervals_by_symbol = bars_intervals_by_symbol or {}
    evaluate = evaluate_plan_fn or evaluate_plan
    clamp_exit_quantity = clamp_exit_quantity_fn or default_clamp_exit_quantity
    execution_blocked_reason = execution_blocked_reason_fn or default_execution_blocked_reason
    snapshot_plan = plan_snapshot_fn or plan_snapshot

    for plan in plan_store.open_plans():
        price = prices.get(plan.symbol)
        if price is None:
            continue

        bar_high: float | None = None
        bar_low: float | None = None
        interval_used = intervals_by_symbol.get(plan.symbol, runtime_interval)
        if bars_by_symbol is not None:
            bars = bars_by_symbol.get(plan.symbol)
            if bars:
                extremes = exit_bar_extremes(
                    bars,
                    interval=interval_used,
                    fine_interval=exit_check_interval,
                    fine_window_bars=exit_check_window_bars,
                    opened_at=plan.opened_at,
                )
                bar_high = extremes.high
                bar_low = extremes.low

        evaluation = evaluate(plan, price=price, bar_high=bar_high, bar_low=bar_low, now=now)
        if evaluation.signal is None:
            if not dry_run:
                plan_store.upsert(evaluation.updated_plan)
            continue

        requested_quantity = evaluation.signal.quantity
        position = broker.positions().get(plan.symbol)
        clamped_quantity, exit_block_reason = clamp_exit_quantity(
            intent="CLOSE",
            action=evaluation.signal.side,
            quantity=requested_quantity,
            position_quantity=0.0 if position is None else position.quantity,
        )
        if exit_block_reason is not None or clamped_quantity <= 0:
            if not dry_run:
                plan_store.close(plan.id)
            blocked_fill = (
                evaluation.signal.fill_price if evaluation.signal.fill_price is not None else price
            )
            entries.append(
                {
                    "symbol": plan.symbol,
                    "side": evaluation.signal.side,
                    "quantity": clamped_quantity,
                    "requested_quantity": requested_quantity,
                    "reason": exit_block_reason or "zero_exit_quantity",
                    "price": price,
                    "fill_price": blocked_fill,
                    "plan_snapshot": snapshot_plan(plan),
                    "bars_interval": interval_used,
                    "executed": False,
                    "dry_run": dry_run,
                }
            )
            continue

        exit_execution_blocked = execution_blocked_reason(
            execution_eligibility or {},
            plan.symbol,
        )
        if exit_execution_blocked is not None:
            entries.append(
                {
                    "symbol": plan.symbol,
                    "side": evaluation.signal.side,
                    "quantity": clamped_quantity,
                    "reason": exit_execution_blocked,
                    "price": price,
                    "executed": False,
                    "dry_run": dry_run,
                }
            )
            continue

        effective_fill_price = evaluation.signal.fill_price if evaluation.signal.fill_price is not None else price

        order = Order(
            symbol=evaluation.signal.symbol,
            side=evaluation.signal.side,
            quantity=clamped_quantity,
            rationale=evaluation.signal.reason,
        )
        fill_rate = rate_fn(plan.symbol) if rate_fn is not None else 1.0
        fill = broker.submit(
            order,
            effective_fill_price,
            now.isoformat(),
            dry_run=dry_run,
            fx_rate=fill_rate,
        )
        if not dry_run:
            final_position = broker.positions().get(plan.symbol)
            final_quantity = 0.0 if final_position is None else final_position.quantity
            if final_quantity == 0:
                plan_store.close_symbol(plan.symbol)
            elif evaluation.close_plan:
                plan_store.close(plan.id)
                plan_store.sync_symbol_quantity(plan.symbol, abs(final_quantity))
            else:
                plan_store.upsert(evaluation.updated_plan)
            plan_store.sync_symbol_quantity(plan.symbol, abs(final_quantity))
            if fill is not None:
                price_map = valuation_prices if valuation_prices is not None else prices
                latest = portfolio.snapshot(
                    broker,
                    lambda s: price_map.get(s, 0.0),
                    starting_equity,
                    fx_rate_of=rate_fn,
                )
                performance_appender(
                    ts=fill.ts,
                    symbol=plan.symbol,
                    action=evaluation.signal.side,
                    intent="PLANNED_EXIT",
                    exit_reason=evaluation.signal.reason,
                    source_plan_id=plan.id,
                    quantity=clamped_quantity,
                    price=effective_fill_price,
                    commission=fill.commission,
                    commission_currency=fill.commission_currency,
                    commission_model=fill.commission_model,
                    fx_rate=fill.fx_rate,
                    confidence=plan.llm_confidence,
                    llm_provider=plan.llm_provider or "unknown",
                    llm_model=plan.llm_model or "unknown",
                    llm_fallback_reason=plan.llm_fallback_reason,
                    equity=latest.equity,
                    cash=latest.cash,
                    position_quantity=final_quantity,
                )
        entries.append(
            {
                "symbol": plan.symbol,
                "side": evaluation.signal.side,
                "quantity": clamped_quantity,
                "requested_quantity": requested_quantity,
                "reason": evaluation.signal.reason,
                "price": price,
                "fill_price": effective_fill_price,
                "plan_snapshot": snapshot_plan(plan),
                "bars_interval": interval_used,
                "executed": fill is not None,
                "dry_run": dry_run,
                **(
                    {
                        "commission": fill.commission,
                        "commission_currency": fill.commission_currency,
                        "commission_model": fill.commission_model,
                        "fx_rate": fill.fx_rate,
                    }
                    if fill is not None
                    else {}
                ),
            }
        )
    return entries
