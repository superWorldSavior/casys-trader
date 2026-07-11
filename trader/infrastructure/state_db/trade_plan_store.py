"""SqliteTradePlanStore — persistance TradePlan sur substrat SQLite.

API publique persistée :
    open_plans()  → list[TradePlan]
    upsert(plan)
    close(id)
    close_symbol(symbol)
    sync_symbol_quantity(symbol, remaining_quantity)
    clear()

Fonctions pures de mapping (utilisées aussi par import_trade_plans_from_json) :
    plan_to_columns(plan, seq) → dict
    row_to_plan(row)           → TradePlan

L'ordre d'open_plans() est préservé via la colonne seq (ORDER BY seq).

Logging : [state_db] (getLogger(__name__), %-style).
"""
from __future__ import annotations

import json
import logging

from trader.domain.trade_plan import TradePlan
from trader.infrastructure.state_db.connection import StateDb

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mapping pur TradePlan ↔ colonnes SQLite
# ---------------------------------------------------------------------------


def plan_to_columns(plan: TradePlan, seq: int) -> dict:
    """Convertit un TradePlan en dict de colonnes pour INSERT/UPDATE SQLite.

    Colonnes scalaires mappées directement ; sous-objets (TrailingStop,
    ProfitProtection, listes TakeProfit/str, dicts) sérialisés en JSON.
    Le JSON NULL Python → chaîne "null" (TEXT), pas SQL NULL.

    Non-finis scalaires (NaN, Inf) → NULL en colonne REAL, relu None par sqlite3.
    Ce comportement conserve la normalisation historique des plans persistés.

    Args:
        plan: TradePlan frozen à convertir.
        seq:  ordinal d'insertion (ORDER BY seq → ordre préservé).

    Returns:
        dict prêt pour sqlite3 named parameters (:col).
    """
    d = plan.model_dump()
    return {
        "id": d["id"],
        "seq": seq,
        "symbol": d["symbol"],
        "side": d["side"],
        "quantity": d["quantity"],
        "remaining_quantity": d["remaining_quantity"],
        "entry_price": d["entry_price"],
        "opened_at": d["opened_at"],
        "reference_volatility": d["reference_volatility"],
        "hard_stop_price": d["hard_stop_price"],
        "max_hold_minutes": d["max_hold_minutes"],
        "high_watermark": d["high_watermark"],
        "low_watermark": d["low_watermark"],
        "llm_provider": d["llm_provider"],
        "llm_model": d["llm_model"],
        "llm_fallback_reason": d["llm_fallback_reason"],
        "llm_confidence": d["llm_confidence"],
        "entry_thesis": d["entry_thesis"],
        "entry_decision_id": d["entry_decision_id"],
        # Sous-objets et listes → JSON TEXT (None Python → chaîne "null")
        "trailing_json": json.dumps(d["trailing_stop"]),
        "profit_protection_json": json.dumps(d["profit_protection"]),
        "take_profits_json": json.dumps(d["take_profits"]),
        "filled_take_profits_json": json.dumps(d["filled_take_profits"]),
        "exit_watch_json": json.dumps(d["exit_watch"]),
        "last_llm_review_json": json.dumps(d["last_llm_review"]),
        "entry_context_json": json.dumps(d["entry_context"]),
    }


def row_to_plan(row) -> TradePlan:
    """Reconstruit un TradePlan depuis une ligne SQLite (Row ou dict).

    Désérialise les colonnes *_json, reconstruit le dict complet, puis valide
    directement le contrat domaine persistant.

    Round-trip garanti : row_to_plan(plan_to_columns(p, seq)).model_dump() == p.model_dump().
    """
    d = dict(row)

    def _load(col: str):
        val = d.get(col)
        if val is None:  # SQL NULL (ne devrait pas arriver avec nos inserts)
            return None
        return json.loads(val)  # "null" → None, "[...]" → list, "{...}" → dict

    raw = {
        "id": d["id"],
        "symbol": d["symbol"],
        "side": d["side"],
        "quantity": d["quantity"],
        "remaining_quantity": d["remaining_quantity"],
        "entry_price": d["entry_price"],
        "opened_at": d["opened_at"],
        "reference_volatility": d["reference_volatility"],
        "hard_stop_price": d["hard_stop_price"],
        "max_hold_minutes": d["max_hold_minutes"],
        "high_watermark": d["high_watermark"],
        "low_watermark": d["low_watermark"],
        "llm_provider": d["llm_provider"],
        "llm_model": d["llm_model"],
        "llm_fallback_reason": d["llm_fallback_reason"],
        "llm_confidence": d["llm_confidence"],
        "entry_thesis": d["entry_thesis"],
        "entry_decision_id": d["entry_decision_id"],
        # JSON columns
        "trailing_stop": _load("trailing_json"),
        "profit_protection": _load("profit_protection_json"),
        "take_profits": _load("take_profits_json") or [],
        "filled_take_profits": _load("filled_take_profits_json") or [],
        "exit_watch": _load("exit_watch_json"),
        "last_llm_review": _load("last_llm_review_json"),
        "entry_context": _load("entry_context_json"),
    }
    return TradePlan.model_validate(raw)


# ---------------------------------------------------------------------------
# SqliteTradePlanStore
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO trade_plans(
    id, seq, symbol, side, quantity, remaining_quantity,
    entry_price, opened_at, reference_volatility, hard_stop_price,
    max_hold_minutes, high_watermark, low_watermark,
    llm_provider, llm_model, llm_fallback_reason, llm_confidence,
    entry_thesis, entry_decision_id,
    trailing_json, profit_protection_json, take_profits_json,
    filled_take_profits_json, exit_watch_json,
    last_llm_review_json, entry_context_json
) VALUES (
    :id,
    (SELECT COALESCE(MAX(seq), 0) + 1 FROM trade_plans),
    :symbol, :side, :quantity, :remaining_quantity,
    :entry_price, :opened_at, :reference_volatility, :hard_stop_price,
    :max_hold_minutes, :high_watermark, :low_watermark,
    :llm_provider, :llm_model, :llm_fallback_reason, :llm_confidence,
    :entry_thesis, :entry_decision_id,
    :trailing_json, :profit_protection_json, :take_profits_json,
    :filled_take_profits_json, :exit_watch_json,
    :last_llm_review_json, :entry_context_json
)
ON CONFLICT(id) DO UPDATE SET
    seq                    = (SELECT COALESCE(MAX(seq), 0) + 1 FROM trade_plans),
    symbol                 = excluded.symbol,
    side                   = excluded.side,
    quantity               = excluded.quantity,
    remaining_quantity     = excluded.remaining_quantity,
    entry_price            = excluded.entry_price,
    opened_at              = excluded.opened_at,
    reference_volatility   = excluded.reference_volatility,
    hard_stop_price        = excluded.hard_stop_price,
    max_hold_minutes       = excluded.max_hold_minutes,
    high_watermark         = excluded.high_watermark,
    low_watermark          = excluded.low_watermark,
    llm_provider           = excluded.llm_provider,
    llm_model              = excluded.llm_model,
    llm_fallback_reason    = excluded.llm_fallback_reason,
    llm_confidence         = excluded.llm_confidence,
    entry_thesis           = excluded.entry_thesis,
    entry_decision_id      = excluded.entry_decision_id,
    trailing_json          = excluded.trailing_json,
    profit_protection_json = excluded.profit_protection_json,
    take_profits_json      = excluded.take_profits_json,
    filled_take_profits_json = excluded.filled_take_profits_json,
    exit_watch_json        = excluded.exit_watch_json,
    last_llm_review_json   = excluded.last_llm_review_json,
    entry_context_json     = excluded.entry_context_json
"""


class SqliteTradePlanStore:
    """TradePlanStore sur substrat SQLite (même API publique que TradePlanStore).

    Args:
        db: StateDb ouverte avec TRADE_PLANS_MIGRATION appliquée.
    """

    def __init__(self, db: StateDb) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # API publique (identique à TradePlanStore)
    # ------------------------------------------------------------------

    def open_plans(self) -> list[TradePlan]:
        """Retourne tous les plans ouverts, triés par seq (ordre d'insertion/upsert)."""
        rows = self._db.query_all("SELECT * FROM trade_plans ORDER BY seq")
        return [row_to_plan(r) for r in rows]

    def upsert(self, plan: TradePlan) -> None:
        """Insère ou met à jour un plan.

        Si le plan existe déjà (même id), il est mis à jour ET déplacé en fin de
        liste (seq = MAX(seq)+1) — comportement identique au « retire + append »
        du TradePlanStore JSON.
        """
        with self._db.transaction() as cur:
            self.upsert_in_tx(cur, plan)

    def upsert_in_tx(self, cur, plan: TradePlan) -> None:
        """Variante transactionnelle de upsert : écrit sur un curseur fourni.

        N'ouvre PAS de transaction, n'écrit PAS le shadow.
        À appeler exclusivement depuis l'intérieur d'un bloc ``with db.transaction() as cur:``.

        Args:
            cur:  Curseur SQLite déjà dans une transaction BEGIN IMMEDIATE.
            plan: TradePlan à insérer ou mettre à jour.
        """
        cols = plan_to_columns(plan, seq=0)  # seq calculé inline en SQL
        cur.execute(_UPSERT_SQL, cols)
        log.debug("[state_db] upsert_in_tx plan %s (%s)", plan.id, plan.symbol)

    def close(self, plan_id: str) -> None:
        """Supprime le plan avec cet id."""
        with self._db.transaction() as cur:
            cur.execute("DELETE FROM trade_plans WHERE id=?", (plan_id,))
        log.debug("[state_db] close plan %s", plan_id)

    def close_symbol(self, symbol: str) -> None:
        """Supprime tous les plans du symbole."""
        with self._db.transaction() as cur:
            self.close_symbol_in_tx(cur, symbol)
        log.debug("[state_db] close_symbol %s", symbol)

    def close_symbol_in_tx(self, cur, symbol: str) -> None:
        """Variante transactionnelle de close_symbol : écrit sur un curseur fourni.

        N'ouvre PAS de transaction, n'écrit PAS le shadow.
        À appeler exclusivement depuis l'intérieur d'un bloc ``with db.transaction() as cur:``.

        Args:
            cur:    Curseur SQLite déjà dans une transaction BEGIN IMMEDIATE.
            symbol: Symbole dont supprimer tous les plans ouverts.
        """
        cur.execute("DELETE FROM trade_plans WHERE symbol=?", (symbol,))
        log.debug("[state_db] close_symbol_in_tx %s", symbol)

    def clear(self) -> None:
        """Supprime TOUS les plans."""
        with self._db.transaction() as cur:
            cur.execute("DELETE FROM trade_plans")
        log.debug("[state_db] clear trade_plans")

    def sync_symbol_quantity(self, symbol: str, remaining_quantity: float) -> None:
        """Synchronise la quantité totale restante du symbole avec le broker.

        - Si remaining_quantity <= 0 : ferme tous les plans du symbole.
        - Sinon : calcule ratio = remaining / total_remaining du symbole,
          rescale remaining_quantity de chaque plan + TP non remplis.
          Les TP déjà remplis (dans filled_take_profits) sont conservés tels quels.
          Le tout dans UNE transaction.

        Porte fidèlement la logique de trade_plan.py:1045-1079.
        """
        with self._db.transaction() as cur:
            self.sync_symbol_quantity_in_tx(cur, symbol, remaining_quantity)

        log.debug(
            "[state_db] sync_symbol_quantity %s remaining=%.4f", symbol, remaining_quantity
        )

    def sync_symbol_quantity_in_tx(
        self, cur, symbol: str, remaining_quantity: float
    ) -> None:
        """Variante transactionnelle de ``sync_symbol_quantity``.

        N'ouvre PAS de transaction. Cette primitive permet à l'UoW d'exécution
        d'atomiser le fill REDUCE et la resynchronisation du plan.
        """
        if remaining_quantity <= 0:
            self.close_symbol_in_tx(cur, symbol)
            return

        rows = cur.execute(
            "SELECT * FROM trade_plans WHERE symbol=? ORDER BY seq",
            (symbol,),
        ).fetchall()

        symbol_plans = [row_to_plan(r) for r in rows]
        total_remaining = sum(p.remaining_quantity for p in symbol_plans)

        if total_remaining <= 0:
            cur.execute("DELETE FROM trade_plans WHERE symbol=?", (symbol,))
            return

        ratio = remaining_quantity / total_remaining
        for row, plan in zip(rows, symbol_plans):
            new_remaining = round(plan.remaining_quantity * ratio, 8)
            if new_remaining <= 0:
                cur.execute("DELETE FROM trade_plans WHERE id=?", (plan.id,))
                continue
            # Rescale TP non remplis ; remplis conservés tels quels
            take_profits = [
                tp
                if tp.name in plan.filled_take_profits
                else tp.model_copy(update={"quantity": round(tp.quantity * ratio, 8)})
                for tp in plan.take_profits
            ]
            updated = plan.model_copy(
                update={
                    "remaining_quantity": new_remaining,
                    "take_profits": take_profits,
                }
            )
            cols = plan_to_columns(updated, int(row["seq"]))
            cur.execute(
                "UPDATE trade_plans"
                " SET remaining_quantity=?, take_profits_json=?"
                " WHERE id=?",
                (cols["remaining_quantity"], cols["take_profits_json"], plan.id),
            )

        log.debug(
            "[state_db] sync_symbol_quantity_in_tx %s remaining=%.4f",
            symbol,
            remaining_quantity,
        )
