"""risk — le FUSIBLE. Codé en dur, non négociable.

Ce n'est PAS de la stratégie : c'est la borne externe qui empêche un bug de
l'agent (boucle d'ordres, position absurde) de tout casser. L'agent peut définir
ses propres règles plus fines par-dessus ; ceci est la dernière barrière.

Toute décision Codex passe par `RiskGate.check(...)` AVANT exécution. Verdict
machine-readable : approuvé, ou rejeté avec un `code` + `context`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .tools.execution import Order


def _validate_confidence_threshold(value: float, name: str) -> None:
    """Lève ValueError si value n'est pas un seuil de confiance valide."""
    if not math.isfinite(value):
        raise ValueError(f"{name}={value!r} doit être fini")
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name}={value!r} doit être dans [0, 1]")


@dataclass(frozen=True)
class RiskLimits:
    max_position_value: float       # $ max par position (valeur absolue)
    max_gross_exposure: float       # $ max exposition brute (somme |positions|)
    max_order_value: float          # $ max par ordre unique
    max_orders_per_cycle: int       # débit max d'ordres par réveil
    min_equity: float               # equity plancher : sous ce seuil, plus aucun ordre
    max_risk_per_trade_pct: float = 0.01  # % equity risqué si le hard_stop saute
    # Gate de confiance adapté au risque :
    #   required = min_trade_confidence + (full_risk_confidence − min_trade_confidence)
    #              × clamp(planned_risk_pct / max_risk_per_trade_pct, 0, 1)
    min_trade_confidence: float = 0.7   # seuil plancher (risque nul)
    full_risk_confidence: float = 0.9   # seuil exigé au budget complet (ou sans stop)

    def __post_init__(self) -> None:
        # Fail-closed : des seuils invalides rendraient le gate inutilisable.
        _validate_confidence_threshold(self.min_trade_confidence, "min_trade_confidence")
        _validate_confidence_threshold(self.full_risk_confidence, "full_risk_confidence")
        if self.min_trade_confidence > self.full_risk_confidence:
            raise ValueError(
                f"min_trade_confidence={self.min_trade_confidence!r} "
                f"> full_risk_confidence={self.full_risk_confidence!r}"
            )

    @classmethod
    def from_dict(cls, d: dict) -> "RiskLimits":
        return cls(
            max_position_value=float(d["max_position_value"]),
            max_gross_exposure=float(d["max_gross_exposure"]),
            max_order_value=float(d["max_order_value"]),
            max_orders_per_cycle=int(d["max_orders_per_cycle"]),
            min_equity=float(d["min_equity"]),
            max_risk_per_trade_pct=float(d.get("max_risk_per_trade_pct", 0.01)),
            min_trade_confidence=float(d.get("min_trade_confidence", 0.7)),
            full_risk_confidence=float(d.get("full_risk_confidence", 0.9)),
        )


@dataclass(frozen=True)
class Verdict:
    approved: bool
    code: str = "ok"
    context: str = ""


class RiskGate:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self._orders_this_cycle = 0

    def start_cycle(self) -> None:
        self._orders_this_cycle = 0

    def max_order_quantity_at_price(self, price: float) -> float:
        if not math.isfinite(price) or price <= 0:
            return 0.0
        quantity = self.limits.max_order_value / price
        while quantity > 0.0 and quantity * price > self.limits.max_order_value:
            quantity = math.nextafter(quantity, 0.0)
        return quantity

    def max_quantity_at_risk(self, equity: float, entry_price: float, stop_price: float) -> float:
        """Plus grande qty telle que qty * distance_stop <= pct_risque * equity."""
        try:
            equity = float(equity)
            entry_price = float(entry_price)
            stop_price = float(stop_price)
            pct = float(self.limits.max_risk_per_trade_pct)
        except (TypeError, ValueError):
            return 0.0

        if (
            not math.isfinite(equity)
            or not math.isfinite(entry_price)
            or not math.isfinite(stop_price)
            or not math.isfinite(pct)
            or equity <= 0
            or pct <= 0
        ):
            return 0.0

        distance = abs(entry_price - stop_price)
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
        """Confiance minimale en fonction du risque planifié.

        Risque non borné (None, nan, inf) → on exige full_risk_confidence.
        """
        lo = self.limits.min_trade_confidence
        hi = self.limits.full_risk_confidence
        budget = self.limits.max_risk_per_trade_pct

        if (
            planned_risk_pct is None
            or not math.isfinite(planned_risk_pct)
            or not math.isfinite(budget)
            or budget <= 0
        ):
            return hi

        ratio = max(0.0, min(1.0, planned_risk_pct / budget))
        return lo + (hi - lo) * ratio

    def check_confidence(
        self,
        confidence: float | None,
        planned_risk_pct: float | None,
    ) -> Verdict:
        """Rejette si la confiance est insuffisante au regard du risque planifié.

        confidence None ou non-finie → rejet fail-safe.
        confidence hors [0,1] → rejet (out_of_domain).
        """
        required = self.required_confidence(planned_risk_pct)

        # Valeur inexploitable : None ou non-finie (nan, inf).
        if confidence is None or not math.isfinite(confidence):
            ctx = (
                f"confidence={confidence}"
                f" required={required:.4f}"
                f" planned_risk_pct={planned_risk_pct}"
            )
            return Verdict(False, "confidence_below_required", ctx)

        # Valeur hors domaine [0,1] : le LLM a renvoyé quelque chose d'absurde.
        if confidence < 0.0 or confidence > 1.0:
            ctx = (
                f"confidence={confidence} out_of_domain"
                f" required={required:.4f}"
                f" planned_risk_pct={planned_risk_pct}"
            )
            return Verdict(False, "confidence_below_required", ctx)

        if confidence < required:
            ctx = (
                f"confidence={confidence}"
                f" required={required:.4f}"
                f" planned_risk_pct={planned_risk_pct}"
            )
            return Verdict(False, "confidence_below_required", ctx)

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
    ) -> Verdict:
        """Valide un ordre contre les bornes. Premier échec = rejet (fail fast)."""
        order_value = abs(order.quantity) * price
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

        if equity < self.limits.min_equity:
            return Verdict(False, "equity_floor_breached", f"equity={equity:.2f} < {self.limits.min_equity}")

        if self._orders_this_cycle >= self.limits.max_orders_per_cycle:
            return Verdict(False, "order_rate_exceeded", f"déjà {self._orders_this_cycle} ordres ce cycle")

        if order_value > self.limits.max_order_value and not risk_reducing:
            return Verdict(False, "order_value_exceeded", f"{order_value:.2f} > {self.limits.max_order_value}")

        if projected_position > self.limits.max_position_value:
            return Verdict(False, "position_value_exceeded", f"{projected_position:.2f} > {self.limits.max_position_value}")

        projected_gross = gross_exposure - current_abs_position + projected_position
        if projected_gross > self.limits.max_gross_exposure:
            return Verdict(False, "gross_exposure_exceeded", f"{projected_gross:.2f} > {self.limits.max_gross_exposure}")

        return Verdict(True)

    def record_pass(self) -> None:
        """À appeler après exécution effective d'un ordre approuvé."""
        self._orders_this_cycle += 1
