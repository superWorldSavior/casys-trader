"""Tool-usage report projection from persisted decision ledgers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from backtest.decision_quality import BAND, score_from_ledger

from trader.reporting.tool_trace import TOOLS, summarize_tools


def domain_tool_usage(judgeable: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Compte l'usage des domain tools (hors TOOLS legacy) avec répartition par outcome.

    Scanne runtime.tool_calls de chaque row du ledger (données brutes, pas les traces
    summarize_tools) pour construire deux agrégats :
    - per_tool : [{tool, used, outcomes: {outcome: count}}] trié par nom d'outil
    - global_outcomes : {outcome: total_count}

    Conforme design §8/§12 : les domain tools et leurs outcomes (ok/rejected/error/
    budget_exhausted/truncated) sont désormais comptabilisés séparément de la section
    legacy (tool_usage_rates).
    """
    tool_outcomes: dict[str, dict[str, int]] = {}
    global_counts: dict[str, int] = {}

    for row in judgeable:
        if not isinstance(row, dict):
            continue
        runtime = row.get("runtime")
        if not isinstance(runtime, dict):
            continue
        calls = runtime.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            tool = call.get("tool")
            if not isinstance(tool, str) or tool in TOOLS:
                continue  # ignorer les outils legacy et les entrées non-str
            outcome = str(call.get("outcome") or "unknown")
            tool_outcomes.setdefault(tool, {})
            tool_outcomes[tool][outcome] = tool_outcomes[tool].get(outcome, 0) + 1
            global_counts[outcome] = global_counts.get(outcome, 0) + 1

    per_tool = [{"tool": t, "used": sum(c.values()), "outcomes": c} for t, c in sorted(tool_outcomes.items())]
    return per_tool, global_counts


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


def _order_items(judgeable_traces: list[dict]) -> list[dict]:
    return [
        item
        for trace in judgeable_traces
        for item in trace.get("trace", [])
        if item.get("tool") == "order" and item.get("invoked")
    ]


def _detail(item: dict) -> dict:
    detail = item.get("detail")
    return detail if isinstance(detail, dict) else {}


def _args(item: dict) -> dict:
    args = item.get("args")
    return args if isinstance(args, dict) else {}


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _rate(count: int, total: int) -> float | None:
    return (count / total) if total else None


def _risk_observed_opening(item: dict) -> bool:
    if _args(item).get("intent") not in {"OPEN_LONG", "OPEN_SHORT"}:
        return False
    detail = _detail(item)
    # Dénominateur des ratios risque : ouvertures pures arrivées à la couche risque.
    # Les rejets antérieurs (ex: invalid_exit_plan) ne sont pas mélangés ici.
    return (
        isinstance(detail.get("risk_clamped"), bool)
        or isinstance(detail.get("risk_unbounded_no_stop"), bool)
        or _finite_float(detail.get("risk_pct")) is not None
        or _finite_float(detail.get("stop_distance")) is not None
    )


def risk_observability(judgeable_traces: list[dict]) -> dict[str, Any]:
    orders = _order_items(judgeable_traces)
    executed = [item for item in orders if item.get("outcome") == "executed"]
    risk_values = [value for item in executed if (value := _finite_float(_detail(item).get("risk_pct"))) is not None]
    opening_orders = [item for item in orders if _risk_observed_opening(item)]
    risk_clamped_count = sum(1 for item in opening_orders if _detail(item).get("risk_clamped") is True)
    risk_unbounded_no_stop_count = sum(
        1 for item in opening_orders if _detail(item).get("risk_unbounded_no_stop") is True
    )

    return {
        "executed_trades": len(executed),
        "executed_trades_with_risk_pct": len(risk_values),
        "mean_risk_pct": (sum(risk_values) / len(risk_values)) if risk_values else None,
        "risk_clamped_count": risk_clamped_count,
        "risk_clamped_rate": _rate(risk_clamped_count, len(opening_orders)),
        "opening_orders": len(opening_orders),
        "risk_unbounded_no_stop_count": risk_unbounded_no_stop_count,
        "risk_unbounded_no_stop_rate": _rate(risk_unbounded_no_stop_count, len(opening_orders)),
    }


def tool_vs_quality(scored_rows: list[dict], traces_by_decision_id: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    matched = [row for row in scored_rows if row.get("decision_id") in traces_by_decision_id]
    for tool in TOOLS:
        for group_name, expected_used in (("used", True), ("skipped", False)):
            group = [
                row
                for row in matched
                if (tool in traces_by_decision_id[str(row.get("decision_id"))].get("tools_used", [])) is expected_used
            ]
            evaluable = [row for row in group if row.get("evaluable") and row.get("forward_return") is not None]
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


def build_report(
    ledger: str | Path,
    *,
    band: float = BAND,
    days_buffer: int = 1,
    output_path: str | Path = "state/last_tool_usage.json",
) -> dict[str, Any]:
    data = score_from_ledger(ledger, band=band, days_buffer=days_buffer)
    traces, indexed = _traces_by_decision_id(data["judgeable"])
    usage = tool_usage_rates(traces)
    clamps = clamp_count(traces)
    risk = risk_observability(traces)
    quality = tool_vs_quality(data["scored_rows"], indexed)
    by_tool, d_outcomes = domain_tool_usage(data["judgeable"])
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
        "domain_usage": by_tool,
        "domain_outcomes": d_outcomes,
        "clamps": clamps,
        "risk": risk,
        "tool_vs_quality": quality,
        "exclusions": data["exclusions"],
        "unavailable_symbols": data["unavailable_symbols"],
        "output_path": str(output_path),
    }
