"""Deterministic enforcement of persisted trade exit plans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .tools.execution import Side
from .trade_plan import ProfitProtection, TakeProfit, TradePlan


@dataclass(frozen=True)
class ExitSignal:
    symbol: str
    side: Side
    quantity: float
    reason: str


@dataclass(frozen=True)
class ExitEvaluation:
    updated_plan: TradePlan
    signal: ExitSignal | None = None
    close_plan: bool = False


def _exit_side(plan: TradePlan) -> Side:
    return "SELL" if plan.side == "LONG" else "BUY"


def _triggered_stop(plan: TradePlan, price: float) -> bool:
    if plan.hard_stop_price is None:
        return False
    return price <= plan.hard_stop_price if plan.side == "LONG" else price >= plan.hard_stop_price


def _triggered_tp(plan: TradePlan, tp: TakeProfit, price: float) -> bool:
    return price >= tp.price if plan.side == "LONG" else price <= tp.price


def _next_take_profit(plan: TradePlan, price: float) -> TakeProfit | None:
    for tp in plan.take_profits:
        if tp.name in plan.filled_take_profits:
            continue
        if _triggered_tp(plan, tp, price):
            return tp
    return None


def _max_hold_due(plan: TradePlan, now: datetime) -> bool:
    if plan.max_hold_minutes is None:
        return False
    opened_at = datetime.fromisoformat(plan.opened_at)
    return (now - opened_at).total_seconds() >= plan.max_hold_minutes * 60.0


def _update_watermarks(plan: TradePlan, price: float) -> TradePlan:
    high = price if plan.high_watermark is None else max(plan.high_watermark, price)
    low = price if plan.low_watermark is None else min(plan.low_watermark, price)
    return replace(plan, high_watermark=high, low_watermark=low)


def _trailing_enabled(plan: TradePlan) -> bool:
    trailing = plan.trailing_stop
    if trailing is None:
        return False
    if trailing.enabled_after is None:
        return True
    return trailing.enabled_after in plan.filled_take_profits


def _trail_amount(plan: TradePlan) -> float | None:
    trailing = plan.trailing_stop
    if trailing is None:
        return None
    if trailing.trail_type == "price":
        return trailing.trail_value
    if trailing.trail_type == "percent":
        return plan.entry_price * trailing.trail_value
    return trailing.trail_value


def _trailing_stop_hit(plan: TradePlan, price: float) -> bool:
    if not _trailing_enabled(plan):
        return False
    amount = _trail_amount(plan)
    if amount is None:
        return False
    if plan.side == "LONG":
        watermark = plan.high_watermark if plan.high_watermark is not None else plan.entry_price
        return price <= watermark - amount
    watermark = plan.low_watermark if plan.low_watermark is not None else plan.entry_price
    return price >= watermark + amount


def _risk_per_share(plan: TradePlan) -> float | None:
    if plan.hard_stop_price is None:
        return None
    risk = abs(plan.hard_stop_price - plan.entry_price)
    return risk if risk > 0 else None


def _favorable_move(plan: TradePlan, price: float) -> float:
    if plan.side == "LONG":
        return price - plan.entry_price
    return plan.entry_price - price


def _best_favorable_move(plan: TradePlan) -> float:
    if plan.side == "LONG":
        watermark = plan.high_watermark if plan.high_watermark is not None else plan.entry_price
        return _favorable_move(plan, watermark)
    watermark = plan.low_watermark if plan.low_watermark is not None else plan.entry_price
    return _favorable_move(plan, watermark)


def _min_hold_elapsed(plan: TradePlan, protection: ProfitProtection, now: datetime) -> bool:
    opened_at = datetime.fromisoformat(plan.opened_at)
    return (now - opened_at).total_seconds() >= protection.min_hold_minutes * 60.0


def _breakeven_stop(plan: TradePlan) -> float:
    if plan.hard_stop_price is None:
        return plan.entry_price
    if plan.side == "LONG":
        return max(plan.hard_stop_price, plan.entry_price)
    return min(plan.hard_stop_price, plan.entry_price)


def _profit_protection_signal(plan: TradePlan, *, price: float, now: datetime) -> ExitEvaluation | None:
    protection = plan.profit_protection
    if protection is None or not protection.enabled or protection.triggered:
        return None
    risk = _risk_per_share(plan)
    if risk is None or not _min_hold_elapsed(plan, protection, now):
        return None

    best_favorable = _best_favorable_move(plan)
    if best_favorable < protection.arm_at_r * risk:
        return None
    current_favorable = max(0.0, _favorable_move(plan, price))
    if best_favorable <= 0:
        return None
    giveback = (best_favorable - current_favorable) / best_favorable
    if giveback < protection.trigger_on_giveback_pct:
        return None

    qty = min(plan.remaining_quantity, plan.quantity * protection.close_fraction)
    if qty <= 0:
        return None
    remaining = max(0.0, plan.remaining_quantity - qty)
    hard_stop = plan.hard_stop_price
    if protection.move_stop_to == "breakeven":
        hard_stop = _breakeven_stop(plan)
    updated = replace(
        plan,
        remaining_quantity=remaining,
        hard_stop_price=hard_stop,
        profit_protection=replace(protection, triggered=True),
    )
    return ExitEvaluation(
        updated_plan=updated,
        signal=_signal(plan, qty, "profit_protection"),
        close_plan=remaining <= 0,
    )


def _signal(plan: TradePlan, quantity: float, reason: str) -> ExitSignal:
    return ExitSignal(
        symbol=plan.symbol,
        side=_exit_side(plan),
        quantity=round(quantity, 8),
        reason=reason,
    )


def _close(plan: TradePlan, reason: str) -> ExitEvaluation:
    updated = replace(plan, remaining_quantity=0.0)
    return ExitEvaluation(
        updated_plan=updated,
        signal=_signal(plan, plan.remaining_quantity, reason),
        close_plan=True,
    )


def evaluate_plan(plan: TradePlan, *, price: float, now: datetime) -> ExitEvaluation:
    """Evaluate a single plan at the current price.

    Priority is defensive: hard stop, max hold, take profits, trailing stop.
    """
    plan = _update_watermarks(plan, price)
    if plan.remaining_quantity <= 0:
        return ExitEvaluation(updated_plan=plan, close_plan=True)

    if _triggered_stop(plan, price):
        return _close(plan, "hard_stop")

    if _max_hold_due(plan, now):
        return _close(plan, "max_hold")

    tp = _next_take_profit(plan, price)
    if tp is not None:
        close_after_fill = tp.after_fill == "close"
        qty = plan.remaining_quantity if close_after_fill else min(tp.quantity, plan.remaining_quantity)
        remaining = max(0.0, plan.remaining_quantity - qty)
        filled = [*plan.filled_take_profits, tp.name]
        hard_stop = plan.hard_stop_price
        if tp.after_fill == "move_stop_to_breakeven":
            hard_stop = plan.entry_price
        updated = replace(
            plan,
            remaining_quantity=remaining,
            filled_take_profits=filled,
            hard_stop_price=hard_stop,
        )
        return ExitEvaluation(
            updated_plan=updated,
            signal=_signal(plan, qty, f"take_profit:{tp.name}"),
            close_plan=remaining <= 0,
        )

    protected = _profit_protection_signal(plan, price=price, now=now)
    if protected is not None:
        return protected

    if _trailing_stop_hit(plan, price):
        return _close(plan, "trailing_stop")

    return ExitEvaluation(updated_plan=plan)
