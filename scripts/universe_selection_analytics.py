#!/usr/bin/env python3
"""Attribution des sélections d'univers — familles / venues / rôles qui paient.

Lit ``universe_selection_outcomes`` dans ``casys.db`` et répond en JSON (AX #3)
à : quelles familles, venues et rôles de sélection paient, et lesquels déçoivent.

Usage :
    uv run python scripts/universe_selection_analytics.py
    uv run python scripts/universe_selection_analytics.py evaluate
    uv run python scripts/universe_selection_analytics.py --state-dir PATH --horizon 5

``evaluate`` juge les mandats de ``state/universe_mandates/history.jsonl`` via
le port ``DataSource`` (YFinance par défaut), upsert les verdicts, recalcule
FLAIR, puis imprime le même résumé.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from trader.application.universe.selection_attribution import (
    iter_mandate_payloads,
    load_mandate_selections,
)

__all__ = ["iter_mandate_payloads", "load_mandate_selections", "main"]


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
    evaluated = evaluate_selections(
        load_mandate_selections(state_dir),
        source,
        horizon_sessions=horizon,
        lookback=lookback,
        interval=interval,
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
        choices=("summary", "evaluate"),
        help="summary (défaut) lit le store ; evaluate juge puis résume.",
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
    else:
        payload = run_summary(state_dir, horizon=args.horizon)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
