"""Pure order-admission helpers for daemon order decisions."""

from __future__ import annotations

import math

from trader.planning.trade_plan import InvalidExitPlanError, normalize_exit_plan

VALID_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "HOLD", "ADD"}
ACTION_INTENTS = {
    "BUY": {"OPEN_LONG", "REDUCE", "CLOSE", "REVERSE", "ADD"},
    "SELL": {"OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "ADD"},
}


def invalid_intent_reason(
    *,
    action: str,
    quantity: float,
    intent: str | None,
) -> str | None:
    if action == "HOLD" or quantity == 0:
        return None
    if intent not in VALID_INTENTS:
        return "invalid_intent"
    if intent == "HOLD":
        return "invalid_intent"
    if intent not in ACTION_INTENTS.get(action, set()):
        return "invalid_intent"
    return None


def hard_stop_price(raw_exit_plan: dict | None) -> float | None:
    if raw_exit_plan is None:
        return None
    try:
        normalized = normalize_exit_plan(raw_exit_plan)
    except InvalidExitPlanError:
        return None
    if not normalized:
        return None

    hard_stop = normalized.get("hard_stop")
    if isinstance(hard_stop, dict):
        if hard_stop.get("type", "price") != "price":
            return None
        raw_price = hard_stop.get("price")
    else:
        raw_price = hard_stop
    if raw_price is None:
        return None

    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def hard_stop_wrong_side(intent: str | None, entry_price: float, stop_price: float) -> bool:
    if not math.isfinite(entry_price) or not math.isfinite(stop_price):
        return False
    if intent == "OPEN_LONG":
        return stop_price >= entry_price
    if intent == "OPEN_SHORT":
        return stop_price <= entry_price
    return False


def loss_distance_to_stop(intent: str | None, entry_price: float, stop_price: float) -> float:
    """Distance de perte réelle jusqu'au stop, clampée à 0 côté profit."""
    try:
        entry_price = float(entry_price)
        stop_price = float(stop_price)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(entry_price) or not math.isfinite(stop_price):
        return 0.0
    if intent == "OPEN_LONG":
        return max(0.0, entry_price - stop_price)
    if intent == "OPEN_SHORT":
        return max(0.0, stop_price - entry_price)
    return abs(entry_price - stop_price)


def reverse_open_quantity(*, action: str, quantity: float, position_quantity: float) -> float:
    signed_order = quantity if action == "BUY" else -quantity
    if position_quantity != 0 and position_quantity * signed_order < 0:
        return max(0.0, abs(signed_order) - abs(position_quantity))
    return quantity


def risk_pct_for_quantity(quantity: float, stop_distance: float | None, equity: float) -> float | None:
    if stop_distance is None:
        return None
    if not math.isfinite(quantity) or not math.isfinite(stop_distance) or stop_distance < 0:
        return None
    if not math.isfinite(equity) or equity <= 0:
        return None
    risk_pct = quantity * stop_distance / equity
    return risk_pct if math.isfinite(risk_pct) else None


def set_entry_risk_metrics(
    entry: dict,
    *,
    quantity: float,
    stop_distance: float | None,
    equity: float,
) -> None:
    entry["stop_distance"] = stop_distance
    entry["risk_pct"] = risk_pct_for_quantity(quantity, stop_distance, equity)


def qty_from_risk_pct(
    risk_pct: float,
    equity: float,
    stop_distance_native: float,
    *,
    fx_rate: float = 1.0,
) -> float:
    """Inverse de risk_pct_for_quantity : qty telle que risk_pct ≈ qty * stop_usd / equity.

    stop_distance_native : distance au hard_stop en unités de prix natives.
    fx_rate : USD par unité native (défaut 1.0 → symbole USD).
    Renvoie 0.0 si les inputs sont invalides ou non-finis.
    """
    try:
        risk_pct = float(risk_pct)
        equity = float(equity)
        stop_distance_native = float(stop_distance_native)
        fx_rate = float(fx_rate)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(risk_pct) or risk_pct <= 0:
        return 0.0
    if not math.isfinite(equity) or equity <= 0:
        return 0.0
    if not math.isfinite(stop_distance_native) or stop_distance_native <= 0:
        return 0.0
    if not math.isfinite(fx_rate) or fx_rate <= 0:
        return 0.0
    stop_usd = stop_distance_native * fx_rate
    if not math.isfinite(stop_usd) or stop_usd <= 0:
        return 0.0
    qty = risk_pct * equity / stop_usd
    return qty if math.isfinite(qty) and qty > 0 else 0.0


def clamp_exit_quantity(
    *,
    intent: str | None,
    action: str,
    quantity: float,
    position_quantity: float,
) -> tuple[float, str | None]:
    if intent not in {"CLOSE", "REDUCE"}:
        return quantity, None
    if position_quantity == 0:
        return 0.0, "no_position_to_reduce"
    if (position_quantity > 0 and action != "SELL") or (position_quantity < 0 and action != "BUY"):
        return 0.0, "exit_side_not_reducing"
    return min(quantity, abs(position_quantity)), None
