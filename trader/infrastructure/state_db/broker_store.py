"""SqliteBroker — Broker paper sur substrat SQLite.

Implémente le port Broker (trader/execution/ports.py) avec StateDb comme
backend. L'état est déjà présent dans les tables (via import_broker_from_json)
avant l'instanciation — pas de starting_cash ici.

Logging : [state_db] (getLogger(__name__), %-style).
"""

from __future__ import annotations

import logging

from trader.domain.contracts import Commission, Fill, Order, Position
from trader.domain.execution.fill_accounting import POSITION_EPSILON, compute_fill_effect
from trader.infrastructure.brokers.commission_models import NoCommissionModel
from trader.infrastructure.state_db.connection import StateDb

log = logging.getLogger(__name__)


class SqliteBroker:
    """Broker paper sur substrat SQLite (Protocol Broker).

    Args:
        db:               StateDb ouverte avec BROKER_MIGRATION appliquée.
        commission_model: modèle de commission (défaut : NoCommissionModel).
    """

    def __init__(
        self,
        db: StateDb,
        commission_model=None,
    ) -> None:
        self._db = db
        self._commission_model = commission_model or NoCommissionModel()

    # ------------------------------------------------------------------
    # Protocol Broker
    # ------------------------------------------------------------------

    def cash(self) -> float:
        """Retourne le cash courant depuis broker_state."""
        row = self._db.query_one("SELECT cash FROM broker_state WHERE id=1")
        if row is None:
            raise RuntimeError("broker_state absent — appeler import_broker_from_json avant SqliteBroker")
        return float(row["cash"])

    def positions(self) -> dict[str, Position]:
        """Retourne les positions non-nulles (filtre aussi la poussière float)."""
        rows = self._db.query_all(
            "SELECT symbol, quantity, avg_price FROM broker_positions WHERE ABS(quantity) > ?",
            (POSITION_EPSILON,),
        )
        return {
            r["symbol"]: Position(
                symbol=r["symbol"],
                quantity=float(r["quantity"]),
                avg_price=float(r["avg_price"]),
            )
            for r in rows
        }

    def fills(self) -> list[dict]:
        """Retourne les fills dans le format ``broker.json`` historique."""
        rows = self._db.query_all(
            "SELECT symbol, side, quantity, price, ts,"
            " commission, commission_currency, commission_model, fx_rate,"
            " process_instance_id, attempt_id, decision_id"
            " FROM broker_fills ORDER BY seq"
        )
        fills: list[dict] = []
        for row in rows:
            fill = {
                "symbol": row["symbol"],
                "side": row["side"],
                "quantity": row["quantity"],
                "price": row["price"],
                "ts": row["ts"],
                "commission": row["commission"],
                "commission_currency": row["commission_currency"],
                "commission_model": row["commission_model"],
                "fx_rate": row["fx_rate"],
            }
            fill.update(
                {key: row[key] for key in ("process_instance_id", "attempt_id", "decision_id") if row[key] is not None}
            )
            fills.append(fill)
        return fills

    def submit(
        self,
        order: Order,
        price: float,
        ts: str,
        dry_run: bool = True,
        fx_rate: float = 1.0,
    ) -> Fill | None:
        """Soumet un ordre paper.

        dry_run=True (défaut Safe) : ne mute rien, retourne None.
        dry_run=False : mutation position+cash+fill atomique en une transaction SQLite
                        (délègue à submit_in_tx).
        """
        if dry_run:
            return None

        with self._db.transaction() as cur:
            fill = self.submit_in_tx(cur, order, price, ts, dry_run=False, fx_rate=fx_rate)

        return fill

    def submit_in_tx(
        self,
        cur,
        order: Order,
        price: float,
        ts: str,
        *,
        dry_run: bool = False,
        fx_rate: float = 1.0,
    ) -> Fill | None:
        """Variante transactionnelle de submit : écrit sur un curseur fourni.

        N'ouvre PAS de transaction.
        À appeler exclusivement depuis l'intérieur d'un bloc ``with db.transaction() as cur:``.

        dry_run=True : calcule le fill, ne mute rien, retourne None.
        dry_run=False : effectue les écritures SQL via ``cur`` et retourne le Fill.

        Args:
            cur:     Curseur SQLite déjà dans une transaction BEGIN IMMEDIATE.
            order:   Ordre à soumettre.
            price:   Prix d'exécution.
            ts:      Timestamp du fill (ISO string).
            dry_run: Si True, lecture seule — aucune écriture.
            fx_rate: Taux de change (devise native → USD).

        Returns:
            Fill si dry_run=False, None si dry_run=True.
        """
        commission: Commission = self._commission_model.calculate(order, price)
        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            ts=ts,
            commission=commission.amount,
            commission_currency=commission.currency,
            commission_model=commission.model,
            fx_rate=fx_rate,
            process_instance_id=order.process_instance_id,
            attempt_id=order.attempt_id,
            decision_id=order.decision_id,
        )

        if dry_run:
            return None

        row = cur.execute(
            "SELECT quantity, avg_price FROM broker_positions WHERE symbol=?",
            (order.symbol,),
        ).fetchone()
        old_qty = float(row["quantity"]) if row is not None else 0.0
        old_avg = float(row["avg_price"]) if row is not None else 0.0

        new_qty, new_avg_price, cash_delta_total = compute_fill_effect(
            old_quantity=old_qty,
            old_avg_price=old_avg,
            order=order,
            price=price,
            fx_rate=fx_rate,
            commission=commission,
        )

        log.debug(
            "[state_db] submit_in_tx %s %s qty=%.4f price=%.4f new_qty=%.4f cash_delta_usd=%.4f",
            order.side,
            order.symbol,
            order.quantity,
            price,
            new_qty,
            cash_delta_total,
        )

        cur.execute(
            "INSERT INTO broker_positions(symbol, quantity, avg_price) VALUES (?,?,?)"
            " ON CONFLICT(symbol) DO UPDATE SET"
            " quantity=excluded.quantity, avg_price=excluded.avg_price",
            (order.symbol, new_qty, new_avg_price),
        )
        cur.execute(
            "UPDATE broker_state SET cash = cash - ? WHERE id = 1",
            (cash_delta_total,),
        )
        fill_values = (
            fill.symbol,
            fill.side,
            fill.quantity,
            fill.price,
            fill.ts,
            fill.commission,
            fill.commission_currency,
            fill.commission_model,
            fill.fx_rate,
        )
        if all(value is None for value in (fill.process_instance_id, fill.attempt_id, fill.decision_id)):
            cur.execute(
                "INSERT INTO broker_fills"
                "(symbol, side, quantity, price, ts,"
                " commission, commission_currency, commission_model, fx_rate)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                fill_values,
            )
        else:
            cur.execute(
                "INSERT INTO broker_fills"
                "(symbol, side, quantity, price, ts,"
                " commission, commission_currency, commission_model, fx_rate,"
                " process_instance_id, attempt_id, decision_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*fill_values, fill.process_instance_id, fill.attempt_id, fill.decision_id),
            )

        return fill
