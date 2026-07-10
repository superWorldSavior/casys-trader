"""Durable-queue adapter for atomic SQLite order execution."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Callable

from trader.domain.trade_plan import TradePlan
from trader.execution.contracts import Order
from trader.infrastructure.state_db.unit_of_work import execute_order_unit

if TYPE_CHECKING:
    from trader.infrastructure.queue.ledger import TaskLedger
    from trader.infrastructure.state_db.broker_store import SqliteBroker
    from trader.infrastructure.state_db.connection import StateDb
    from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore


def make_execute_order_handler(
    *,
    db: "StateDb",
    broker: "SqliteBroker",
    plan_store: "SqliteTradePlanStore",
    ledger: "TaskLedger",
    now_fn: Callable[[], float] = time.time,
) -> Callable[..., str | None]:
    """Build a worker handler backed by the shared atomic SQLite unit of work."""

    def handler(task: dict, *, heartbeat=None) -> str | None:
        payload = json.loads(task["payload"])
        order_raw = payload["order"]
        order = Order(
            symbol=str(order_raw["symbol"]),
            side=order_raw["side"],
            quantity=float(order_raw["quantity"]),
            rationale=str(order_raw.get("rationale", "")),
        )
        plan_to_upsert = (
            TradePlan.model_validate(payload["plan_to_upsert"])
            if payload.get("plan_to_upsert") is not None
            else None
        )
        symbol_to_close: str | None = payload.get("symbol_to_close") or None

        execute_order_unit(
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
        return None

    return handler
