"""Handler execute_order — exécution atomique d'un ordre via le TaskLedger.

``make_execute_order_handler`` retourne un ``Callable[[dict], str | None]``
compatible avec ``Worker.run_once`` (``DecidePool`` ou pool équivalent).

Atomicité (outbox transactionnel)
----------------------------------
``execute_order_unit`` écrit broker + plan + ``ledger.complete_in_tx`` dans
UNE transaction SQLite.  Si N'IMPORTE quelle étape lève → ROLLBACK total :
pas de fill sans plan, pas de cash muté sans task done.

Double-complete
---------------
``execute_order_unit`` appelle ``complete_in_tx`` DANS sa transaction → la
task passe à ``status='done'``, ``claim_token=NULL``.  Quand le Worker appelle
ensuite ``complete(result=fill_json)``, le WHERE clause ``claim_token=?`` ne
matche plus → no-op (fencing).  Le résultat (Fill JSON) est alors patché
best-effort par ``worker.run_once`` via ``_patch_result`` (voir ``worker.py``).

AX §5 Fast Fail Early : payload invalide → KeyError/ValueError remonte au
Worker → task passée en dead (non retryable).
AX §4 Machine-Readable Errors : exceptions non catchées ici — frontière = Worker.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from trader.state_db.connection import StateDb
    from trader.state_db.broker_store import SqliteBroker
    from trader.state_db.trade_plan_store import SqliteTradePlanStore
    from trader.queue.ledger import TaskLedger

log = logging.getLogger(__name__)


def make_execute_order_handler(
    *,
    db: "StateDb",
    broker: "SqliteBroker",
    plan_store: "SqliteTradePlanStore",
    ledger: "TaskLedger",
    now_fn: Callable[[], float] = time.time,
) -> Callable[[dict], "str | None"]:
    """Fabrique un handler ``execute_order`` pour ``Worker.run_once``.

    Précondition : ``broker._db``, ``plan_store._db`` et ``ledger._db`` doivent
    être la même instance que ``db`` (partagent casys.db).  Sans ça, l'atomicité
    de ``execute_order_unit`` n'est pas garantie.

    Args:
        db:         StateDb partagé (casys.db) — substrat atomique.
        broker:     SqliteBroker sur ``db``.
        plan_store: SqliteTradePlanStore sur ``db``.
        ledger:     TaskLedger sur ``db``.
        now_fn:     Callable[[], float] retournant epoch secondes.  Injecté pour
                    le déterminisme des tests (défaut : ``time.time``).

    Returns:
        Handler ``Callable[[dict], str | None]`` :
        - Retourne le ``Fill`` sérialisé en JSON (str) si le fill a eu lieu.
        - Retourne ``None`` si ``dry_run=True`` ou si l'ordre n'a pas produit de fill.
        - Lève l'exception d'``execute_order_unit`` sur erreur (Worker → dead).

    Note double-complete : le handler NE fait PAS lui-même ``ledger.complete`` —
    c'est ``execute_order_unit`` qui le fait dans sa transaction.  Le Worker
    appellera ensuite ``complete()`` (no-op, fencing) puis patchera le résultat.
    """

    def handler(task: dict) -> "str | None":
        payload = json.loads(task["payload"])

        # --- Decode order ---
        from trader.tools.execution import Order  # import local — pas de circular dep
        order_raw = payload["order"]
        order = Order(
            symbol=str(order_raw["symbol"]),
            side=order_raw["side"],
            quantity=float(order_raw["quantity"]),
            rationale=str(order_raw.get("rationale", "")),
        )

        # --- Decode plan (optional) ---
        plan_to_upsert = None
        if payload.get("plan_to_upsert") is not None:
            from trader.planning.trade_plan import trade_plan_from_dict  # noqa: PLC0415
            plan_to_upsert = trade_plan_from_dict(payload["plan_to_upsert"])

        symbol_to_close: str | None = payload.get("symbol_to_close") or None

        # --- Execute (broker + plan + task done — atomic) ---
        from trader.state_db.unit_of_work import execute_order_unit  # noqa: PLC0415
        fill = execute_order_unit(
            db=db,
            broker=broker,
            plan_store=plan_store,
            ledger=ledger,
            order=order,
            price=float(payload["price"]),
            ts=str(payload["ts"]),
            fx_rate=float(payload.get("fx_rate", 1.0)),
            dry_run=bool(payload.get("dry_run", False)),
            plan_to_upsert=plan_to_upsert,
            symbol_to_close=symbol_to_close,
            task_id=task["id"],
            token=task["claim_token"],
            now_ms=int(now_fn() * 1000),
        )

        if fill is None:
            return None
        return json.dumps(asdict(fill))

    return handler
