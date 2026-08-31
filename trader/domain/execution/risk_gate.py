"""Pure, fail-closed execution risk gate."""

from __future__ import annotations

import math

from trader.domain.contracts import Order
from trader.domain.risk import RiskLimits, Verdict


class RiskGate:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def max_order_quantity_at_price(
        self,
        price: float,
        *,
        fx_rate: float = 1.0,
    ) -> float:
        if not math.isfinite(price) or price <= 0:
            return 0.0
        if not math.isfinite(fx_rate) or fx_rate <= 0:
            return 0.0
        price_usd = price * fx_rate
        quantity = self.limits.max_order_value / price_usd
        while quantity > 0.0 and quantity * price_usd > self.limits.max_order_value:
            quantity = math.nextafter(quantity, 0.0)
        return quantity

    def max_quantity_at_risk(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        *,
        fx_rate: float = 1.0,
    ) -> float:
        """Largest quantity whose stop distance stays inside the risk budget."""

        try:
            equity = float(equity)
            entry_price = float(entry_price)
            stop_price = float(stop_price)
            pct = float(self.limits.max_risk_per_trade_pct)
            fx_rate = float(fx_rate)
        except (TypeError, ValueError):
            return 0.0

        if (
            not math.isfinite(equity)
            or not math.isfinite(entry_price)
            or not math.isfinite(stop_price)
            or not math.isfinite(pct)
            or not math.isfinite(fx_rate)
            or equity <= 0
            or pct <= 0
            or fx_rate <= 0
        ):
            return 0.0

        distance = abs(entry_price - stop_price) * fx_rate
        risk_cap = pct * equity
        if (
            not math.isfinite(distance)
            or distance <= 0
            or not math.isfinite(risk_cap)
            or risk_cap <= 0
        ):
            return 0.0

        quantity = risk_cap / distance
        if not math.isfinite(quantity) or quantity <= 0:
            return 0.0
        while quantity > 0.0 and quantity * distance > risk_cap:
            quantity = math.nextafter(quantity, 0.0)
        return quantity

    def required_confidence(self, planned_risk_pct: float | None) -> float:
        """Return the minimum confidence required for the planned risk."""

        low = self.limits.min_trade_confidence
        high = self.limits.full_risk_confidence
        budget = self.limits.max_risk_per_trade_pct

        if (
            planned_risk_pct is None
            or not math.isfinite(planned_risk_pct)
            or not math.isfinite(budget)
            or budget <= 0
        ):
            return high

        ratio = max(0.0, min(1.0, planned_risk_pct / budget))
        return low + (high - low) * ratio

    def check_confidence(
        self,
        confidence: float | None,
        planned_risk_pct: float | None,
    ) -> Verdict:
        """Reject missing, invalid or insufficient confidence fail-closed."""

        if not self.limits.confidence_gate_enabled:
            return Verdict(True)

        required = self.required_confidence(planned_risk_pct)
        if confidence is None or not math.isfinite(confidence):
            context = (
                f"confidence={confidence}"
                f" required={required:.4f}"
                f" planned_risk_pct={planned_risk_pct}"
            )
            return Verdict(False, "confidence_below_required", context)

        if confidence < 0.0 or confidence > 1.0:
            context = (
                f"confidence={confidence} out_of_domain"
                f" required={required:.4f}"
                f" planned_risk_pct={planned_risk_pct}"
            )
            return Verdict(False, "confidence_below_required", context)

        if confidence < required:
            context = (
                f"confidence={confidence}"
                f" required={required:.4f}"
                f" planned_risk_pct={planned_risk_pct}"
            )
            return Verdict(False, "confidence_below_required", context)

        return Verdict(True)

    def check(
        self,
        order: Order,
        price: float,
        *,
        current_position_value: float,
        gross_exposure: float,
        equity: float,
        allow_risk_reduction: bool = False,
        fx_rate: float = 1.0,
    ) -> Verdict:
        """Validate an order against hard execution bounds, failing fast."""

        order_value = abs(order.quantity) * price * fx_rate
        if not math.isfinite(order_value):
            return Verdict(
                False,
                "non_finite_order",
                f"order_value={order_value}",
            )
        signed = order_value if order.side == "BUY" else -order_value
        current_abs_position = abs(current_position_value)
        projected_position = abs(current_position_value + signed)
        risk_reducing = (
            allow_risk_reduction
            and current_abs_position > 0
            and current_position_value * signed < 0
            and abs(signed) <= current_abs_position
            and projected_position < current_abs_position
        )

        if equity < self.limits.min_equity and not risk_reducing:
            return Verdict(
                False,
                "equity_floor_breached",
                f"equity={equity:.2f} < {self.limits.min_equity}",
            )

        if order_value > self.limits.max_order_value and not risk_reducing:
            return Verdict(
                False,
                "order_value_exceeded",
                f"{order_value:.2f} > {self.limits.max_order_value}",
            )

        if projected_position > self.limits.max_position_value and not risk_reducing:
            return Verdict(
                False,
                "position_value_exceeded",
                f"{projected_position:.2f} > {self.limits.max_position_value}",
            )

        projected_gross = (
            gross_exposure - current_abs_position + projected_position
        )
        if projected_gross > self.limits.max_gross_exposure and not risk_reducing:
            return Verdict(
                False,
                "gross_exposure_exceeded",
                f"{projected_gross:.2f} > {self.limits.max_gross_exposure}",
            )

        return Verdict(True)


__all__ = ["RiskGate"]
