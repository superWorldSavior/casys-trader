"""risk — le FUSIBLE. Codé en dur, non négociable.

Ce n'est PAS de la stratégie : c'est la borne externe qui empêche un bug de
l'agent (boucle d'ordres, position absurde) de tout casser. L'agent peut définir
ses propres règles plus fines par-dessus ; ceci est la dernière barrière.

Toute décision Codex passe par `RiskGate.check(...)` AVANT exécution. Verdict
machine-readable : approuvé, ou rejeté avec un `code` + `context`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tools.execution import Order


@dataclass(frozen=True)
class RiskLimits:
    max_position_value: float       # $ max par position (valeur absolue)
    max_gross_exposure: float       # $ max exposition brute (somme |positions|)
    max_order_value: float          # $ max par ordre unique
    max_orders_per_cycle: int       # débit max d'ordres par réveil
    min_equity: float               # equity plancher : sous ce seuil, plus aucun ordre

    @classmethod
    def from_dict(cls, d: dict) -> "RiskLimits":
        return cls(
            max_position_value=float(d["max_position_value"]),
            max_gross_exposure=float(d["max_gross_exposure"]),
            max_order_value=float(d["max_order_value"]),
            max_orders_per_cycle=int(d["max_orders_per_cycle"]),
            min_equity=float(d["min_equity"]),
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

    def check(
        self,
        order: Order,
        price: float,
        *,
        current_position_value: float,
        gross_exposure: float,
        equity: float,
    ) -> Verdict:
        """Valide un ordre contre les bornes. Premier échec = rejet (fail fast)."""
        order_value = abs(order.quantity) * price

        if equity < self.limits.min_equity:
            return Verdict(False, "equity_floor_breached", f"equity={equity:.2f} < {self.limits.min_equity}")

        if self._orders_this_cycle >= self.limits.max_orders_per_cycle:
            return Verdict(False, "order_rate_exceeded", f"déjà {self._orders_this_cycle} ordres ce cycle")

        if order_value > self.limits.max_order_value:
            return Verdict(False, "order_value_exceeded", f"{order_value:.2f} > {self.limits.max_order_value}")

        signed = order_value if order.side == "BUY" else -order_value
        projected_position = abs(current_position_value + signed)
        if projected_position > self.limits.max_position_value:
            return Verdict(False, "position_value_exceeded", f"{projected_position:.2f} > {self.limits.max_position_value}")

        projected_gross = gross_exposure + order_value
        if projected_gross > self.limits.max_gross_exposure:
            return Verdict(False, "gross_exposure_exceeded", f"{projected_gross:.2f} > {self.limits.max_gross_exposure}")

        return Verdict(True)

    def record_pass(self) -> None:
        """À appeler après exécution effective d'un ordre approuvé."""
        self._orders_this_cycle += 1
