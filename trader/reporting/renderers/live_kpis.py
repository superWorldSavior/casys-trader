"""Text renderer for live KPI reports."""

from __future__ import annotations


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

    def fmt_count(v: int | None) -> str:
        return "n/a" if v is None else str(v)

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
        f"Trades          : {fmt_count(kpis['num_trades'])}",
        f"Positions ouv.  : {fmt_count(kpis['n_positions'])}",
    ]
    broker_quality = kpis.get("broker_quality")
    if (
        isinstance(broker_quality, dict)
        and broker_quality.get("status") == "unavailable"
    ):
        lines.append(
            "Qualité broker  : indisponible "
            f"({broker_quality.get('reason') or 'raison inconnue'})"
        )
    trade_quality = kpis.get("trade_economics_quality")
    if (
        isinstance(trade_quality, dict)
        and trade_quality.get("status") in {"incomplete", "unavailable"}
    ):
        lines.append(
            "Économie trades : "
            f"{trade_quality['status']} "
            f"({trade_quality.get('reason') or 'raison inconnue'})"
        )
    for pos in kpis.get("positions") or []:
        lines.append(f"  {pos['symbol']}: qty={pos['quantity']} avg={pos['avg_price']:.4f}")
    if kpis.get("model_performance"):
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
