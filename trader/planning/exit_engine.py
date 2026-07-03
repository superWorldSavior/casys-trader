"""Deterministic enforcement of persisted trade exit plans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from trader.domain.orders import Side
from .trade_plan import ProfitProtection, TakeProfit, TradePlan


@dataclass(frozen=True)
class ExitSignal:
    symbol: str
    side: Side
    quantity: float
    reason: str
    fill_price: float | None = None


@dataclass(frozen=True)
class ExitEvaluation:
    updated_plan: TradePlan
    signal: ExitSignal | None = None
    close_plan: bool = False


def _exit_side(plan: TradePlan) -> Side:
    return "SELL" if plan.side == "LONG" else "BUY"


def _check_stop(
    plan: TradePlan,
    price: float,
    bar_high: float | None,
    bar_low: float | None,
) -> tuple[bool, float | None]:
    """Return (triggered, fill_price).

    Fill price is conservative: never better than the stop level.
    - LONG: triggered if min(price, bar_low) <= hard_stop. fill = min(hard_stop, price).
    - SHORT: triggered if max(price, bar_high) >= hard_stop. fill = max(hard_stop, price).
    """
    if plan.hard_stop_price is None:
        return False, None
    stop = plan.hard_stop_price
    if plan.side == "LONG":
        effective_low = price if bar_low is None else min(price, bar_low)
        if effective_low <= stop:
            return True, min(stop, price)
        return False, None
    else:  # SHORT
        effective_high = price if bar_high is None else max(price, bar_high)
        if effective_high >= stop:
            return True, max(stop, price)
        return False, None


def _check_tp(
    plan: TradePlan,
    tp: TakeProfit,
    price: float,
    bar_high: float | None,
    bar_low: float | None,
) -> tuple[bool, float | None]:
    """Return (triggered, fill_price) for a take-profit level.

    Sémantique limit: TP is triggered when the extreme crosses the level.
    Fill = tp.price, unless current price is already more favorable.
    - LONG TP: triggered if max(price, bar_high) >= tp.price. fill = max(tp.price, price)... wait,
      for a limit sell the fill is AT tp.price or better → fill = tp.price unless price > tp.price.
    - SHORT TP: triggered if min(price, bar_low) <= tp.price. fill = tp.price unless price < tp.price.
    """
    if plan.side == "LONG":
        effective_high = price if bar_high is None else max(price, bar_high)
        if effective_high >= tp.price:
            fill = price if price >= tp.price else tp.price
            return True, fill
        return False, None
    else:  # SHORT
        effective_low = price if bar_low is None else min(price, bar_low)
        if effective_low <= tp.price:
            fill = price if price <= tp.price else tp.price
            return True, fill
        return False, None


def _next_take_profit(
    plan: TradePlan,
    price: float,
    bar_high: float | None = None,
    bar_low: float | None = None,
) -> tuple[TakeProfit, float] | None:
    """Return (tp, fill_price) for the first unfilled triggered take-profit, or None."""
    for tp in plan.take_profits:
        if tp.name in plan.filled_take_profits:
            continue
        triggered, fill = _check_tp(plan, tp, price, bar_high, bar_low)
        if triggered:
            return tp, fill  # type: ignore[return-value]
    return None


def _max_hold_due(plan: TradePlan, now: datetime) -> bool:
    if plan.max_hold_minutes is None:
        return False
    opened_at = datetime.fromisoformat(plan.opened_at)
    return (now - opened_at).total_seconds() >= plan.max_hold_minutes * 60.0


def _update_watermarks(
    plan: TradePlan,
    price: float,
    bar_high: float | None = None,
    bar_low: float | None = None,
) -> TradePlan:
    # Bar extremes advance watermarks just like observed prices.
    effective_high = price if bar_high is None else max(price, bar_high)
    effective_low = price if bar_low is None else min(price, bar_low)
    high = effective_high if plan.high_watermark is None else max(plan.high_watermark, effective_high)
    low = effective_low if plan.low_watermark is None else min(plan.low_watermark, effective_low)
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
    if trailing.trail_type == "volatility_multiple":
        if plan.reference_volatility is None:
            return None
        return plan.reference_volatility * trailing.trail_value
    return None


def _check_trailing(
    plan: TradePlan,
    price: float,
    bar_high: float | None = None,
    bar_low: float | None = None,
    guard_plan: TradePlan | None = None,
) -> tuple[bool, float | None]:
    """Return (triggered, fill_price) for the trailing stop.

    Consistent with _check_stop: bar extremes are used to detect intra-bar crosses.
    Fill is conservative — never better than the trail level:
    - LONG: triggered if min(price, bar_low) <= trail_level. fill = min(trail_level, price).
    - SHORT: triggered if max(price, bar_high) >= trail_level. fill = max(trail_level, price).
    """
    if not _trailing_enabled(plan):
        return False, None
    amount = _trail_amount(plan)
    if amount is None:
        return False, None
    if plan.trailing_stop is not None and plan.trailing_stop.enabled_after is None:
        armed_from = plan if guard_plan is None else guard_plan
        if _best_favorable_move(armed_from) < amount:
            return False, None
    if plan.side == "LONG":
        watermark = plan.high_watermark if plan.high_watermark is not None else plan.entry_price
        trail_level = watermark - amount
        effective_low = price if bar_low is None else min(price, bar_low)
        if effective_low <= trail_level:
            return True, min(trail_level, price)
        return False, None
    else:  # SHORT
        watermark = plan.low_watermark if plan.low_watermark is not None else plan.entry_price
        trail_level = watermark + amount
        effective_high = price if bar_high is None else max(price, bar_high)
        if effective_high >= trail_level:
            return True, max(trail_level, price)
        return False, None


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


def _lock_r_stop(plan: TradePlan, lock_r: float) -> float:
    risk = _risk_per_share(plan)
    if risk is None:
        return _breakeven_stop(plan)
    if plan.side == "LONG":
        target = plan.entry_price + lock_r * risk
        return target if plan.hard_stop_price is None else max(plan.hard_stop_price, target)
    target = plan.entry_price - lock_r * risk
    return target if plan.hard_stop_price is None else min(plan.hard_stop_price, target)


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
    if protection.lock_r is not None:
        hard_stop = _lock_r_stop(plan, protection.lock_r)
    elif protection.move_stop_to == "breakeven":
        hard_stop = _breakeven_stop(plan)
    updated = replace(
        plan,
        remaining_quantity=remaining,
        hard_stop_price=hard_stop,
        profit_protection=replace(protection, triggered=True),
    )
    return ExitEvaluation(
        updated_plan=updated,
        signal=_signal(plan, qty, "profit_protection", fill_price=price),
        close_plan=remaining <= 0,
    )


def _signal(plan: TradePlan, quantity: float, reason: str, fill_price: float | None = None) -> ExitSignal:
    return ExitSignal(
        symbol=plan.symbol,
        side=_exit_side(plan),
        quantity=round(quantity, 8),
        reason=reason,
        fill_price=fill_price,
    )


def _close(plan: TradePlan, reason: str, fill_price: float | None = None) -> ExitEvaluation:
    updated = replace(plan, remaining_quantity=0.0)
    return ExitEvaluation(
        updated_plan=updated,
        signal=_signal(plan, plan.remaining_quantity, reason, fill_price=fill_price),
        close_plan=True,
    )


def evaluate_plan(
    plan: TradePlan,
    *,
    price: float,
    bar_high: float | None = None,
    bar_low: float | None = None,
    now: datetime,
) -> ExitEvaluation:
    """Evaluate a single plan at the current price, optionally with bar extremes.

    bar_high / bar_low: high and low of the last completed bar. When provided,
    stops and TPs are also checked against intra-bar extremes so that a spike
    that crosses a level and reverses within the bar is still detected.

    When bar_high/bar_low are None the behaviour is identical to the previous
    price-only evaluation (all existing tests pass unchanged).

    Priority is defensive: hard stop > max hold > take profits > trailing stop.
    When both stop and TP are crossed in the same bar, the order within the bar
    is unknown — we default to stop-first (conservative / reduces loss).
    """
    plan_at_eval_start = plan
    plan = _update_watermarks(plan, price, bar_high=bar_high, bar_low=bar_low)
    if plan.remaining_quantity <= 0:
        return ExitEvaluation(updated_plan=plan, close_plan=True)

    # Hard stop: check against bar extreme as well as current price.
    stop_triggered, stop_fill = _check_stop(plan, price, bar_high, bar_low)
    if stop_triggered:
        return _close(plan, "hard_stop", fill_price=stop_fill)

    if _max_hold_due(plan, now):
        return _close(plan, "max_hold", fill_price=price)

    tp_result = _next_take_profit(plan, price, bar_high=bar_high, bar_low=bar_low)
    if tp_result is not None:
        tp, tp_fill = tp_result
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
            signal=_signal(plan, qty, f"take_profit:{tp.name}", fill_price=tp_fill),
            close_plan=remaining <= 0,
        )

    protected = _profit_protection_signal(plan, price=price, now=now)
    if protected is not None:
        return protected

    trail_triggered, trail_fill = _check_trailing(
        plan,
        price,
        bar_high=bar_high,
        bar_low=bar_low,
        guard_plan=plan_at_eval_start,
    )
    if trail_triggered:
        return _close(plan, "trailing_stop", fill_price=trail_fill)

    return ExitEvaluation(updated_plan=plan)
