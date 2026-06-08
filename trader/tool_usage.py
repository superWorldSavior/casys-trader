"""CLI lecture-seule pour croiser trace d'outils et qualité forward."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from backtest.decision_quality import BAND, CAVEAT, score_from_ledger

from .tool_trace import TOOLS, summarize_tools

STATE_DIR = Path("state")
DEFAULT_LEDGER = STATE_DIR / "decisions.jsonl"
OUTPUT_PATH = STATE_DIR / "last_tool_usage.json"


def tool_usage_rates(judgeable_traces: list[dict]) -> list[dict]:
    total = len(judgeable_traces)
    rows: list[dict] = []
    for tool in TOOLS:
        used = sum(1 for trace in judgeable_traces if tool in trace.get("tools_used", []))
        skipped = total - used
        rows.append(
            {
                "tool": tool,
                "used": used,
                "skipped": skipped,
                "usage_rate": (used / total) if total else None,
            }
        )
    return rows


def clamp_count(judgeable_traces: list[dict]) -> int:
    return sum(
        1
        for trace in judgeable_traces
        for item in trace.get("trace", [])
        if item.get("tool") == "next_wake" and item.get("outcome") == "clamped"
    )


def tool_vs_quality(scored_rows: list[dict], traces_by_decision_id: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    matched = [
        row
        for row in scored_rows
        if row.get("decision_id") in traces_by_decision_id
    ]
    for tool in TOOLS:
        for group_name, expected_used in (("used", True), ("skipped", False)):
            group = [
                row
                for row in matched
                if (tool in traces_by_decision_id[str(row.get("decision_id"))].get("tools_used", []))
                is expected_used
            ]
            evaluable = [
                row
                for row in group
                if row.get("evaluable") and row.get("forward_return") is not None
            ]
            returns = [float(row["forward_return"]) for row in evaluable]
            rows.append(
                {
                    "tool": tool,
                    "group": group_name,
                    "n": len(group),
                    "n_evaluable": len(evaluable),
                    "mean_forward_return": (sum(returns) / len(returns)) if returns else None,
                }
            )
    return rows


def _traces_by_decision_id(judgeable: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    traces = [summarize_tools(row) for row in judgeable]
    indexed = {
        str(row["decision_id"]): trace
        for row, trace in zip(judgeable, traces, strict=True)
        if row.get("decision_id") is not None
    }
    return traces, indexed


def build_report(ledger: str | Path, *, band: float = BAND, days_buffer: int = 1) -> dict[str, Any]:
    data = score_from_ledger(ledger, band=band, days_buffer=days_buffer)
    traces, indexed = _traces_by_decision_id(data["judgeable"])
    usage = tool_usage_rates(traces)
    clamps = clamp_count(traces)
    quality = tool_vs_quality(data["scored_rows"], indexed)
    start, end = data["window"]
    return {
        "params": {
            "band": band,
            "days_buffer": days_buffer,
            "start": start,
            "end": end,
        },
        "ledger": str(ledger),
        "counts": {
            "ledger": len(data["ledger_rows"]),
            "judgeable": len(data["judgeable"]),
            "scored_rows": len(data["scored_rows"]),
            "joined_decisions": len(indexed),
        },
        "usage": usage,
        "clamps": clamps,
        "tool_vs_quality": quality,
        "exclusions": data["exclusions"],
        "unavailable_symbols": data["unavailable_symbols"],
        "output_path": str(OUTPUT_PATH),
    }


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _format_return(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.2f}%"


def _render_cli(report: dict[str, Any]) -> str:
    lines = [
        "Usage des outils (décisions jugeables)",
        f"{'Outil':<18} {'used':>5} {'skipped':>8} {'usage%':>8}",
        "-" * 43,
    ]
    for item in report["usage"]:
        lines.append(
            f"{item['tool']:<18} {item['used']:>5} {item['skipped']:>8} {_format_pct(item['usage_rate']):>8}"
        )

    lines.extend(
        [
            "",
            f"Écrêtages next_wake : {report['clamps']} décisions (demandé > effectif)",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Usage des canaux d'outils vs qualité forward")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="ledger JSONL des décisions")
    parser.add_argument("--band", type=float, default=BAND, help="bande significative de rendement forward")
    parser.add_argument("--days-buffer", type=int, default=1, help="jours de marge avant la première décision")
    parser.add_argument("--json", action="store_true", help="affiche le rapport complet en JSON")
    args = parser.parse_args()

    report = build_report(args.ledger, band=args.band, days_buffer=args.days_buffer)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render_cli(report))


if __name__ == "__main__":
    main()
