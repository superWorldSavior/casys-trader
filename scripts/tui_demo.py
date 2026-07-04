"""Preview du dashboard TUI avec des données d'exemple (tous les panneaux remplis).

Usage : uv run python scripts/tui_demo.py
But : juger le rendu visuel sans avoir besoin de données live.
"""

from rich.console import Console

from trader.ui.rich_panels import build_view

_DEMO_STATE = {
    "ts": "2026-06-07T03:10:00+00:00",
    "dry_run": False,
    "kill_switch": False,
    "source": "current_report",
    "portfolio": {
        "cash": 64_200.0,
        "equity": 103_850.0,
        "total_return_pct": 3.85,
        "holdings": [
            {"symbol": "BTC-USD", "quantity": 0.45, "avg_price": 61000.0, "last_price": 61406.0, "unrealized_pnl": 182.7},
            {"symbol": "GC=F", "quantity": 3.0, "avg_price": 4360.0, "last_price": 4365.3, "unrealized_pnl": 15.9},
            {"symbol": "NVDA", "quantity": -40.0, "avg_price": 205.1, "last_price": 201.4, "unrealized_pnl": 148.0},
        ],
    },
    "kpis": {
        "sharpe": 1.42, "max_drawdown": -0.061, "period_win_rate": 0.58,
        "volatility": 0.118, "num_trades": 23,
    },
    "equity_curve": [100000, 100400, 99850, 100900, 101600, 101200, 102300,
                     102050, 102900, 103400, 103100, 103850],
    "attribution": {
        "n_closed_trades": 23, "realized_pnl": 1240.5, "win_rate": 0.57,
        "avg_pnl": 53.9, "avg_holding_minutes": 74.0,
        "by_confidence": [
            {"bucket": "0.5-0.7", "n": 9, "total_pnl": -180.0, "win_rate": 0.33, "avg_pnl": -20.0},
            {"bucket": "0.7-0.85", "n": 10, "total_pnl": 820.5, "win_rate": 0.70, "avg_pnl": 82.0},
            {"bucket": "0.85-1.0", "n": 4, "total_pnl": 600.0, "win_rate": 0.75, "avg_pnl": 150.0},
        ],
        "by_exit_reason": [
            {"reason": "take_profit", "n": 11, "total_pnl": 1680.0, "win_rate": 1.0, "avg_pnl": 152.7},
            {"reason": "hard_stop", "n": 7, "total_pnl": -540.0, "win_rate": 0.0, "avg_pnl": -77.0},
            {"reason": "max_hold", "n": 5, "total_pnl": 100.5, "win_rate": 0.6, "avg_pnl": 20.1},
        ],
    },
    "decisions": [
        {"symbol": "BTC-USD", "action": "BUY", "qty": 0.45, "rationale": "cassure range asiatique + momentum 15m", "confidence": 0.81, "executed": True, "reason": "ok"},
        {"symbol": "GC=F", "action": "BUY", "qty": 3.0, "rationale": "repli sur support, RR 1:2", "confidence": 0.66, "executed": True, "reason": "ok"},
        {"symbol": "NVDA", "action": "HOLD", "qty": 0.0, "rationale": "attente confirmation cassure", "confidence": 0.5, "executed": False, "reason": "hold"},
    ],
    "learnings": [
        {"ts": "2026-06-07T02:40:00+00:00", "symbol": "BTC-USD", "note": "les calls >0.7 sur cassure 15m paient mieux que le mean-reversion", "reason": "ok"},
        {"ts": "2026-06-07T01:10:00+00:00", "symbol": "GC=F", "note": "stops trop serrés sur l'or la nuit -> élargir en unités de vol"},
    ],
    "daemon_status": {
        "phase": "cycle_completed", "current_symbol": "BTC-USD",
        "decisions_done": 3, "symbols_total": 3,
        "model_calls_used": 1, "max_model_calls_per_cycle": 25,
    },
}


if __name__ == "__main__":
    Console().print(build_view(_DEMO_STATE))
