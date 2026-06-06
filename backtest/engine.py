"""engine — boucle de rejeu déterministe de l'agent.

Le moteur ne connaît aucune stratégie : il injecte un contexte as-of à une
fonction de décision, puis passe tout ordre au fusible avant exécution simulée.
"""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, cast

from trader.codex_client import Decision
from trader.risk import RiskGate, RiskLimits
from trader.tools.execution import Order, Position, SimBroker


class HistoryLike(Protocol):
    def price_asof(self, symbol: str, ts: str) -> float | None: ...
    def bars_asof(self, symbol: str, ts: str, lookback: int) -> list: ...


DecisionFn = Callable[[str, dict], Decision]
Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: list[tuple[str, float]]
    trades: list[dict]
    starting_equity: float
    final_equity: float


def _bar_to_dict(bar: object) -> dict:
    """Convertit une barre en JSON sans dépendre d'une classe concrète."""
    if isinstance(bar, dict):
        return dict(bar)
    if is_dataclass(bar) and not isinstance(bar, type):
        return asdict(bar)
    return {
        "ts": getattr(bar, "ts"),
        "open": getattr(bar, "open"),
        "high": getattr(bar, "high"),
        "low": getattr(bar, "low"),
        "close": getattr(bar, "close"),
        "volume": getattr(bar, "volume"),
    }


def _positions_to_dict(positions: dict[str, Position]) -> dict:
    """Expose les positions dans le contexte JSON de décision."""
    return {
        symbol: {
            "symbol": position.symbol,
            "quantity": position.quantity,
            "avg_price": position.avg_price,
        }
        for symbol, position in positions.items()
    }


def _position_prices(
    history: HistoryLike,
    ts: str,
    positions: dict[str, Position],
    current_prices: dict[str, float],
) -> dict[str, float]:
    """Prix as-of disponibles pour valoriser les positions ouvertes."""
    prices = dict(current_prices)
    for symbol in positions:
        if symbol in prices:
            continue
        price = history.price_asof(symbol, ts)
        if price is not None:
            prices[symbol] = float(price)
    return prices


def _portfolio_snapshot(
    broker: SimBroker,
    history: HistoryLike,
    ts: str,
    current_prices: dict[str, float],
) -> tuple[dict, float, float, dict[str, Position], dict[str, float]]:
    """Retourne le portefeuille et ses métriques marquées au prix as-of."""
    positions = broker.positions()
    prices = _position_prices(history, ts, positions, current_prices)
    cash = broker.cash()
    position_value = sum(position.quantity * prices[symbol] for symbol, position in positions.items() if symbol in prices)
    equity = cash + position_value
    portfolio = {
        "positions": _positions_to_dict(positions),
        "cash": cash,
        "equity": equity,
    }
    return portfolio, cash, equity, positions, prices


def _gross_exposure(positions: dict[str, Position], prices: dict[str, float]) -> float:
    """Somme des valeurs absolues des positions valorisables."""
    return sum(abs(position.quantity * prices[symbol]) for symbol, position in positions.items() if symbol in prices)


def _current_prices(history: HistoryLike, timeline_symbols: list[str], ts: str) -> dict[str, float]:
    """Prix as-of du pas courant, en ignorant explicitement les absences."""
    prices: dict[str, float] = {}
    for symbol in timeline_symbols:
        price = history.price_asof(symbol, ts)
        if price is not None:
            prices[symbol] = float(price)
    return prices


def run_backtest(
    *,
    history: HistoryLike,
    timeline: list[str],
    symbols: list[str],
    decision_fn: DecisionFn,
    risk_limits: dict,
    starting_cash: float,
    lookback: int = 50,
) -> BacktestResult:
    """Rejoue l'agent sur une timeline échantillonnée, sans lookahead."""
    limits = RiskLimits.from_dict(risk_limits)
    gate = RiskGate(limits)
    equity_curve: list[tuple[str, float]] = []
    trades: list[dict] = []
    starting_equity = float(starting_cash)

    with tempfile.TemporaryDirectory(prefix="casys-backtest-") as temp_dir:
        broker = SimBroker(Path(temp_dir) / "broker.json", starting_cash=starting_equity)

        for ts in timeline:
            prices = _current_prices(history, symbols, ts)
            gate.start_cycle()

            for symbol, price in prices.items():
                portfolio, _cash, equity, positions, position_prices = _portfolio_snapshot(broker, history, ts, prices)
                context = {
                    "now": ts,
                    "symbol": symbol,
                    "price": price,
                    "bars": [_bar_to_dict(bar) for bar in history.bars_asof(symbol, ts, lookback)],
                    "portfolio": portfolio,
                }
                decision = decision_fn(symbol, context)
                if decision.action == "HOLD" or decision.quantity == 0:
                    continue

                side = cast(Side, decision.action)
                order = Order(symbol, side, abs(decision.quantity))
                current_position = positions.get(symbol)
                current_position_value = (current_position.quantity * price) if current_position else 0.0
                current_quantity = current_position.quantity if current_position else 0.0
                allow_risk_reduction = (
                    current_quantity > 0
                    and side == "SELL"
                    and order.quantity <= abs(current_quantity)
                ) or (
                    current_quantity < 0
                    and side == "BUY"
                    and order.quantity <= abs(current_quantity)
                )
                verdict = gate.check(
                    order,
                    price,
                    current_position_value=current_position_value,
                    gross_exposure=_gross_exposure(positions, position_prices),
                    equity=equity,
                    allow_risk_reduction=allow_risk_reduction,
                )
                if not verdict.approved:
                    continue

                fill = broker.submit(order, price, ts, dry_run=False)
                if fill is None:
                    continue
                gate.record_pass()
                trades.append(
                    {
                        "ts": fill.ts,
                        "symbol": fill.symbol,
                        "side": fill.side,
                        "quantity": fill.quantity,
                        "price": fill.price,
                    }
                )

            _portfolio, _cash, equity, _positions, _position_prices = _portfolio_snapshot(broker, history, ts, prices)
            equity_curve.append((ts, equity))

    final_equity = equity_curve[-1][1] if equity_curve else starting_equity
    return BacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        starting_equity=starting_equity,
        final_equity=final_equity,
    )
