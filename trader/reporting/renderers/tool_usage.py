"""Text renderer for tool-usage reports."""

from __future__ import annotations

from typing import Any

from backtest.decision_quality import CAVEAT


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _format_return(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.2f}%"


def render_cli(report: dict[str, Any]) -> str:
    lines = [
        "Usage des outils (décisions jugeables)",
        f"{'Outil':<18} {'used':>5} {'skipped':>8} {'usage%':>8}",
        "-" * 43,
    ]
    for item in report["usage"]:
        lines.append(f"{item['tool']:<18} {item['used']:>5} {item['skipped']:>8} {_format_pct(item['usage_rate']):>8}")

    lines.extend(
        [
            "",
            f"Écrêtages next_wake : {report['clamps']} décisions (demandé > effectif)",
            (
                "Risque exécuté : "
                f"moyenne {_format_pct(report['risk']['mean_risk_pct'])} "
                f"({report['risk']['executed_trades_with_risk_pct']}/"
                f"{report['risk']['executed_trades']} trades mesurés)"
            ),
            (
                "Clamp risque : "
                f"{report['risk']['risk_clamped_count']}/"
                f"{report['risk']['opening_orders']} ouvertures tracées "
                f"({_format_pct(report['risk']['risk_clamped_rate'])}) | "
                "ouvertures tracées sans stop : "
                f"{report['risk']['risk_unbounded_no_stop_count']}/"
                f"{report['risk']['opening_orders']} "
                f"({_format_pct(report['risk']['risk_unbounded_no_stop_rate'])})"
            ),
            "",
            "Outil × qualité forward (le sous-usage coûte-t-il ?)",
            f"{'Outil':<18} {'groupe':<8} {'n':>4} {'éval':>5} {'mean fwd':>10}",
            "-" * 52,
        ]
    )
    for item in report["tool_vs_quality"]:
        lines.append(
            f"{item['tool']:<18} {item['group']:<8} {item['n']:>4} "
            f"{item['n_evaluable']:>5} {_format_return(item['mean_forward_return']):>10}"
        )

    exclusions = report["exclusions"]
    lines.extend(
        [
            "",
            (
                f"Décisions jugeables: {report['counts']['judgeable']} / {report['counts']['ledger']} | "
                f"lignes scorées: {report['counts']['scored_rows']}"
            ),
            f"Exclus : {exclusions['stale_market_data']} stale_market_data, {exclusions['gates']} gates",
        ]
    )
    if exclusions["by_reason"]:
        detail = ", ".join(f"{name}={count}" for name, count in sorted(exclusions["by_reason"].items()))
        lines.append(f"Détail exclusions : {detail}")
    if report["unavailable_symbols"]:
        lines.append(f"Symboles indisponibles : {', '.join(report['unavailable_symbols'])}")
    lines.append(CAVEAT)
    lines.append(f"Rapport complet → {report['output_path']}")
    return "\n".join(lines)


_render_cli = render_cli
