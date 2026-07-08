"""Armed entry orders attached to indicator watches."""

from __future__ import annotations

import math

from .exit_plan_spec import InvalidExitPlanError, normalize_exit_plan, validate_exit_plan

_ARMABLE_INTENTS = {"OPEN_LONG": "BUY", "OPEN_SHORT": "SELL"}

# Armed plan TTL cap = 240 min, aligned with the guaranteed periodic gate
# review (4 h). Watch expiration is silent, so a shorter TTL would force
# re-arming wakes or leave unarmed gaps. Freshness is protected at trigger time
# (stop crossed, existing position, stale), not by the clock.
ARMED_ORDER_MAX_TTL_MINUTES = 240.0


def _armed_hard_stop_price(exit_plan: object) -> float | None:
    """Return the hard-stop price through the common exit-plan normalizer."""
    if not isinstance(exit_plan, dict):
        return None
    try:
        normalized = normalize_exit_plan(exit_plan)
    except InvalidExitPlanError:
        return None
    if not normalized:
        return None
    hard_stop = normalized.get("hard_stop")
    if isinstance(hard_stop, dict):
        if hard_stop.get("type", "price") != "price":
            return None
        raw = hard_stop.get("price")
    else:
        raw = hard_stop
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _armed_hard_stop_is_specified(exit_plan: object) -> bool:
    if not isinstance(exit_plan, dict):
        return False
    sentinel = object()
    hard_stop = exit_plan.get("hard_stop", sentinel)
    if hard_stop is sentinel:
        for alias in ("stop_loss", "stop", "sl"):
            if alias in exit_plan:
                hard_stop = exit_plan[alias]
                break
    if hard_stop is sentinel:
        return False
    if not isinstance(hard_stop, dict):
        try:
            price = float(hard_stop)
        except (TypeError, ValueError):
            return False
        return math.isfinite(price) and price > 0
    hard_stop_type = str(hard_stop.get("type", "price"))
    if hard_stop_type == "price":
        try:
            price = float(hard_stop.get("price"))
        except (TypeError, ValueError):
            return False
        return math.isfinite(price) and price > 0
    if hard_stop_type == "percent":
        return hard_stop.get("percent") is not None
    if hard_stop_type == "volatility_multiple":
        return hard_stop.get("multiple") is not None
    if hard_stop_type == "structural":
        return hard_stop.get("anchor") is not None
    return False


def _pine_qty_percent_fraction(raw: dict) -> float | None:
    if raw.get("qty_percent") is None:
        return None
    try:
        fraction = float(raw["qty_percent"]) / 100.0
    except (TypeError, ValueError):
        return None
    return fraction if math.isfinite(fraction) and 0.0 < fraction <= 1.0 else None


def _pine_armed_exit_to_exit_plan(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    stop = raw.get("stop")
    if stop is None:
        stop = raw.get("hard_stop")
    if stop is None:
        stop = raw.get("sl")
    if stop is not None:
        out["hard_stop"] = stop
    if raw.get("limit") is not None:
        take_profit = {
            "type": "price",
            "price": raw["limit"],
            "fraction": _pine_qty_percent_fraction(raw) or 1.0,
        }
        if raw.get("id") is not None:
            take_profit["name"] = raw["id"]
        out["take_profits"] = [take_profit]
    elif raw.get("tp") is not None:
        out["take_profits"] = raw["tp"]
    elif raw.get("take_profits") is not None:
        out["take_profits"] = raw["take_profits"]
    if raw.get("trail") is not None:
        out["trailing_stop"] = raw["trail"]
    elif raw.get("trail_offset") is not None:
        out["trailing_stop"] = {
            "trail_type": raw.get("trail_type") or raw.get("offset_type") or "price",
            "trail_value": raw["trail_offset"],
        }
    if raw.get("protect") is not None:
        out["profit_protection"] = raw["protect"]
    if raw.get("exit_watch") is not None:
        out["exit_watch"] = raw["exit_watch"]
    if raw.get("max_hold_minutes") is not None:
        out["max_hold_minutes"] = raw["max_hold_minutes"]
    return out or None


def _strategy_entry_armed_order(raw: dict) -> dict | None:
    args: dict | None = None
    if raw.get("tool") == "strategy_entry" and isinstance(raw.get("args"), dict):
        args = dict(raw["args"])
    elif isinstance(raw.get("strategy_entry"), dict):
        args = dict(raw["strategy_entry"])
    elif isinstance(raw.get("entry"), dict):
        args = dict(raw["entry"])
    elif any(raw.get(key) is not None for key in ("direction", "side", "exit", "stop", "limit")):
        args = dict(raw)
    if args is None:
        return None
    for key in ("confidence", "rationale"):
        if args.get(key) is None and raw.get(key) is not None:
            args[key] = raw[key]
    direction = str(args.get("direction") or args.get("side") or "").lower().replace("strategy.", "")
    if direction in {"long", "buy"}:
        intent = "OPEN_LONG"
    elif direction in {"short", "sell"}:
        intent = "OPEN_SHORT"
    else:
        return None
    exit_raw = args.get("exit")
    if exit_raw is None and any(
        args.get(key) is not None for key in ("stop", "hard_stop", "sl", "limit", "tp", "take_profits", "trail")
    ):
        exit_raw = args
    return {
        "intent": intent,
        "qty": args.get("qty", args.get("quantity")),
        "confidence": args.get("confidence"),
        "exit_plan": _pine_armed_exit_to_exit_plan(exit_raw),
        "rationale": args.get("rationale"),
    }


def normalize_armed_order(raw: object) -> dict | None:
    """Return an armable order or None. The action is derived from intent."""
    if not isinstance(raw, dict):
        return None
    raw = _strategy_entry_armed_order(raw) or raw
    intent = str(raw.get("intent") or "").upper()
    action = _ARMABLE_INTENTS.get(intent)
    if action is None:
        return None
    try:
        qty = float(raw.get("qty") if raw.get("qty") is not None else raw.get("quantity"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qty) or qty <= 0:
        return None
    exit_plan = raw.get("exit_plan")
    if not _armed_hard_stop_is_specified(exit_plan):
        return None
    try:
        validate_exit_plan(exit_plan, allow_unresolved=True)
    except InvalidExitPlanError:
        return None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    order = {
        "intent": intent,
        "action": action,
        "qty": qty,
        "confidence": confidence,
        "exit_plan": exit_plan,
    }
    if raw.get("rationale"):
        order["rationale"] = str(raw["rationale"])[:500]
    return order


def armed_order_price_coherent(order: dict, *, price: float) -> bool:
    """Return whether the trigger price is on the safe side of the hard stop.

    An armed plan can trigger far from the price assumed at arming time. If the
    stop is already crossed, opening would create an instantly stoppable
    position.
    """
    stop = _armed_hard_stop_price(order.get("exit_plan"))
    if stop is None or not math.isfinite(price):
        return False
    if order.get("intent") == "OPEN_LONG":
        return price > stop
    return price < stop
