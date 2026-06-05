"""execution — passage d'ordres.

Interface `Broker` unique. v1 : `SimBroker` (fills simulés au dernier prix, état
persisté en JSON). Plus tard : `IBBroker` derrière la MÊME interface, l'agent ne
voit pas la différence.

Safe defaults : `dry_run` log l'intention sans muter l'état.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Protocol

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: float
    rationale: str = ""


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: str


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0


class Broker(Protocol):
    def submit(self, order: Order, price: float, ts: str, dry_run: bool = True) -> Fill | None: ...
    def positions(self) -> dict[str, Position]: ...
    def cash(self) -> float: ...


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

    def __init__(self, state_path: str | Path, starting_cash: float = 100_000.0):
        self.state_path = Path(state_path)
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            self._state = _State(cash=raw["cash"], positions=raw["positions"], fills=raw["fills"])
        else:
            self._state = _State(cash=starting_cash)
            self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(self._state), indent=2))

    def submit(self, order: Order, price: float, ts: str, dry_run: bool = True) -> Fill | None:
        fill = Fill(order.symbol, order.side, order.quantity, price, ts)
        if dry_run:
            return None  # intention loggée par l'appelant, état non muté

        signed = order.quantity if order.side == "BUY" else -order.quantity
        pos = self._state.positions.get(order.symbol, {"symbol": order.symbol, "quantity": 0.0, "avg_price": 0.0})
        new_qty = pos["quantity"] + signed
        if signed > 0:  # achat -> moyenne le prix d'entrée
            total = pos["avg_price"] * pos["quantity"] + price * signed
            pos["avg_price"] = total / new_qty if new_qty else 0.0
        pos["quantity"] = new_qty
        self._state.positions[order.symbol] = pos
        self._state.cash -= signed * price
        self._state.fills.append(asdict(fill))
        self._save()
        return fill

    def positions(self) -> dict[str, Position]:
        return {s: Position(**p) for s, p in self._state.positions.items() if p["quantity"] != 0}

    def cash(self) -> float:
        return self._state.cash
