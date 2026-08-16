#!/usr/bin/env python3
"""Attribution des sélections d'univers — familles / venues / rôles qui paient.

Lit ``universe_selection_outcomes`` dans ``casys.db`` et répond en JSON (AX #3)
à : quelles familles, venues et rôles de sélection paient, et lesquels déçoivent.

Usage :
    uv run python scripts/universe_selection_analytics.py
    uv run python scripts/universe_selection_analytics.py evaluate
    uv run python scripts/universe_selection_analytics.py trader
    uv run python scripts/universe_selection_analytics.py trader --json
    uv run python scripts/universe_selection_analytics.py --state-dir PATH --horizon 5

``evaluate`` juge les mandats de ``state/universe_mandates/history.jsonl`` via
le port ``DataSource`` (YFinance par défaut), upsert les verdicts, recalcule
FLAIR, puis imprime le même résumé.

``trader`` joint les décisions exécutées porteuses d'un ``mandate_ref``
(``state/decisions.jsonl``) aux round-trips FIFO
(``state/model_performance.jsonl``) : n, win rate et P&L net par famille et
par rôle, comparés aux trades sans ``mandate_ref`` sur la même période.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from trader.application.record.decision_ledger_rows import decision_row_mandate_ref
from trader.application.universe.selection_attribution import (
    iter_mandate_payloads,
    load_mandate_selections,
    selections_from_mandate_payload,
)
from trader.domain.semantic.catalog import family_for_symbol

__all__ = [
    "iter_mandate_payloads",
    "load_mandate_selections",
    "main",
    "run_trader",
]


def build_script_data_source():
    from trader.infrastructure.market_sources.data_source import YFinanceDataSource

    return YFinanceDataSource()


def _open_store(state_dir: Path):
    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

    return try_open_universe_selection_store(state_dir)


def run_summary(state_dir: Path, *, horizon: int | None) -> dict[str, Any]:
    from trader.application.universe.selection_attribution import summarize_outcomes

    rows = _open_store(state_dir).load_outcomes()
    if horizon is not None:
        rows = [row for row in rows if int(row.get("horizon_sessions") or 0) == horizon]
    return summarize_outcomes(rows)


def run_evaluate(
    state_dir: Path,
    *,
    horizon: int,
    lookback: str,
    interval: str,
    shrinkage_k: float,
    data_source=None,
) -> dict[str, Any]:
    from trader.application.universe.selection_attribution import (
        evaluate_selections,
        persist_and_score,
        summarize_outcomes,
    )

    store = _open_store(state_dir)
    source = data_source if data_source is not None else build_script_data_source()
    from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore

    evaluated = evaluate_selections(
        load_mandate_selections(state_dir),
        source,
        horizon_sessions=horizon,
        lookback=lookback,
        interval=interval,
        scope_reader=CandidateScopeStore(state_dir / "candidate_scopes"),
    )
    persist_and_score(store, evaluated, shrinkage_k=shrinkage_k)
    rows = [
        row
        for row in store.load_outcomes()
        if int(row.get("horizon_sessions") or 0) == horizon
    ]
    summary = summarize_outcomes(rows)
    summary["n_evaluated_this_run"] = len(evaluated)
    return summary


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _round_trips(state_dir: Path) -> list[dict[str, Any]]:
    try:
        from trader.reporting.read_models.trade_history import compute_round_trips
    except ImportError:
        return []
    return list(compute_round_trips(state_dir))


def _fill_decision_ids(state_dir: Path) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for row in _read_jsonl_dicts(state_dir / "model_performance.jsonl"):
        symbol = str(row.get("symbol") or "").strip()
        ts = str(row.get("ts") or "").strip()
        decision_id = str(row.get("decision_id") or "").strip()
        if symbol and ts and decision_id:
            index[(symbol, ts)] = decision_id
    return index


def _decisions_by_id(state_dir: Path) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl_dicts(state_dir / "decisions.jsonl"):
        decision_id = str(row.get("decision_id") or "").strip()
        if decision_id:
            indexed[decision_id] = row
    return indexed


def _executed_decisions_by_symbol_ts(state_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _read_jsonl_dicts(state_dir / "decisions.jsonl"):
        if row.get("executed") is not True:
            continue
        symbol = str(row.get("symbol") or "").strip()
        ts = str(row.get("cycle_ts") or row.get("ts") or "").strip()
        if symbol and ts:
            indexed[(symbol, ts)] = row
    return indexed


def _mandate_selection_index(state_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    index: dict[tuple[str, str], dict[str, str]] = {}
    for payload in iter_mandate_payloads(state_dir):
        for selection in selections_from_mandate_payload(payload):
            index[(selection.mandate_id, selection.symbol)] = {
                "family": selection.family,
                "role": selection.role,
            }
    return index


def _trade_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0, "win_rate": None, "net_pnl": 0.0}
    pnls = [float(row["pnl"]) for row in rows]
    wins = sum(1 for pnl in pnls if pnl > 0.0)
    return {
        "n": n,
        "win_rate": wins / n,
        "net_pnl": sum(pnls),
    }


def _group_stats(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = str(row.get(key) or "")
        if not name:
            continue
        buckets.setdefault(name, []).append(row)
    grouped: list[dict[str, Any]] = []
    for name, items in buckets.items():
        stats = _trade_stats(items)
        grouped.append({key: name, **stats})
    grouped.sort(
        key=lambda item: (-int(item["n"]), str(item[key])),
    )
    return grouped


def _annotate_trips(state_dir: Path) -> list[dict[str, Any]]:
    trips = _round_trips(state_dir)
    decisions = _decisions_by_id(state_dir)
    by_symbol_ts = _executed_decisions_by_symbol_ts(state_dir)
    fill_ids = _fill_decision_ids(state_dir)
    selections = _mandate_selection_index(state_dir)
    annotated: list[dict[str, Any]] = []
    for trip in trips:
        symbol = str(trip.get("symbol") or "")
        entry_ts = str(trip.get("entry_ts") or "")
        decision_id = fill_ids.get((symbol, entry_ts), "")
        decision = decisions.get(decision_id) or by_symbol_ts.get((symbol, entry_ts)) or {}
        if not decision_id:
            decision_id = str(decision.get("decision_id") or "")
        mandate_ref = decision_row_mandate_ref(decision)
        family = ""
        role = ""
        if isinstance(mandate_ref, dict):
            mandate_id = str(mandate_ref.get("mandate_id") or "")
            looked_up = selections.get((mandate_id, symbol), {})
            family = str(looked_up.get("family") or family_for_symbol(symbol) or "")
            role = str(looked_up.get("role") or "")
        annotated.append(
            {
                **trip,
                "decision_id": decision_id,
                "mandate_ref": mandate_ref,
                "family": family,
                "role": role,
            }
        )
    return annotated


def run_trader(state_dir: Path) -> dict[str, Any]:
    """Join executed mandate-bearing decisions to FIFO round-trips."""

    trips = _annotate_trips(state_dir)
    with_ref = [row for row in trips if row.get("mandate_ref")]
    without_ref = [row for row in trips if not row.get("mandate_ref")]
    timestamps = [
        str(row.get(key) or "")
        for row in trips
        for key in ("entry_ts", "exit_ts")
        if row.get(key)
    ]
    return {
        "period": {
            "start": min(timestamps) if timestamps else None,
            "end": max(timestamps) if timestamps else None,
        },
        "with_mandate_ref": _trade_stats(with_ref),
        "without_mandate_ref": _trade_stats(without_ref),
        "by_family": _group_stats(with_ref, "family"),
        "by_role": _group_stats(with_ref, "role"),
    }


def _format_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _format_pnl(value: float) -> str:
    return f"{value:.2f}"


def _format_trader_report(payload: dict[str, Any]) -> str:
    period = payload.get("period") or {}
    with_ref = payload.get("with_mandate_ref") or {}
    without_ref = payload.get("without_mandate_ref") or {}
    lines = [
        "Attribution trader × mandate_ref",
        f"période: {period.get('start') or '—'} → {period.get('end') or '—'}",
        "",
        (
            "avec mandate_ref   "
            f"n={with_ref.get('n', 0)}  "
            f"win_rate={_format_rate(with_ref.get('win_rate'))}  "
            f"pnl_net={_format_pnl(float(with_ref.get('net_pnl') or 0.0))}"
        ),
        (
            "sans mandate_ref   "
            f"n={without_ref.get('n', 0)}  "
            f"win_rate={_format_rate(without_ref.get('win_rate'))}  "
            f"pnl_net={_format_pnl(float(without_ref.get('net_pnl') or 0.0))}"
        ),
        "",
        "par famille",
    ]
    families = payload.get("by_family") or []
    if not families:
        lines.append("  (aucune)")
    for item in families:
        lines.append(
            f"  {item['family']:<20} n={item['n']}  "
            f"win_rate={_format_rate(item.get('win_rate'))}  "
            f"pnl_net={_format_pnl(float(item.get('net_pnl') or 0.0))}"
        )
    lines.extend(["", "par rôle"])
    roles = payload.get("by_role") or []
    if not roles:
        lines.append("  (aucun)")
    for item in roles:
        lines.append(
            f"  {item['role']:<20} n={item['n']}  "
            f"win_rate={_format_rate(item.get('win_rate'))}  "
            f"pnl_net={_format_pnl(float(item.get('net_pnl') or 0.0))}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from trader.application.universe.selection_attribution import (
        DEFAULT_BAR_INTERVAL,
        DEFAULT_BAR_LOOKBACK,
    )
    from trader.domain.universe.selection_attribution import (
        DEFAULT_FORWARD_SESSIONS,
        DEFAULT_SHRINKAGE_K,
    )

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="summary",
        choices=("summary", "evaluate", "trader"),
        help="summary (défaut) lit le store ; evaluate juge puis résume ; "
        "trader joint mandate_ref et round-trips.",
    )
    parser.add_argument(
        "--state-dir",
        default=str(ROOT / "state"),
        help="Répertoire d'état (défaut : state/ à la racine du projet).",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=DEFAULT_FORWARD_SESSIONS,
        help=f"Nombre de séances forward (défaut : {DEFAULT_FORWARD_SESSIONS}).",
    )
    parser.add_argument("--lookback", default=DEFAULT_BAR_LOOKBACK)
    parser.add_argument("--interval", default=DEFAULT_BAR_INTERVAL)
    parser.add_argument("--shrinkage-k", type=float, default=DEFAULT_SHRINKAGE_K)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Sortie JSON (toujours utilisée pour summary/evaluate).",
    )
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    if args.command == "evaluate":
        payload = run_evaluate(
            state_dir,
            horizon=args.horizon,
            lookback=args.lookback,
            interval=args.interval,
            shrinkage_k=args.shrinkage_k,
        )
    elif args.command == "trader":
        payload = run_trader(state_dir)
        if not args.json:
            print(_format_trader_report(payload), end="")
            return 0
    else:
        payload = run_summary(state_dir, horizon=args.horizon)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
