"""execute_order_unit — UnitOfWork atomique broker + plan + ledger.

Ouvre UNE transaction SQLite : submit_in_tx → plan (upsert ou close) →
complete_in_tx. Si N'IMPORTE quelle étape lève → ROLLBACK total (pas de fill
sans plan, pas de cash muté sans task done). Après COMMIT réussi : shadows
best-effort hors transaction (broker.json + trade_plans.json).

Précondition : broker, plan_store et ledger doivent partager le MÊME StateDb
(db). Sans ça, l'atomicité n'est PAS garantie — les écritures seraient
réparties sur des connexions distinctes.

Logging : [unit_of_work] (getLogger(__name__), %-style).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.state_db.connection import StateDb
    from trader.state_db.broker_store import SqliteBroker
    from trader.state_db.trade_plan_store import SqliteTradePlanStore
    from trader.queue.ledger import TaskLedger
    from trader.tools.execution import Fill, Order
    from trader.planning.trade_plan import TradePlan

log = logging.getLogger(__name__)


def execute_order_unit(
    *,
    db: "StateDb",
    broker: "SqliteBroker",
    plan_store: "SqliteTradePlanStore",
    ledger: "TaskLedger",
    order: "Order",
    price: float,
    ts: str,
    fx_rate: float,
    dry_run: bool,
    plan_to_upsert: "TradePlan | None" = None,
    symbol_to_close: str | None = None,
    task_id: int,
    token: str,
    now_ms: int,
) -> "Fill | None":
    """Exécute broker + plan + ledger dans UNE transaction SQLite atomique.

    L'ordre des opérations dans la transaction :
      1. submit_in_tx  — écrit positions/cash/fill (ou no-op si dry_run=True).
      2. plan          — close_symbol_in_tx OU upsert_in_tx (si fourni).
      3. complete_in_tx — passe la tâche à 'done' (fencing token).

    Si N'IMPORTE quelle étape lève → ROLLBACK complet de la transaction :
    aucune des trois écritures n'est persistée. Après COMMIT réussi : les
    shadows broker.json et trade_plans.json sont régénérés best-effort hors
    transaction.

    Précondition : broker._db, plan_store._db et ledger._db doivent tous être
    la même instance que ``db``. Un assert-log (pas un assert dur) signale
    l'incohérence sans interrompre l'exécution, mais l'atomicité n'est plus
    garantie dans ce cas.

    Args:
        db:              StateDb partagé — substrat unique de toutes les écritures.
        broker:          SqliteBroker pour le submit de l'ordre.
        plan_store:      SqliteTradePlanStore pour l'upsert ou close du plan.
        ledger:          TaskLedger pour la complétion de la tâche.
        order:           Ordre à soumettre au broker.
        price:           Prix d'exécution.
        ts:              Timestamp du fill (ISO string).
        fx_rate:         Taux de change devise native → USD.
        dry_run:         Si True, aucune écriture SQL (submit_in_tx no-op).
        plan_to_upsert:  TradePlan à upsert après le fill (ignoré si symbol_to_close).
        symbol_to_close: Symbole dont fermer tous les plans ouverts (prioritaire).
        task_id:         ID de la tâche ledger à compléter.
        token:           Token de fencing de la tâche (claim_token attendu).
        now_ms:          Timestamp courant en epoch ms (injecté pour déterminisme).

    Returns:
        Fill retourné par submit_in_tx (None si dry_run=True ou si la tâche
        était déjà done / token invalide ne lève pas).

    Raises:
        Toute exception levée dans la transaction → ROLLBACK total + re-raise.
    """
    # Vérification de cohérence du StateDb partagé (best-effort, pas un assert dur)
    if broker._db is not db:
        log.warning(
            "[unit_of_work] broker._db is not db — atomicité NON garantie"
        )
    if plan_store._db is not db:
        log.warning(
            "[unit_of_work] plan_store._db is not db — atomicité NON garantie"
        )
    if ledger._db is not db:
        log.warning(
            "[unit_of_work] ledger._db is not db — atomicité NON garantie"
        )

    with db.transaction() as cur:
        # Étape 1 — broker
        fill = broker.submit_in_tx(cur, order, price, ts, dry_run=dry_run, fx_rate=fx_rate)

        # Étape 2 — plan (close prioritaire sur upsert)
        if symbol_to_close is not None:
            plan_store.close_symbol_in_tx(cur, symbol_to_close)
        elif plan_to_upsert is not None:
            plan_store.upsert_in_tx(cur, plan_to_upsert)

        # Étape 3 — ledger
        ledger.complete_in_tx(cur, task_id=task_id, token=token, now_ms=now_ms)

    log.debug(
        "[unit_of_work] committed task_id=%s dry_run=%s fill=%s",
        task_id,
        dry_run,
        fill,
    )

    # Shadows best-effort hors transaction (après COMMIT réussi)
    try:
        broker.regenerate_shadow()
    except Exception as exc:
        log.warning("[unit_of_work] broker shadow échec: %s", exc)
    try:
        plan_store.regenerate_shadow()
    except Exception as exc:
        log.warning("[unit_of_work] plan_store shadow échec: %s", exc)

    return fill
