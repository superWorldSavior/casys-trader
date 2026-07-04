"""stats — read-only live KPI reporting helpers."""

from __future__ import annotations

from pathlib import Path

from trader.read_models import live_kpis


def _compute_model_performance(state_dir: Path) -> list[dict]:
    """Compatibility wrapper for legacy tests/imports."""
    return live_kpis.compute_model_performance(state_dir)


def compute_live_kpis(state_dir: Path) -> dict:
    """Compatibility wrapper for the canonical live KPI read model."""
    return live_kpis.compute_live_kpis(state_dir)


def render_text(kpis: dict) -> str:
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
    if kpis["model_performance"]:
        lines.append("Perf modèles:")
        for row in kpis["model_performance"]:
            delta = fmt_float(row["portfolio_equity_delta"])
            avg_conf = fmt_float(row["avg_confidence"], decimals=3)
            lines.append(
                f"  {row['provider']}/{row['model']}: fills={row['fills']} "
                f"delta_equity={delta} avg_conf={avg_conf} fallbacks={row['fallbacks']}"
            )
    return "\n".join(lines)


_render_text = render_text


if __name__ == "__main__":
    from trader.commands.stats import main

    main()
