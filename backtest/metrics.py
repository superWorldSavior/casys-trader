"""metrics — KPI du backtest et rendu CLI.

Module pur : aucun accès disque, réseau ou temps courant. Les métriques sont
calculées uniquement depuis la courbe d'équité et la liste des trades fournis.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True)
class Metrics:
    total_return: float
    max_drawdown: float
    period_win_rate: float
    volatility: float
    sharpe: float | None
    num_trades: int


def compute_metrics(equity_curve: list[tuple[str, float]], trades: list[dict], starting_equity: float) -> Metrics:
    """Calcule les KPI agrégés depuis une courbe d'équité déjà construite."""
    num_trades = len(trades)
    if len(equity_curve) <= 1:
        return Metrics(
            total_return=0.0,
            max_drawdown=0.0,
            period_win_rate=0.0,
            volatility=0.0,
            sharpe=None,
            num_trades=num_trades,
        )

    values = [equity for _, equity in equity_curve]
    returns = _period_returns(values)
    volatility = _std(returns)
    sharpe = None if len(returns) < 2 or volatility == 0.0 else _mean(returns) / volatility

    return Metrics(
        total_return=values[-1] / starting_equity - 1.0,
        max_drawdown=_max_drawdown(values),
        period_win_rate=sum(1 for ret in returns if ret > 0.0) / len(returns),
        volatility=volatility,
        sharpe=sharpe,
        num_trades=num_trades,
    )


def render_cli(metrics: Metrics) -> str:
    """Retourne un résumé multi-lignes lisible par un opérateur CLI."""
    sharpe = "n/a" if metrics.sharpe is None else f"{metrics.sharpe:.2f}"
    return "\n".join(
        [
            "Backtest metrics",
            f"Rendement total : {_format_percent(metrics.total_return)}",
            f"Drawdown max    : {_format_percent(metrics.max_drawdown)}",
            f"Win rate périodes: {_format_percent(metrics.period_win_rate)}",
            f"Volatilité      : {_format_percent(metrics.volatility)}",
            f"Sharpe          : {sharpe}",
            f"Trades          : {metrics.num_trades}",
        ]
    )


def _period_returns(values: list[float]) -> list[float]:
    """Variations relatives successives de la courbe d'équité."""
    return [current / previous - 1.0 for previous, current in zip(values, values[1:])]


def _max_drawdown(values: list[float]) -> float:
    """Plus gros repli pic-vers-creux, retourné en fraction positive."""
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        if value > peak:
            peak = value
        drawdown = (peak - value) / peak
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return sqrt(variance)


def _format_percent(value: float) -> str:
    return f"{value * 100.0:.2f}%"
