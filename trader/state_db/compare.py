"""compare — outil shadow-compare json↔sqlite (FIX 3).

Compare les états broker, trade_plans et scheduler entre les stores JSON et les
stores SQLite d'un même state_dir. Machine-readable (dict structuré), pas de prose.

API publique :
    compare_backends(state_dir) → dict structuré (identical: bool + diffs)

CLI :
    python -m trader.state_db.compare <state_dir>
    Imprime le dict JSON (indent=2) ; exit 0 si identical, exit 1 sinon.

Le module ouvre la connexion SQLite via open_state_db (registre singleton) et
appelle les import_* en mode idempotent (sentinels state_imports). Pas d'effet
de bord destructif si casys.db est déjà peuplé.

Logging : [state_db] (getLogger(__name__), %-style).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_EPS = 1e-9  # seuil de comparaison cash/quantité


def _norm_ts(raw: str | None) -> str | None:
    """Canonicalise un timestamp ISO → UTC +00:00 pour comparaison sûre."""
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def compare_backends(state_dir: str | Path) -> dict:
    """Compare les états JSON et SQLite d'un state_dir.

    Ouvre SimBroker/TradePlanStore/Scheduler (JSON) et SqliteBroker/
    SqliteTradePlanStore/SqliteScheduler (SQLite) sur le même répertoire.
    Les imports SQLite sont idempotents (pas de re-import si sentinels déjà posés).

    Returns:
        dict avec structure :
        {
          "broker": {
            "cash": {"json": float, "sqlite": float, "identical": bool},
            "positions_diff": [{"symbol": str, "json": {...}, "sqlite": {...}}, ...],
          },
          "trade_plans": {
            "diff": [{"id": str, "json": dict|None, "sqlite": dict|None}, ...],
          },
          "scheduler": {
            "wakes_diff":   [{"field": str, "json": ..., "sqlite": ...}, ...],
            "watches_diff": [{"id": str, "json": dict|None, "sqlite": dict|None}, ...],
            "stale_diff":   [{"symbol": str, "json": int, "sqlite": int}, ...],
          },
          "identical": bool,
        }
        ``identical=True`` si aucune divergence détectée.

    Args:
        state_dir: répertoire d'état (ex. ROOT / "state").
    """
    # Imports locaux — évite dépendances circulaires et imports lourds au top-level
    from trader.state_db.connection import open_state_db
    from trader.state_db.migrations import (
        import_broker_from_json,
        import_trade_plans_from_json,
        import_scheduler_from_json,
    )
    from trader.state_db.broker_store import SqliteBroker
    from trader.state_db.trade_plan_store import SqliteTradePlanStore
    from trader.state_db.scheduler_store import SqliteScheduler
    from trader.tools.execution import SimBroker, NoCommissionModel
    from trader.planning.trade_plan import TradePlanStore

    state_dir = Path(state_dir)
    json_broker_path = state_dir / "broker.json"
    json_plans_path = state_dir / "trade_plans.json"
    json_sched_path = state_dir / "scheduler.json"

    # ------------------------------------------------------------------
    # Côté JSON
    # ------------------------------------------------------------------

    # Broker JSON
    json_broker = SimBroker(
        json_broker_path,
        starting_cash=0.0,
        commission_model=NoCommissionModel(),
    )
    json_cash: float = json_broker.cash()
    json_positions = json_broker.positions()

    # Plans JSON
    json_plans_store = TradePlanStore(json_plans_path)
    json_plans = json_plans_store.open_plans()

    # Scheduler JSON — lire le fichier directement pour accès complet
    # (stale_streaks + indicator_watches sans effets de bord de purge)
    json_sched_raw: dict = {}
    if json_sched_path.exists():
        json_sched_raw = json.loads(json_sched_path.read_text())
    json_sched_raw.setdefault("default_next_wake", None)
    json_sched_raw.setdefault("symbols", {})
    json_sched_raw.setdefault("stale_streaks", {})
    json_sched_raw.setdefault("indicator_watches", {})

    json_symbols_with_wake: set[str] = {
        s for s, v in json_sched_raw["symbols"].items() if v
    }
    json_default_wake_norm = _norm_ts(json_sched_raw["default_next_wake"])
    json_stale: dict[str, int] = {
        s: int(v) for s, v in json_sched_raw["stale_streaks"].items()
    }
    # indicator_watches : id → watch_dict (toutes, y compris expirées)
    json_watches: dict[str, dict] = dict(json_sched_raw["indicator_watches"])

    # ------------------------------------------------------------------
    # Côté SQLite
    # ------------------------------------------------------------------

    db_path = state_dir / "casys.db"
    db = open_state_db(db_path)

    # Imports idempotents : no-op si sentinels déjà posés
    import_broker_from_json(db, json_broker_path, starting_cash=0.0)
    import_trade_plans_from_json(db, json_plans_path)
    import_scheduler_from_json(db, json_sched_path)

    sqlite_broker = SqliteBroker(db, commission_model=NoCommissionModel())
    sqlite_cash: float = sqlite_broker.cash()
    sqlite_positions = sqlite_broker.positions()

    sqlite_plans_store = SqliteTradePlanStore(db)
    sqlite_plans = sqlite_plans_store.open_plans()

    sqlite_sched = SqliteScheduler(db)
    sqlite_symbols_with_wake: set[str] = sqlite_sched.symbols_with_wake()

    default_wake_dt = sqlite_sched.next_wake()
    sqlite_default_wake_norm = _norm_ts(
        default_wake_dt.isoformat() if default_wake_dt else None
    )

    # Stale streaks — lecture directe de la table (pas d'API d'itération sur SqliteScheduler)
    stale_rows = db.query_all(
        "SELECT symbol, streak FROM scheduler_stale_streaks ORDER BY symbol"
    )
    sqlite_stale: dict[str, int] = {r["symbol"]: int(r["streak"]) for r in stale_rows}

    # Watches — toutes (y compris expirées, pour parité avec le JSON)
    watch_rows = db.query_all(
        "SELECT id, watch_json FROM scheduler_watches ORDER BY seq"
    )
    sqlite_watches: dict[str, dict] = {
        r["id"]: json.loads(r["watch_json"]) for r in watch_rows
    }

    # ------------------------------------------------------------------
    # Comparaison broker
    # ------------------------------------------------------------------

    cash_identical = abs(json_cash - sqlite_cash) < _EPS
    cash_info: dict = {
        "json": json_cash,
        "sqlite": sqlite_cash,
        "identical": cash_identical,
    }

    all_pos_symbols = sorted(set(json_positions) | set(sqlite_positions))
    positions_diff: list[dict] = []
    for sym in all_pos_symbols:
        j_pos = json_positions.get(sym)
        s_pos = sqlite_positions.get(sym)
        j_qty = j_pos.quantity if j_pos is not None else None
        j_avg = j_pos.avg_price if j_pos is not None else None
        s_qty = s_pos.quantity if s_pos is not None else None
        s_avg = s_pos.avg_price if s_pos is not None else None
        qty_ok = (j_qty is None and s_qty is None) or (
            j_qty is not None
            and s_qty is not None
            and abs(j_qty - s_qty) < _EPS
        )
        avg_ok = (j_avg is None and s_avg is None) or (
            j_avg is not None
            and s_avg is not None
            and abs(j_avg - s_avg) < _EPS
        )
        if not (qty_ok and avg_ok):
            positions_diff.append(
                {
                    "symbol": sym,
                    "json": {"quantity": j_qty, "avg_price": j_avg},
                    "sqlite": {"quantity": s_qty, "avg_price": s_avg},
                }
            )

    # ------------------------------------------------------------------
    # Comparaison trade_plans
    # ------------------------------------------------------------------

    json_plans_by_id = {p.id: asdict(p) for p in json_plans}
    sqlite_plans_by_id = {p.id: asdict(p) for p in sqlite_plans}
    all_plan_ids = sorted(set(json_plans_by_id) | set(sqlite_plans_by_id))
    plans_diff: list[dict] = []
    for plan_id in all_plan_ids:
        j_plan = json_plans_by_id.get(plan_id)
        s_plan = sqlite_plans_by_id.get(plan_id)
        if j_plan != s_plan:
            plans_diff.append({"id": plan_id, "json": j_plan, "sqlite": s_plan})

    # ------------------------------------------------------------------
    # Comparaison scheduler
    # ------------------------------------------------------------------

    wakes_diff: list[dict] = []

    # Symboles avec override de réveil
    if json_symbols_with_wake != sqlite_symbols_with_wake:
        wakes_diff.append(
            {
                "field": "symbols_with_wake",
                "json": sorted(json_symbols_with_wake),
                "sqlite": sorted(sqlite_symbols_with_wake),
            }
        )

    # Réveil global par défaut
    if json_default_wake_norm != sqlite_default_wake_norm:
        wakes_diff.append(
            {
                "field": "default_next_wake",
                "json": json_default_wake_norm,
                "sqlite": sqlite_default_wake_norm,
            }
        )

    # Stale streaks
    all_stale_symbols = sorted(set(json_stale) | set(sqlite_stale))
    stale_diff: list[dict] = []
    for sym in all_stale_symbols:
        j_s = json_stale.get(sym, 0)
        s_s = sqlite_stale.get(sym, 0)
        if j_s != s_s:
            stale_diff.append({"symbol": sym, "json": j_s, "sqlite": s_s})

    # Indicator watches
    all_watch_ids = sorted(set(json_watches) | set(sqlite_watches))
    watches_diff: list[dict] = []
    for watch_id in all_watch_ids:
        j_w = json_watches.get(watch_id)
        s_w = sqlite_watches.get(watch_id)
        if j_w != s_w:
            watches_diff.append({"id": watch_id, "json": j_w, "sqlite": s_w})

    # ------------------------------------------------------------------
    # Résultat
    # ------------------------------------------------------------------

    identical = (
        cash_identical
        and not positions_diff
        and not plans_diff
        and not wakes_diff
        and not stale_diff
        and not watches_diff
    )

    return {
        "broker": {
            "cash": cash_info,
            "positions_diff": positions_diff,
        },
        "trade_plans": {
            "diff": plans_diff,
        },
        "scheduler": {
            "wakes_diff": wakes_diff,
            "watches_diff": watches_diff,
            "stale_diff": stale_diff,
        },
        "identical": identical,
    }


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(
            "Usage: python -m trader.state_db.compare <state_dir>",
            file=sys.stderr,
        )
        sys.exit(2)

    _state_dir = Path(sys.argv[1])
    _result = compare_backends(_state_dir)
    print(json.dumps(_result, indent=2, default=str))
    sys.exit(0 if _result["identical"] else 1)
