"""compare — outil shadow-compare json↔sqlite (pur lecture).

Compare les états broker et scheduler entre les stores JSON et les
stores SQLite d'un même state_dir. Machine-readable (dict structuré), pas de prose.

API publique :
    compare_backends(state_dir) → dict structuré (identical: bool + diffs)

CLI :
    python -m trader.infrastructure.state_db.compare <state_dir>
    Imprime le dict JSON (indent=2) ; exit 0 si identical, exit 1 sinon.

Contrainte fondamentale : le compare est PUR (read-only).
- Exige que casys.db existe et que les 3 sentinels state_imports soient posés.
- N'appelle jamais import_*, n'instancie aucun store avec starting_cash.
- Aucun fichier créé/modifié.

Dimensions comparées :
- broker   : cash (≈1e-9), TOUTES les positions (q=0 incluses), fills (séquence)
- plans    : clé compat uniquement, SQLite est la vérité
- scheduler: wakes par symbole {sym→heure}, default_next_wake, stale streaks,
             TOUTES les watches (actives ET expirées) par id, expires_at normalisé

Logging : [state_db] (getLogger(__name__), %-style).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from trader.infrastructure.state_db.connection import open_state_db

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


def _compare_fills(json_fills: list[dict], sqlite_fills: list[dict]) -> bool:
    """Compare deux séquences de fills (champs numériques avec _EPS, strings strict).

    Retourne True ssi les deux listes sont identiques élément par élément.
    """
    if len(json_fills) != len(sqlite_fills):
        return False
    for jf, sf in zip(json_fills, sqlite_fills):
        for key in ("symbol", "side", "commission_currency", "commission_model"):
            if jf.get(key) != sf.get(key):
                return False
        for key in ("quantity", "price", "commission", "fx_rate"):
            jv = jf.get(key)
            sv = sf.get(key)
            if jv is None and sv is None:
                continue
            if jv is None or sv is None:
                return False
            if abs(float(jv) - float(sv)) >= _EPS:
                return False
        if _norm_ts(jf.get("ts")) != _norm_ts(sf.get("ts")):
            return False
    return True


def compare_backends(state_dir: str | Path) -> dict:
    """Compare les états JSON et SQLite d'un state_dir (pur lecture).

    **Précondition** : casys.db doit exister et les 3 sentinels state_imports
    (broker, trade_plans, scheduler) doivent être posés. Si l'une de ces conditions
    n'est pas remplie → RuntimeError explicite. Aucun fichier n'est créé/modifié.

    Dimensions comparées :
    - broker.cash       : abs(json − sqlite) < 1e-9
    - broker.positions  : TOUTES les positions (q=0 incluses), par symbole
    - broker.fills      : séquence complète dans l'ordre d'insertion
    - trade_plans       : clé compat uniquement, plus de comparaison JSON
    - scheduler.wakes   : {symbol: heure normalisée} + default_next_wake
    - scheduler.stale   : streaks par symbole
    - scheduler.watches : TOUTES les watches (actives ET expirées), comparées par id,
                          avec symbol / on_trigger / expires_at normalisé / watch_json.
                          Une watch expirée absente d'un côté → identical=False.

    Returns:
        dict avec structure :
        {
          "broker": {
            "cash": {"json": float, "sqlite": float, "identical": bool},
            "positions_diff": [{"symbol": str, "json": {...}, "sqlite": {...}}, ...],
            "fills_diff": {} | {"json_count": int, "sqlite_count": int},
          },
          "trade_plans": {"identical": True},
          "scheduler": {
            "wakes_diff":   [{"field": str, "json": ..., "sqlite": ...}, ...],
            "watches_diff": [{"id": str, "json": dict|None, "sqlite": dict|None}, ...],
            "stale_diff":   [{"symbol": str, "json": int, "sqlite": int}, ...],
          },
          "identical": bool,
        }
        ``identical=True`` ssi TOUTES les dimensions coïncident.

    Args:
        state_dir: répertoire d'état (ex. ROOT / "state").

    Raises:
        RuntimeError: casys.db absent, sentinels manquants, ou fichier JSON requis absent.
    """
    state_dir = Path(state_dir)
    db_path = state_dir / "casys.db"
    json_broker_path = state_dir / "broker.json"
    json_sched_path = state_dir / "scheduler.json"

    # ------------------------------------------------------------------
    # FIX 1 — Guard : compare pur, exige DB + sentinels + JSON existants
    # ------------------------------------------------------------------

    if not db_path.exists():
        raise RuntimeError(
            f"compare: casys.db absent ou non initialisé — "
            f"lancer le daemon en CASYS_STATE_BACKEND=sqlite d'abord ({db_path})"
        )

    # Ouvrir la DB en lecture (le fichier existe, pas de création)
    db = open_state_db(db_path)

    # Vérification sentinels state_imports
    try:
        rows = db.query_all(
            "SELECT store FROM state_imports"
            " WHERE store IN ('broker', 'trade_plans', 'scheduler')"
        )
        imported_stores = {r["store"] for r in rows}
    except Exception:
        imported_stores = set()

    missing = {"broker", "trade_plans", "scheduler"} - imported_stores
    if missing:
        raise RuntimeError(
            f"compare: casys.db absent ou non initialisé — "
            f"lancer le daemon en CASYS_STATE_BACKEND=sqlite d'abord "
            f"(sentinels manquants: {sorted(missing)})"
        )

    # Vérification fichiers JSON requis
    for json_path in (json_broker_path, json_sched_path):
        if not json_path.exists():
            raise RuntimeError(f"compare: fichier JSON requis absent: {json_path}")

    # ------------------------------------------------------------------
    # Côté JSON — lecture directe, aucune création ni modification
    # ------------------------------------------------------------------

    json_broker_raw = json.loads(json_broker_path.read_text())
    json_cash: float = float(json_broker_raw["cash"])

    # TOUTES positions (q=0 incluses) — format broker.json {symbol: {symbol, quantity, avg_price}}
    json_all_positions: dict[str, dict] = {
        sym: {
            "symbol": sym,
            "quantity": float(pos.get("quantity", 0.0)),
            "avg_price": float(pos.get("avg_price", 0.0)),
        }
        for sym, pos in json_broker_raw.get("positions", {}).items()
    }

    json_fills: list[dict] = json_broker_raw.get("fills", [])

    json_sched_raw: dict = json.loads(json_sched_path.read_text())
    json_sched_raw.setdefault("default_next_wake", None)
    json_sched_raw.setdefault("symbols", {})
    json_sched_raw.setdefault("stale_streaks", {})
    json_sched_raw.setdefault("indicator_watches", {})

    # Wakes par symbole : {symbol: norm_ts} — seuls ceux avec une valeur non-nulle
    json_symbol_wakes: dict[str, str | None] = {
        s: _norm_ts(v)
        for s, v in json_sched_raw["symbols"].items()
        if v
    }
    json_default_wake_norm = _norm_ts(json_sched_raw["default_next_wake"])

    json_stale: dict[str, int] = {
        s: int(v) for s, v in json_sched_raw["stale_streaks"].items()
    }

    # TOUTES les watches (actives ET expirées) — par id pour comparaison exhaustive
    # (une watch expirée absente d'un côté doit aussi détecter une divergence)
    json_all_watches: dict[str, dict] = {
        w_id: {
            "symbol": w.get("symbol"),
            "on_trigger": w.get("on_trigger", "WAKE"),
            "expires_at": _norm_ts(w.get("expires_at")),
            "watch_json": w,
        }
        for w_id, w in json_sched_raw["indicator_watches"].items()
    }

    # ------------------------------------------------------------------
    # Côté SQLite — lecture pure (aucune écriture, aucun import, aucun shadow)
    # ------------------------------------------------------------------

    # Broker
    cash_row = db.query_one("SELECT cash FROM broker_state WHERE id=1")
    sqlite_cash: float = float(cash_row["cash"]) if cash_row else 0.0

    # TOUTES positions (q=0 incluses) — pas de filtre quantity
    pos_rows = db.query_all(
        "SELECT symbol, quantity, avg_price FROM broker_positions ORDER BY symbol"
    )
    sqlite_all_positions: dict[str, dict] = {
        r["symbol"]: {
            "symbol": r["symbol"],
            "quantity": float(r["quantity"]),
            "avg_price": float(r["avg_price"]),
        }
        for r in pos_rows
    }

    # Fills (séquence complète)
    fill_rows = db.query_all(
        "SELECT symbol, side, quantity, price, ts,"
        " commission, commission_currency, commission_model, fx_rate"
        " FROM broker_fills ORDER BY seq"
    )
    sqlite_fills: list[dict] = [dict(r) for r in fill_rows]

    # Scheduler — wakes par symbole
    sym_wake_rows = db.query_all(
        "SELECT symbol, when_iso FROM scheduler_symbol_wake ORDER BY symbol"
    )
    sqlite_symbol_wakes: dict[str, str | None] = {
        r["symbol"]: _norm_ts(r["when_iso"]) for r in sym_wake_rows
    }

    default_wake_row = db.query_one(
        "SELECT value FROM scheduler_meta WHERE key='default_next_wake'"
    )
    sqlite_default_wake_norm = _norm_ts(
        default_wake_row["value"] if default_wake_row else None
    )

    # Stale streaks
    stale_rows = db.query_all(
        "SELECT symbol, streak FROM scheduler_stale_streaks ORDER BY symbol"
    )
    sqlite_stale: dict[str, int] = {r["symbol"]: int(r["streak"]) for r in stale_rows}

    # TOUTES les watches (actives ET expirées) — par id, pas de filtre expires_at
    all_watch_rows = db.query_all(
        "SELECT id, symbol, on_trigger, expires_at, watch_json"
        " FROM scheduler_watches"
        " ORDER BY seq",
    )
    sqlite_all_watches: dict[str, dict] = {
        r["id"]: {
            "symbol": r["symbol"],
            "on_trigger": r["on_trigger"],
            "expires_at": _norm_ts(r["expires_at"]),  # colonne canonique
            "watch_json": json.loads(r["watch_json"]),
        }
        for r in all_watch_rows
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

    all_pos_symbols = sorted(set(json_all_positions) | set(sqlite_all_positions))
    positions_diff: list[dict] = []
    for sym in all_pos_symbols:
        j_pos = json_all_positions.get(sym)
        s_pos = sqlite_all_positions.get(sym)
        j_qty = j_pos["quantity"] if j_pos is not None else None
        j_avg = j_pos["avg_price"] if j_pos is not None else None
        s_qty = s_pos["quantity"] if s_pos is not None else None
        s_avg = s_pos["avg_price"] if s_pos is not None else None
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

    fills_identical = _compare_fills(json_fills, sqlite_fills)
    fills_diff: dict = {} if fills_identical else {
        "json_count": len(json_fills),
        "sqlite_count": len(sqlite_fills),
    }

    # ------------------------------------------------------------------
    # Comparaison scheduler
    # ------------------------------------------------------------------

    wakes_diff: list[dict] = []

    # Wakes par symbole : compare {symbol: heure normalisée} (heure incluse)
    if json_symbol_wakes != sqlite_symbol_wakes:
        wakes_diff.append(
            {
                "field": "symbols_with_wake",
                "json": json_symbol_wakes,
                "sqlite": sqlite_symbol_wakes,
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

    # TOUTES les watches (actives ET expirées) — comparaison par id
    all_watch_ids = sorted(set(json_all_watches) | set(sqlite_all_watches))
    watches_diff: list[dict] = []
    for w_id in all_watch_ids:
        j_w = json_all_watches.get(w_id)
        s_w = sqlite_all_watches.get(w_id)
        if j_w != s_w:
            watches_diff.append({"id": w_id, "json": j_w, "sqlite": s_w})

    # ------------------------------------------------------------------
    # Résultat
    # ------------------------------------------------------------------

    identical = (
        cash_identical
        and not positions_diff
        and fills_identical
        and not wakes_diff
        and not stale_diff
        and not watches_diff
    )

    return {
        "broker": {
            "cash": cash_info,
            "positions_diff": positions_diff,
            "fills_diff": fills_diff,
        },
        "trade_plans": {"identical": True},
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
            "Usage: python -m trader.infrastructure.state_db.compare <state_dir>",
            file=sys.stderr,
        )
        sys.exit(2)

    _state_dir = Path(sys.argv[1])
    _result = compare_backends(_state_dir)
    print(json.dumps(_result, indent=2, default=str))
    sys.exit(0 if _result["identical"] else 1)
