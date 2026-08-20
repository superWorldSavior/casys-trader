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

    quality = attr.get("commission_quality")
    economics_available = (
        isinstance(quality, dict)
        and quality.get("status") == "available"
        and attr.get("realized_pnl") is not None
        and attr.get("total_commissions") is not None
    )
    lines = [
        "Attribution décision->résultat",
        f"Trades clôturés : {attr['n_closed_trades']}",
        f"P&L brut USD    : {fmt(attr.get('realized_gross_pnl'))}",
        (
            f"P&L net courtage: {fmt(attr.get('realized_pnl'))}"
            if economics_available
            else "P&L net courtage: n/a (commissions incomplètes)"
        ),
        (
            f"Commissions     : {fmt(attr.get('total_commissions'))}"
            if economics_available
            else "Commissions     : n/a"
        ),
        f"Win rate net    : {fmt(attr.get('win_rate'), 3) if economics_available else 'n/a'}",
        f"P&L net moyen   : {fmt(attr.get('avg_pnl')) if economics_available else 'n/a'}",
        f"Détention moy.  : {fmt(attr['avg_holding_minutes'])} min",
    ]
    if not economics_available:
        counts = quality.get("counts") if isinstance(quality, dict) else None
        reasons = quality.get("reasons") if isinstance(quality, dict) else None
        lines.append(
            "Qualité frais   : "
            + (
                f"{counts.get('available', 0)}/{counts.get('total', 0)} complets"
                if isinstance(counts, dict)
                else "indisponible"
            )
            + (
                f" ({', '.join(str(value) for value in reasons)})"
                if isinstance(reasons, list) and reasons
                else ""
            )
        )
    if attr["by_confidence"]:
        lines.append("Calibration confidence:")
        for bucket in attr["by_confidence"]:
            bucket_quality = bucket.get("commission_quality")
            bucket_available = (
                isinstance(bucket_quality, dict)
                and bucket_quality.get("status") == "available"
                and bucket.get("total_pnl") is not None
            )
            lines.append(
                f"  {bucket['bucket']}: n={bucket['n']} "
                f"gross={fmt(bucket.get('total_gross_pnl'))} "
                f"win_net={fmt(bucket.get('win_rate'), 3) if bucket_available else 'n/a'} "
                f"net={fmt(bucket.get('total_pnl')) if bucket_available else 'n/a'}"
            )
    if attr["by_exit_reason"]:
        lines.append("Par raison de sortie:")
        for reason in attr["by_exit_reason"]:
            reason_quality = reason.get("commission_quality")
            reason_available = (
                isinstance(reason_quality, dict)
                and reason_quality.get("status") == "available"
                and reason.get("total_pnl") is not None
            )
            lines.append(
                f"  {reason['reason']}: n={reason['n']} "
                f"gross={fmt(reason.get('total_gross_pnl'))} "
                f"net={fmt(reason.get('total_pnl')) if reason_available else 'n/a'}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    from trader.interfaces.cli.attribution import main

    main()
