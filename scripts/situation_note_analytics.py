#!/usr/bin/env python3
"""Attribution des notes de situation — directions / familles / horizons.

Lit ``situation_memory.db`` et répond en JSON (AX #3) à : quels types de notes
prédisent, par direction, famille et horizon.

Usage :
    uv run python scripts/situation_note_analytics.py
    uv run python scripts/situation_note_analytics.py evaluate
    uv run python scripts/situation_note_analytics.py --state-dir PATH

``evaluate`` juge les notes via le port ``DataSource`` (YFinance par défaut),
écrit les verdicts en place, recalcule FLAIR, puis imprime le même résumé.
Réévaluer une note déjà jugée écrase le verdict ; ça ne duplique rien.
``q_value`` n'est pas touché.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

__all__ = ["main"]


def build_script_data_source():
    from trader.infrastructure.market_sources.data_source import YFinanceDataSource

    return YFinanceDataSource()


def _open_store(state_dir: Path):
    from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore

    return SituationMemoryStore(state_dir / "situation_memory.db")


def run_summary(state_dir: Path) -> dict[str, Any]:
    from trader.application.analyst.situation_attribution import summarize_outcomes

    return summarize_outcomes(_open_store(state_dir).load_outcomes())


def run_evaluate(
    state_dir: Path,
    *,
    lookback: str,
    interval: str,
    shrinkage_k: float,
    data_source=None,
) -> dict[str, Any]:
    from trader.application.analyst.situation_attribution import (
        evaluate_notes,
        persist_and_score,
        summarize_outcomes,
    )

    store = _open_store(state_dir)
    source = data_source if data_source is not None else build_script_data_source()
    evaluated = evaluate_notes(
        store.load_notes(),
        source,
        lookback=lookback,
        interval=interval,
    )
    persist_and_score(store, evaluated, shrinkage_k=shrinkage_k)
    summary = summarize_outcomes(store.load_outcomes())
    summary["n_evaluated_this_run"] = len(evaluated)
    return summary


def main(argv: list[str] | None = None) -> int:
    from trader.application.analyst.situation_attribution import (
        DEFAULT_BAR_INTERVAL,
        DEFAULT_BAR_LOOKBACK,
    )
    from trader.domain.situation.attribution import DEFAULT_SHRINKAGE_K

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
    parser.add_argument("--lookback", default=DEFAULT_BAR_LOOKBACK)
    parser.add_argument("--interval", default=DEFAULT_BAR_INTERVAL)
    parser.add_argument("--shrinkage-k", type=float, default=DEFAULT_SHRINKAGE_K)
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    if args.command == "evaluate":
        payload = run_evaluate(
            state_dir,
            lookback=args.lookback,
            interval=args.interval,
            shrinkage_k=args.shrinkage_k,
        )
    else:
        payload = run_summary(state_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
