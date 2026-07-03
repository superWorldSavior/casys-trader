"""Migrations SQLite pour les stores d'état (broker, plans, scheduler).

Contient :
- BROKER_MIGRATION : schéma broker v1 (broker_state, broker_positions, broker_fills)
- import_broker_from_json : migration one-shot idempotente depuis broker.json
- TRADE_PLANS_MIGRATION : schéma trade_plans v2
- import_trade_plans_from_json : migration one-shot idempotente depuis trade_plans.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from trader.state_db.connection import StateDb

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Broker — migration v1
# ---------------------------------------------------------------------------

BROKER_MIGRATION: tuple[int, list[str]] = (
    1,
    [
        """CREATE TABLE broker_state (
            id   INTEGER PRIMARY KEY CHECK(id=1),
            cash REAL NOT NULL
        )""",
        """CREATE TABLE broker_positions (
            symbol    TEXT PRIMARY KEY,
            quantity  REAL NOT NULL,
            avg_price REAL NOT NULL
        )""",
        """CREATE TABLE broker_fills (
            seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol              TEXT,
            side                TEXT,
            quantity            REAL,
            price               REAL,
            ts                  TEXT,
            commission          REAL DEFAULT 0.0,
            commission_currency TEXT DEFAULT 'USD',
            commission_model    TEXT DEFAULT 'none',
            fx_rate             REAL DEFAULT 1.0
        )""",
    ],
)


def import_broker_from_json(
    db: StateDb,
    json_path: Path,
    *,
    starting_cash: float,
) -> None:
    """Migration one-shot idempotente : importe broker.json dans les tables SQLite.

    - Applique BROKER_MIGRATION (idempotent).
    - Si broker_state n'est pas vide : skip (déjà migré), pas de log.
    - Si le JSON existe : backup horodaté (isoformat UTC sans ':'), puis import en
      une transaction (broker_state + positions + fills, defaults patchés sur anciens
      fills). Positions q==0 conservées.
    - Si le JSON n'existe pas : broker neuf → insert broker_state(id=1, cash=starting_cash)
      dans une transaction.
    """
    db.apply_migrations([BROKER_MIGRATION])

    # Idempotence : déjà migré → no-op silencieux
    if not db.table_is_empty("broker_state"):
        return

    json_path = Path(json_path)

    if json_path.exists():
        # 1. Parse/validate AVANT toute mutation (JSON invalide → JSONDecodeError, fichier intact)
        raw = json.loads(json_path.read_text())
        cash: float = raw["cash"]
        # positions = dict symbol → dict ; on itère les valeurs
        positions: list[dict] = list(raw.get("positions", {}).values())
        fills: list[dict] = raw.get("fills", [])

        # 2. Import atomique dans la base
        with db.transaction() as cur:
            cur.execute("INSERT INTO broker_state(id, cash) VALUES (1, ?)", (cash,))
            for pos in positions:
                cur.execute(
                    "INSERT INTO broker_positions(symbol, quantity, avg_price)"
                    " VALUES (?, ?, ?)",
                    (pos["symbol"], pos["quantity"], pos["avg_price"]),
                )
            for fill in fills:
                cur.execute(
                    "INSERT INTO broker_fills"
                    "(symbol, side, quantity, price, ts,"
                    " commission, commission_currency, commission_model, fx_rate)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fill.get("symbol"),
                        fill.get("side"),
                        fill.get("quantity"),
                        fill.get("price"),
                        fill.get("ts"),
                        fill.get("commission", 0.0),
                        fill.get("commission_currency", "USD"),
                        fill.get("commission_model", "none"),
                        fill.get("fx_rate", 1.0),
                    ),
                )

        # 3. Backup horodaté SEULEMENT après commit réussi (JSON original intact en cas d'erreur)
        ts = datetime.now(timezone.utc).isoformat().replace(":", "")
        backup_path = json_path.with_name(json_path.name + f".bak-{ts}")
        json_path.rename(backup_path)

        log.info(
            "[state_db] broker importé: %d positions, %d fills, cash=%.2f",
            len(positions),
            len(fills),
            cash,
        )
    else:
        # Broker neuf : initialiser avec starting_cash
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO broker_state(id, cash) VALUES (1, ?)",
                (starting_cash,),
            )


# ---------------------------------------------------------------------------
# TradePlans — migration v2
# ---------------------------------------------------------------------------

TRADE_PLANS_MIGRATION: tuple[int, list[str]] = (
    2,
    [
        """CREATE TABLE trade_plans (
            id                    TEXT PRIMARY KEY,
            seq                   INTEGER,
            symbol                TEXT NOT NULL,
            side                  TEXT,
            quantity              REAL,
            remaining_quantity    REAL,
            entry_price           REAL,
            opened_at             TEXT,
            reference_volatility  REAL,
            hard_stop_price       REAL,
            max_hold_minutes      REAL,
            high_watermark        REAL,
            low_watermark         REAL,
            trailing_json         TEXT,
            profit_protection_json TEXT,
            take_profits_json     TEXT,
            filled_take_profits_json TEXT,
            exit_watch_json       TEXT,
            last_llm_review_json  TEXT,
            entry_context_json    TEXT,
            llm_provider          TEXT,
            llm_model             TEXT,
            llm_fallback_reason   TEXT,
            llm_confidence        REAL,
            entry_thesis          TEXT,
            entry_decision_id     TEXT
        )""",
        "CREATE INDEX idx_trade_plans_symbol ON trade_plans(symbol)",
    ],
)


def import_trade_plans_from_json(db: StateDb, json_path: Path) -> None:
    """Migration one-shot idempotente : importe trade_plans.json dans les tables SQLite.

    - Applique TRADE_PLANS_MIGRATION (idempotent).
    - Si trade_plans n'est pas vide : skip (déjà migré), pas de log.
    - Si le JSON existe : parse/valide AVANT toute mutation (JSON invalide → erreur,
      fichier intact) ; import en une transaction (un plan = une ligne, seq = index) ;
      backup horodaté SEULEMENT après commit réussi.
    - Si le JSON n'existe pas : démarrage avec liste vide (table déjà créée par la migration).
    """
    db.apply_migrations([TRADE_PLANS_MIGRATION])

    # Idempotence : déjà migré → no-op silencieux
    if not db.table_is_empty("trade_plans"):
        return

    json_path = Path(json_path)

    if not json_path.exists():
        log.info("[state_db] trade_plans: JSON absent, démarrage avec liste vide")
        return

    # 1. Parse/validate AVANT toute mutation (JSON invalide → exception, fichier intact)
    raw = json.loads(json_path.read_text())
    plans_raw: list[dict] = raw.get("plans", [])

    # Imports locaux pour éviter les dépendances circulaires au top-level
    from trader.state_db.trade_plan_store import plan_to_columns  # noqa: PLC0415
    from trader.planning.trade_plan import trade_plan_from_dict  # noqa: PLC0415

    # 2. Import atomique dans la base
    with db.transaction() as cur:
        for seq, plan_dict in enumerate(plans_raw, start=1):
            plan = trade_plan_from_dict(plan_dict)
            cols = plan_to_columns(plan, seq)
            cur.execute(
                """INSERT INTO trade_plans(
                    id, seq, symbol, side, quantity, remaining_quantity,
                    entry_price, opened_at, reference_volatility, hard_stop_price,
                    max_hold_minutes, high_watermark, low_watermark,
                    llm_provider, llm_model, llm_fallback_reason, llm_confidence,
                    entry_thesis, entry_decision_id,
                    trailing_json, profit_protection_json, take_profits_json,
                    filled_take_profits_json, exit_watch_json,
                    last_llm_review_json, entry_context_json
                ) VALUES (
                    :id, :seq, :symbol, :side, :quantity, :remaining_quantity,
                    :entry_price, :opened_at, :reference_volatility, :hard_stop_price,
                    :max_hold_minutes, :high_watermark, :low_watermark,
                    :llm_provider, :llm_model, :llm_fallback_reason, :llm_confidence,
                    :entry_thesis, :entry_decision_id,
                    :trailing_json, :profit_protection_json, :take_profits_json,
                    :filled_take_profits_json, :exit_watch_json,
                    :last_llm_review_json, :entry_context_json
                )""",
                cols,
            )

    # 3. Backup horodaté SEULEMENT après commit réussi (JSON original intact en cas d'erreur)
    ts = datetime.now(timezone.utc).isoformat().replace(":", "")
    backup_path = json_path.with_name(json_path.name + f".bak-{ts}")
    json_path.rename(backup_path)

    log.info("[state_db] trade_plans importés: %d plans", len(plans_raw))
