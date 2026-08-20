#!/usr/bin/env python3
"""Attribution des sélections d'univers — familles / venues / rôles qui paient.

Lit ``universe_selection_outcomes`` dans ``casys.db`` et répond en JSON (AX #3)
à : quelles familles, venues et rôles de sélection paient, et lesquels déçoivent.

Usage :
    uv run python scripts/universe_selection_analytics.py
    uv run python scripts/universe_selection_analytics.py bench
    uv run python scripts/universe_selection_analytics.py evaluate
    uv run python scripts/universe_selection_analytics.py trader
    uv run python scripts/universe_selection_analytics.py trader --json
    uv run python scripts/universe_selection_analytics.py --state-dir PATH --horizon 5

``bench`` compare ``selector=agent`` et ``selector=baseline_fallback`` par
base (beat_bench_rate allocation, win_rate directionnel, n).

``evaluate`` juge les mandats de ``state/universe_mandates/history.jsonl`` via
le port ``DataSource`` (YFinance par défaut), upsert les verdicts, recalcule
FLAIR, puis imprime le même résumé.

``trader`` joint les décisions exécutées porteuses d'un ``mandate_ref``
(archives gzip + ``state/decisions.jsonl``) aux cycles de position flat-to-flat
(``state/model_performance.jsonl``) : n, P&L brut et, seulement si les
commissions sont complètes, win rate/P&L net par famille et par rôle, comparés
aux trades dont la décision disponible n'a pas de ``mandate_ref`` sur la même
période. Les cycles sans décision attribuable restent séparés.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from trader.application.record.decision_ledger_rows import decision_row_mandate_ref
from trader.application.universe.selection_attribution import (
    iter_mandate_payloads,
    load_mandate_selections,
    selections_from_mandate_payload,
)
from trader.domain.semantic.catalog import family_for_symbol

ROOT = Path(__file__).resolve().parent.parent

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


def _horizon_rows(state_dir: Path, *, horizon: int | None) -> list[dict[str, Any]]:
    rows = _open_store(state_dir).load_outcomes()
    if horizon is not None:
        rows = [row for row in rows if int(row.get("horizon_sessions") or 0) == horizon]
    return rows


def run_summary(state_dir: Path, *, horizon: int | None) -> dict[str, Any]:
    from trader.application.universe.selection_attribution import summarize_outcomes

    return summarize_outcomes(_horizon_rows(state_dir, horizon=horizon))


def run_bench(state_dir: Path, *, horizon: int | None) -> dict[str, Any]:
    from trader.application.universe.selection_attribution import compare_selection_selectors

    rows = _horizon_rows(state_dir, horizon=horizon)
    horizons = sorted({int(row["horizon_sessions"]) for row in rows if row.get("horizon_sessions") is not None})
    return {
        "horizon_sessions": horizons[0] if len(horizons) == 1 else None,
        "horizons": horizons,
        "selectors": compare_selection_selectors(rows),
    }


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
    store.ensure_selection_semantics()
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
    rows = [row for row in store.load_outcomes() if int(row.get("horizon_sessions") or 0) == horizon]
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
        from trader.reporting.read_models.trade_history import (
            aggregate_position_cycles,
            compute_round_trips,
        )
    except ImportError:
        return []
    return aggregate_position_cycles(compute_round_trips(state_dir))


def _fill_decision_ids(state_dir: Path) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for row in _read_jsonl_dicts(state_dir / "model_performance.jsonl"):
        symbol = str(row.get("symbol") or "").strip()
        ts = str(row.get("ts") or "").strip()
        decision_id = str(row.get("decision_id") or "").strip()
        if symbol and ts and decision_id:
            index[(symbol, ts)] = decision_id
    return index


def _decisions_by_id(
    state_dir: Path,
    decision_ids: frozenset[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    from trader.reporting.read_models.trade_history import (
        _read_decision_rows_by_id,
    )

    rows_by_id, source_quality = _read_decision_rows_by_id(
        state_dir,
        decision_ids=decision_ids,
    )
    indexed = {decision_id: rows[-1] for decision_id, rows in rows_by_id.items() if rows}
    missing = sorted(decision_ids - indexed.keys())
    duplicate_rows = sum(max(0, len(rows) - 1) for rows in rows_by_id.values())
    quality = {
        **source_quality,
        "rows_requested": len(decision_ids),
        "rows_found": len(indexed),
        "rows_missing": len(missing),
        "duplicate_rows": duplicate_rows,
    }
    if quality.get("status") == "available" and missing:
        quality.update(
            {
                "status": "partial",
                "reason": "decision_history_rows_missing",
                "missing_decision_ids": missing,
            }
        )
    return indexed, quality


def _executed_decisions_by_symbol_ts(
    decisions: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in decisions:
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
    net_values: list[float] = []
    gross_values: list[float] = []
    models: set[str] = set()
    reasons: set[str] = set()
    for row in rows:
        raw_net = row.get("pnl")
        try:
            net = float(raw_net) if raw_net is not None else None
        except (OverflowError, TypeError, ValueError):
            net = None
        if net is not None and math.isfinite(net):
            net_values.append(net)

        raw_gross = row.get("gross_pnl")
        try:
            gross = float(raw_gross) if raw_gross is not None else None
        except (OverflowError, TypeError, ValueError):
            gross = None
        if gross is not None and math.isfinite(gross):
            gross_values.append(gross)

        quality = row.get("commission_quality")
        if not isinstance(quality, dict) or quality.get("status") == "available":
            continue
        raw_models = quality.get("models")
        if isinstance(raw_models, list):
            models.update(str(value) for value in raw_models)
        raw_reasons = quality.get("reasons")
        if isinstance(raw_reasons, list):
            reasons.update(str(value) for value in raw_reasons)
        elif quality.get("reason"):
            reasons.add(str(quality["reason"]))

    net_complete = len(net_values) == n
    gross_complete = len(gross_values) == n
    wins = sum(1 for pnl in net_values if pnl > 0.0)
    return {
        "n": n,
        "win_rate": (wins / n) if n and net_complete else None,
        "net_pnl": sum(net_values) if net_complete else None,
        "gross_pnl": sum(gross_values) if gross_complete else None,
        "economics_quality": {
            "status": "complete" if net_complete else "incomplete",
            "reason": (None if net_complete else "commission_or_net_economics_incomplete"),
            "models": sorted(models),
            "reasons": sorted(reasons),
            "trips": n,
            "net_known": len(net_values),
            "net_unknown": n - len(net_values),
            "gross_known": len(gross_values),
            "gross_unknown": n - len(gross_values),
            "gross_pnl_available": gross_complete,
            "commission_and_net_available": net_complete,
        },
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


def _annotate_trips(
    state_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trips = _round_trips(state_dir)
    fill_ids = _fill_decision_ids(state_dir)
    requested_ids: set[str] = set()
    for trip in trips:
        raw_ids = trip.get("entry_decision_ids")
        if isinstance(raw_ids, list):
            requested_ids.update(decision_id for value in raw_ids if (decision_id := str(value or "").strip()))
        direct_id = str(trip.get("entry_decision_id") or "").strip()
        if direct_id:
            requested_ids.add(direct_id)
        fill_id = fill_ids.get(
            (
                str(trip.get("symbol") or ""),
                str(trip.get("entry_ts") or ""),
            ),
            "",
        )
        if fill_id:
            requested_ids.add(fill_id)
    decisions, decision_rows_quality = _decisions_by_id(
        state_dir,
        frozenset(requested_ids),
    )
    by_symbol_ts = _executed_decisions_by_symbol_ts(list(decisions.values()))
    selections = _mandate_selection_index(state_dir)
    annotated: list[dict[str, Any]] = []
    for trip in trips:
        symbol = str(trip.get("symbol") or "")
        entry_ts = str(trip.get("entry_ts") or "")
        decision_id = str(trip.get("entry_decision_id") or "").strip()
        if not decision_id:
            decision_id = fill_ids.get((symbol, entry_ts), "")
        decision = decisions.get(decision_id) or by_symbol_ts.get((symbol, entry_ts)) or {}
        if not decision_id:
            decision_id = str(decision.get("decision_id") or "")
        mandate_ref = decision_row_mandate_ref(decision)
        attribution_status = (
            "available"
            if decision
            else (
                "decision_history_unavailable"
                if decision_rows_quality.get("status") == "unavailable"
                else "decision_missing"
            )
        )
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
                "mandate_attribution_status": attribution_status,
                "family": family,
                "role": role,
            }
        )
    return annotated, decision_rows_quality


def run_trader(state_dir: Path) -> dict[str, Any]:
    """Joint les décisions archivées/vives aux cycles flat-to-flat."""

    trips, decision_rows_quality = _annotate_trips(state_dir)
    with_ref = [row for row in trips if row.get("mandate_ref")]
    without_ref = [
        row for row in trips if row.get("mandate_attribution_status") == "available" and not row.get("mandate_ref")
    ]
    unattributed = [row for row in trips if row.get("mandate_attribution_status") != "available"]
    timestamps = [str(row.get(key) or "") for row in trips for key in ("entry_ts", "exit_ts") if row.get(key)]
    return {
        "period": {
            "start": min(timestamps) if timestamps else None,
            "end": max(timestamps) if timestamps else None,
        },
        "with_mandate_ref": _trade_stats(with_ref),
        "without_mandate_ref": _trade_stats(without_ref),
        "unattributed": _trade_stats(unattributed),
        "decision_rows_quality": decision_rows_quality,
        "by_family": _group_stats(with_ref, "family"),
        "by_role": _group_stats(with_ref, "role"),
        "method": {
            "outcome_unit": "flat_to_flat_position_cycle",
            "decision_history": "archive_gzip_then_live_last_row_wins",
        },
    }


def _format_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _format_pnl(value: object) -> str:
    try:
        parsed = float(value) if value is not None else None
    except (OverflowError, TypeError, ValueError):
        parsed = None
    return f"{parsed:.2f}" if parsed is not None and math.isfinite(parsed) else "n/a"


def _format_economics_coverage(stats: dict[str, Any]) -> str:
    quality = stats.get("economics_quality")
    if not isinstance(quality, dict):
        return "net ?/?"
    return f"net {quality.get('net_known', 0)}/{quality.get('trips', 0)}"


def _format_trader_report(payload: dict[str, Any]) -> str:
    period = payload.get("period") or {}
    with_ref = payload.get("with_mandate_ref") or {}
    without_ref = payload.get("without_mandate_ref") or {}
    unattributed = payload.get("unattributed") or {}
    decision_quality = payload.get("decision_rows_quality") or {}
    lines = [
        "Attribution trader × mandate_ref",
        f"période: {period.get('start') or '—'} → {period.get('end') or '—'}",
        "",
        (
            "avec mandate_ref   "
            f"n={with_ref.get('n', 0)}  "
            f"win_rate={_format_rate(with_ref.get('win_rate'))}  "
            f"pnl_net={_format_pnl(with_ref.get('net_pnl'))}  "
            f"pnl_brut={_format_pnl(with_ref.get('gross_pnl'))}  "
            f"{_format_economics_coverage(with_ref)}"
        ),
        (
            "sans mandate_ref   "
            f"n={without_ref.get('n', 0)}  "
            f"win_rate={_format_rate(without_ref.get('win_rate'))}  "
            f"pnl_net={_format_pnl(without_ref.get('net_pnl'))}  "
            f"pnl_brut={_format_pnl(without_ref.get('gross_pnl'))}  "
            f"{_format_economics_coverage(without_ref)}"
        ),
        (
            "sans décision attribuable   "
            f"n={unattributed.get('n', 0)}  "
            f"pnl_brut={_format_pnl(unattributed.get('gross_pnl'))}  "
            f"{_format_economics_coverage(unattributed)}"
        ),
        (
            "couverture décisions   "
            f"status={decision_quality.get('status', 'unavailable')}  "
            f"rows={decision_quality.get('rows_found', 0)}/"
            f"{decision_quality.get('rows_requested', 0)}  "
            f"duplicates={decision_quality.get('duplicate_rows', 0)}"
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
            f"pnl_net={_format_pnl(item.get('net_pnl'))}  "
            f"pnl_brut={_format_pnl(item.get('gross_pnl'))}  "
            f"{_format_economics_coverage(item)}"
        )
    lines.extend(["", "par rôle"])
    roles = payload.get("by_role") or []
    if not roles:
        lines.append("  (aucun)")
    for item in roles:
        lines.append(
            f"  {item['role']:<20} n={item['n']}  "
            f"win_rate={_format_rate(item.get('win_rate'))}  "
            f"pnl_net={_format_pnl(item.get('net_pnl'))}  "
            f"pnl_brut={_format_pnl(item.get('gross_pnl'))}  "
            f"{_format_economics_coverage(item)}"
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
        choices=("summary", "bench", "evaluate", "trader"),
        help="summary (défaut) lit le store ; bench compare agent vs baseline ; "
        "evaluate juge puis résume ; trader joint mandate_ref et round-trips.",
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
    elif args.command == "bench":
        payload = run_bench(state_dir, horizon=args.horizon)
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
