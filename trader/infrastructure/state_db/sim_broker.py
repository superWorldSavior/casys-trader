"""JSON-backed paper broker infrastructure adapter."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from trader.application.execute.protocols import CommissionModel
from trader.domain.contracts import Fill, Order, Position
from trader.domain.execution.fill_accounting import POSITION_EPSILON, compute_fill_effect
from trader.infrastructure.brokers.commission_models import NoCommissionModel


@dataclass
class _State:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)


class SimBroker:
    """Broker paper simulé. État persisté pour survivre aux redémarrages du daemon.

    Limites assumées : fills parfaits au dernier prix, pas de slippage. Suffisant
    pour valider la boucle agent, PAS pour juger la performance d'une stratégie.
    """

    def __init__(
        self,
        state_path: str | Path,
        starting_cash: float = 100_000.0,
        commission_model: CommissionModel | None = None,
    ):
        self.state_path = Path(state_path)
        self._commission_model = commission_model or NoCommissionModel()
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            self._state = _State(cash=raw["cash"], positions=raw["positions"], fills=raw["fills"])
        else:
            self._state = _State(cash=starting_cash)
            self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(self._state), indent=2))

    def submit(self, order: Order, price: float, ts: str, dry_run: bool = True, fx_rate: float = 1.0) -> Fill | None:
        commission = self._commission_model.calculate(order, price)
        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            ts=ts,
            commission=commission.amount,
            commission_currency=commission.currency,
            commission_model=commission.model,
            fx_rate=fx_rate,
            process_instance_id=order.process_instance_id,
            attempt_id=order.attempt_id,
            decision_id=order.decision_id,
        )
        if dry_run:
            return None  # intention loggée par l'appelant, état non muté

        pos = self._state.positions.get(order.symbol, {"symbol": order.symbol, "quantity": 0.0, "avg_price": 0.0})
        new_qty, new_avg_price, cash_delta_total = compute_fill_effect(
            old_quantity=pos["quantity"],
            old_avg_price=pos["avg_price"],
            order=order,
            price=price,
            fx_rate=fx_rate,
            commission=commission,
        )
        pos["quantity"] = new_qty
        pos["avg_price"] = new_avg_price
        self._state.positions[order.symbol] = pos
        self._state.cash -= cash_delta_total
        self._state.fills.append(fill.model_dump())
        self._save()
        return fill

    def positions(self) -> dict[str, Position]:
        return {
            s: Position(**p) for s, p in self._state.positions.items() if abs(float(p["quantity"])) > POSITION_EPSILON
        }

    def cash(self) -> float:
        return self._state.cash
