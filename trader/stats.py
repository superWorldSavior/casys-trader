"""stats — KPI live depuis l'historique d'équité et l'état du broker.

Module pur côté I/O fichier (lecture uniquement). Réutilise backtest.metrics
pour les calculs. N'importe jamais daemon (pas de cycle circulaire).

CLI : python -m trader.stats [--json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from backtest.metrics import Metrics, compute_metrics, render_cli


def compute_live_kpis(state_dir: Path) -> dict:
    """Calcule les KPI live depuis l'historique + l'état broker.

    Lit :
      - ``state_dir/history.jsonl``  → courbe d'équité (lignes equity=null ignorées)
      - ``state_dir/broker.json``    → positions, fills, cash
      - ``state_dir.parent/config/universe.yaml`` → starting_cash

    Tolère les fichiers absents : retourne des valeurs neutres sans lever.

    Retourne un dict machine-readable compact.
    """
    # --- starting_equity depuis universe.yaml ---
    universe_path = state_dir.parent / "config" / "universe.yaml"
    starting_equity: float = 100_000.0
    if universe_path.exists():
        try:
            cfg = yaml.safe_load(universe_path.read_text())
            starting_equity = float(cfg.get("starting_cash", 100_000.0))
        except Exception:
            pass

    # --- Courbe d'équité depuis history.jsonl ---
    equity_curve: list[tuple[str, float]] = []
    history_path = state_dir / "history.jsonl"
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("equity") is not None:
                    equity_curve.append((row["ts"], float(row["equity"])))
            except Exception:
                continue

    # --- Broker : fills, positions, cash ---
    fills: list[dict] = []
    positions_raw: dict[str, dict] = {}
    current_cash: float | None = None

    broker_path = state_dir / "broker.json"
    if broker_path.exists():
        try:
            broker_data = json.loads(broker_path.read_text())
            fills = broker_data.get("fills", [])
            positions_raw = broker_data.get("positions", {})
            current_cash = float(broker_data.get("cash", starting_equity))
        except Exception:
            pass

    # --- Dernière equity connue (dernier point non-null de la courbe) ---
    last_equity: float | None = equity_curve[-1][1] if equity_curve else None

    # --- Positions actives (quantité non nulle) ---
    positions_list = [
        {
            "symbol": v["symbol"],
            "quantity": v["quantity"],
            "avg_price": v.get("avg_price", 0.0),
        }
        for v in positions_raw.values()
        if v.get("quantity", 0) != 0
    ]

    # --- Métriques via backtest.metrics ---
    metrics: Metrics = compute_metrics(equity_curve, fills, starting_equity)

    return {
        "equity": last_equity,
        "cash": current_cash,
        "total_return": metrics.total_return,
        "max_drawdown": metrics.max_drawdown,
        "period_win_rate": metrics.period_win_rate,
        "volatility": metrics.volatility,
        "sharpe": metrics.sharpe,
        "num_trades": metrics.num_trades,
        "n_positions": len(positions_list),
        "positions": positions_list,
    }


def _render_text(kpis: dict) -> str:
    """Rendu texte lisible pour l'opérateur CLI (inspiré de backtest.metrics.render_cli)."""

    def fmt_pct(v: float | None) -> str:
        if v is None:
            return "n/a"
        return f"{v * 100.0:.2f}%"

    def fmt_float(v: float | None, decimals: int = 2) -> str:
        if v is None:
            return "n/a"
        return f"{v:.{decimals}f}"

    sharpe_str = "n/a" if kpis["sharpe"] is None else f"{kpis['sharpe']:.2f}"
    lines = [
        "Live KPIs",
        f"Équité courante : {fmt_float(kpis['equity'])}",
        f"Cash            : {fmt_float(kpis['cash'])}",
        f"Rendement total : {fmt_pct(kpis['total_return'])}",
        f"Drawdown max    : {fmt_pct(kpis['max_drawdown'])}",
        f"Win rate        : {fmt_pct(kpis['period_win_rate'])}",
        f"Volatilité      : {fmt_pct(kpis['volatility'])}",
        f"Sharpe          : {sharpe_str}",
        f"Trades          : {kpis['num_trades']}",
        f"Positions ouv.  : {kpis['n_positions']}",
    ]
    for pos in kpis["positions"]:
        lines.append(f"  {pos['symbol']}: qty={pos['quantity']} avg={pos['avg_price']:.4f}")
    return "\n".join(lines)


def main() -> None:
    """Point d'entrée CLI : python -m trader.stats [--json]."""
    parser = argparse.ArgumentParser(description="KPI live du trader paper")
    parser.add_argument("--json", action="store_true", help="sortie JSON compact")
    args = parser.parse_args()

    # STATE_DIR = racine repo / "state"
    state_dir = Path(__file__).resolve().parent.parent / "state"
    kpis = compute_live_kpis(state_dir)

    if args.json:
        print(json.dumps(kpis, separators=(",", ":"), ensure_ascii=False))
    else:
        print(_render_text(kpis))


if __name__ == "__main__":
    main()
