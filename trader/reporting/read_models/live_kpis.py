"""Live KPI read model projected from persisted runtime state files."""

from __future__ import annotations

import json
from pathlib import Path

from backtest.metrics import Metrics, compute_metrics
from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    compute_round_trips,
)


def compute_model_performance(state_dir: Path) -> list[dict]:
    path = state_dir / "model_performance.jsonl"
    if not path.exists():
        return []

    groups: dict[tuple[str, str], dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        provider = str(row.get("llm_provider") or "unknown")
        model = str(row.get("llm_model") or "unknown")
        key = (provider, model)
        group = groups.setdefault(
            key,
            {
                "provider": provider,
                "model": model,
                "fills": 0,
                "symbols": set(),
                "fallbacks": 0,
                "first_equity": None,
                "last_equity": None,
                "_confidence_sum": 0.0,
                "_confidence_n": 0,
            },
        )
        group["fills"] += 1
        if row.get("symbol"):
            group["symbols"].add(str(row["symbol"]))
        if row.get("llm_fallback_reason"):
            group["fallbacks"] += 1
        if row.get("confidence") is not None:
            try:
                group["_confidence_sum"] += float(row["confidence"])
                group["_confidence_n"] += 1
            except Exception:
                pass
        if row.get("equity") is not None:
            try:
                equity = float(row["equity"])
                if group["first_equity"] is None:
                    group["first_equity"] = equity
                group["last_equity"] = equity
            except Exception:
                pass

    rows: list[dict] = []
    for group in groups.values():
        first_equity = group["first_equity"]
        last_equity = group["last_equity"]
        confidence_n = group["_confidence_n"]
        rows.append(
            {
                "provider": group["provider"],
                "model": group["model"],
                "fills": group["fills"],
                "symbols": sorted(group["symbols"]),
                "fallbacks": group["fallbacks"],
                "first_equity": first_equity,
                "last_equity": last_equity,
                "portfolio_equity_delta": (
                    None if first_equity is None or last_equity is None else last_equity - first_equity
                ),
                "avg_confidence": (None if confidence_n == 0 else group["_confidence_sum"] / confidence_n),
            }
        )
    rows.sort(key=lambda row: (row["provider"], row["model"]))
    return rows


def compute_live_kpis(state_dir: Path) -> dict:
    """Compute live KPIs from history, broker state and model-performance logs."""
    from trader.support.config.portfolio import load_starting_cash

    starting_equity: float = load_starting_cash(state_dir.parent / "config")

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

    fills: list[dict] = []
    positions_raw: dict[str, dict] = {}
    current_cash: float | None = starting_equity

    db_path = state_dir / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.broker_store import SqliteBroker
            from trader.infrastructure.state_db.connection import open_state_db

            broker = SqliteBroker(open_state_db(db_path))
            fills = broker.fills()
            positions_raw = {
                symbol: {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "avg_price": position.avg_price,
                }
                for symbol, position in broker.positions().items()
            }
            current_cash = broker.cash()
        except Exception:
            pass
    elif (broker_path := state_dir / "broker.json").exists():
        try:
            broker_data = json.loads(broker_path.read_text())
            fills = broker_data.get("fills", [])
            positions_raw = broker_data.get("positions", {})
            current_cash = float(broker_data.get("cash", starting_equity))
        except Exception:
            pass

    last_equity: float | None = equity_curve[-1][1] if equity_curve else None
    positions_list = [
        {
            "symbol": value["symbol"],
            "quantity": value["quantity"],
            "avg_price": value.get("avg_price", 0.0),
        }
        for value in positions_raw.values()
        if value.get("quantity", 0) != 0
    ]

    metrics: Metrics = compute_metrics(equity_curve, fills, starting_equity)
    completed_cycles = aggregate_position_cycles(compute_round_trips(state_dir))

    return {
        "equity": last_equity,
        "cash": current_cash,
        "total_return": metrics.total_return,
        "max_drawdown": metrics.max_drawdown,
        "period_win_rate": metrics.period_win_rate,
        "volatility": metrics.volatility,
        "sharpe": metrics.sharpe,
        # Compatibility key with corrected semantics: a trade is one completed
        # flat-to-flat position cycle, not a broker fill.
        "num_trades": len(completed_cycles),
        "num_closed_position_cycles": len(completed_cycles),
        "num_fills": metrics.num_trades,
        "trade_count_grain": "flat_to_flat_position_cycle",
        "n_positions": len(positions_list),
        "positions": positions_list,
        "model_performance": compute_model_performance(state_dir),
    }
