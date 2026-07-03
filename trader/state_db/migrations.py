"""Migrations SQLite pour les stores d'état (broker, plans, scheduler).

Contient :
- BROKER_MIGRATION : schéma broker v1 (broker_state, broker_positions, broker_fills)
- import_broker_from_json : migration one-shot idempotente depuis broker.json
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
