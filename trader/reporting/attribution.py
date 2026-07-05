"""Attribution reporting facade.

Projection canonique : `trader.reporting.read_models.attribution`.
Ce module garde la compatibilité d'import et le rendu texte CLI.
"""

from __future__ import annotations

from trader.reporting.read_models.attribution import (
    compute_attribution,
    compute_hard_stop_diagnostics,
    compute_round_trips,
    select_hard_stop_symbols,
)

__all__ = [
    "compute_attribution",
    "compute_hard_stop_diagnostics",
    "compute_round_trips",
    "render_text",
    "select_hard_stop_symbols",
]


def render_text(attr: dict) -> str:
    def fmt(value: float | None, decimals: int = 2) -> str:
        return "n/a" if value is None else f"{value:.{decimals}f}"

    lines = [
        "Attribution décision->résultat",
        f"Trades clôturés : {attr['n_closed_trades']}",
        f"P&L réalisé     : {fmt(attr['realized_pnl'])}",
        f"Win rate        : {fmt(attr['win_rate'], 3)}",
        f"P&L moyen/trade : {fmt(attr['avg_pnl'])}",
        f"Détention moy.  : {fmt(attr['avg_holding_minutes'])} min",
    ]
    if attr["by_confidence"]:
        lines.append("Calibration confidence:")
        for bucket in attr["by_confidence"]:
            lines.append(
                f"  {bucket['bucket']}: n={bucket['n']} win={fmt(bucket['win_rate'], 3)} pnl={fmt(bucket['total_pnl'])}"
            )
    if attr["by_exit_reason"]:
        lines.append("Par raison de sortie:")
        for reason in attr["by_exit_reason"]:
            lines.append(f"  {reason['reason']}: n={reason['n']} pnl={fmt(reason['total_pnl'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    from trader.interfaces.cli.attribution import main

    main()
