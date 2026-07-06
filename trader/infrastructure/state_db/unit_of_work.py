"""execute_order_unit — UnitOfWork atomique broker + plan + ledger.

Ouvre UNE transaction SQLite : fence → submit_in_tx → plan (close et/ou upsert)
→ complete_in_tx. Si N'IMPORTE quelle étape lève → ROLLBACK total (pas de fill
sans plan, pas de cash muté sans task done). Après COMMIT réussi : shadows
best-effort hors transaction (broker.json + trade_plans.json).

Précondition (élevée en erreur dure) : broker, plan_store et ledger doivent
partager le MÊME StateDb (db). Sinon RuntimeError levée AVANT d'ouvrir la
transaction — l'atomicité ne peut pas être garantie.

Logging : [unit_of_work] (getLogger(__name__), %-style).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.infrastructure.state_db.connection import StateDb
    from trader.infrastructure.state_db.broker_store import SqliteBroker
    from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore
    from trader.infrastructure.queue.ledger import TaskLedger
    from trader.execution.contracts import Fill, Order
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

    Ordre des opérations dans la transaction :
      0. Fence early — SELECT status/claim_token ; lève si task non-running ou
         token mismatch (ROLLBACK total, rien écrit).
      1. submit_in_tx  — écrit positions/cash/fill (no-op si dry_run=True).
      2. plan          — close_symbol_in_tx ET/OU upsert_in_tx (si fourni,
         uniquement si dry_run=False). Les deux peuvent coexister (FLIP :
         ferme l'ancien plan PUIS ouvre le nouveau dans la même transaction).
      3. complete_in_tx — passe la tâche à 'done' ; vérifie retour=True.

    Si N'IMPORTE quelle étape lève → ROLLBACK complet de la transaction.
    Après COMMIT réussi : shadows broker.json et trade_plans.json régénérés
    best-effort hors transaction.

    Préconditions (erreur dure avant ouverture de la transaction) :
        broker._db, plan_store._db et ledger._db doivent tous être la même
        instance que ``db``. Si ce n'est pas le cas, RuntimeError levée avant
        tout SQL — l'atomicité ne peut pas être garantie sur des connexions
        distinctes.

    Args:
        db:              StateDb partagé — substrat unique de toutes les écritures.
        broker:          SqliteBroker pour le submit de l'ordre.
        plan_store:      SqliteTradePlanStore pour l'upsert ou close du plan.
        ledger:          TaskLedger pour la complétion de la tâche.
        order:           Ordre à soumettre au broker.
        price:           Prix d'exécution.
        ts:              Timestamp du fill (ISO string).
        fx_rate:         Taux de change devise native → USD.
        dry_run:         Si True, submit et mutations plan sont no-op ; la tâche
                         est quand même complétée (évite le re-play).
        plan_to_upsert:  TradePlan à upsert après le fill (uniquement si
                         dry_run=False). Compatible avec symbol_to_close
                         (FLIP : les deux s'appliquent, close avant upsert).
        symbol_to_close: Symbole dont fermer tous les plans ouverts (uniquement
                         si dry_run=False). Compatible avec plan_to_upsert.
        task_id:         ID de la tâche ledger à compléter.
        token:           Token de fencing de la tâche (claim_token attendu).
        now_ms:          Timestamp courant en epoch ms (injecté pour déterminisme).

    Returns:
        Fill retourné par submit_in_tx (None si dry_run=True).

    Raises:
        RuntimeError: si db partagé non identique (précondition), si la tâche
                      est introuvable, si status != 'running' ou token mismatch
                      (fence early), ou si complete_in_tx retourne False (défense
                      en profondeur). Dans tous les cas → ROLLBACK total.
        Toute autre exception levée dans la transaction → ROLLBACK total + re-raise.
    """
    # FIX 2 — Précondition StateDb enforce : raise dur AVANT d'ouvrir la transaction.
    # Sans ça, les trois écritures seraient réparties sur des connexions distinctes
    # → atomicité silencieusement rompue.
    if broker._db is not db:
        raise RuntimeError(
            "[unit_of_work] broker._db is not db — atomicité rompue, abandon"
        )
    if plan_store._db is not db:
        raise RuntimeError(
            "[unit_of_work] plan_store._db is not db — atomicité rompue, abandon"
        )
    if ledger._db is not db:
        raise RuntimeError(
            "[unit_of_work] ledger._db is not db — atomicité rompue, abandon"
        )

    with db.transaction() as cur:
        # FIX 1 — Fence early : SELECT status/claim_token dans la MÊME transaction,
        # AVANT le submit. Si la task est déjà 'done' ou le token est stale (rejeu,
        # double-claim, recover_on_boot), lève ici → ROLLBACK total, rien n'est écrit.
        fence_row = cur.execute(
            "SELECT status, claim_token FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if fence_row is None:
            raise RuntimeError(
                f"[unit_of_work] task {task_id} introuvable — abandon"
            )
        if fence_row["status"] != "running":
            raise RuntimeError(
                f"[unit_of_work] task {task_id} status={fence_row['status']!r} != 'running'"
                " — double-fill évité (ROLLBACK)"
            )
        if fence_row["claim_token"] != token:
            raise RuntimeError(
                f"[unit_of_work] task {task_id} token mismatch — token stale,"
                " double-fill évité (ROLLBACK)"
            )

        # Étape 1 — broker (no-op si dry_run=True)
        fill = broker.submit_in_tx(cur, order, price, ts, dry_run=dry_run, fx_rate=fx_rate)

        # FIX 3 — dry_run : pas de mutation plan.
        # submit_in_tx est déjà no-op en dry_run ; on protège aussi le plan.
        if not dry_run:
            # FIX 4 — FLIP = close ET upsert dans la même UoW (non exclusifs).
            # Étape 2a — close (si fourni)
            if symbol_to_close is not None:
                plan_store.close_symbol_in_tx(cur, symbol_to_close)
            # Étape 2b — upsert (si fourni) — s'applique APRÈS le close pour FLIP
            if plan_to_upsert is not None:
                plan_store.upsert_in_tx(cur, plan_to_upsert)

        # Étape 3 — ledger (toujours, même en dry_run).
        # FIX 1 — Fill atomique : sérialise le fill ET passe-le à complete_in_tx
        # DANS LA MÊME TRANSACTION. Ainsi task 'done' ET result=<fill JSON> sont
        # atomiques — un crash post-commit garantit les deux présents ensemble.
        _fill_result: str | None = json.dumps(asdict(fill)) if fill is not None else None
        completed = ledger.complete_in_tx(
            cur, task_id=task_id, token=token, now_ms=now_ms, result=_fill_result
        )
        if not completed:
            # Défense en profondeur : ne devrait pas arriver grâce au fence early.
            raise RuntimeError(
                f"[unit_of_work] complete_in_tx task {task_id} retourné False"
                " malgré le fence — abandon (ROLLBACK)"
            )

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
