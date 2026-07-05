"""Post-fill trade-plan effects for executed agent decisions."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Protocol

from trader.application import planned_exits
from trader.execution.contracts import Position
from trader.planning.trade_plan import TradePlan, create_trade_plan, create_trade_plan_from_order

OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "REVERSE", "ADD"}


class PositionReader(Protocol):
    def positions(self) -> dict[str, Position]:
        ...


class TradePlanStoreLike(Protocol):
    def open_plans(self) -> list[TradePlan]:
        ...

    def upsert(self, plan: TradePlan) -> None:
        ...

    def close_symbol(self, symbol: str) -> None:
        ...

    def sync_symbol_quantity(self, symbol: str, quantity: float) -> None:
        ...


def apply_filled_plan_effects(
    *,
    entry: dict,
    broker: PositionReader,
    plan_store: TradePlanStoreLike,
    symbol: str,
    action: str,
    intent: str | None,
    quantity: float,
    price: float,
    opened_at: str,
    runtime_exit_plan: dict | None,
    reference_volatility: float | None,
    llm_provider: str | None,
    llm_model: str | None,
    llm_fallback_reason: str | None,
    llm_confidence: float | None,
    queue_execute_enabled: bool,
    entry_thesis: str,
    entry_context: dict,
) -> None:
    """Apply trade-plan mutations and decision-entry snapshots after a fill."""
    if intent in {"CLOSE", "REVERSE"} and not queue_execute_enabled:
        plan_store.close_symbol(symbol)

    if intent == "REDUCE":
        final_position = broker.positions().get(symbol)
        plan_store.sync_symbol_quantity(
            symbol,
            0.0 if final_position is None else abs(final_position.quantity),
        )

    if not runtime_exit_plan or intent not in OPENING_INTENTS:
        return

    entry_meta = {
        "entry_thesis": entry_thesis,
        "entry_context": copy.deepcopy(dict(entry_context)),
    }

    if intent == "REVERSE":
        _apply_reverse_plan_effects(
            entry=entry,
            broker=broker,
            plan_store=plan_store,
            symbol=symbol,
            price=price,
            opened_at=opened_at,
            runtime_exit_plan=runtime_exit_plan,
            reference_volatility=reference_volatility,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_fallback_reason=llm_fallback_reason,
            llm_confidence=llm_confidence,
            queue_execute_enabled=queue_execute_enabled,
            entry_meta=entry_meta,
        )
        return

    plan_quantity = quantity
    plan_entry_price = price
    add_previous_plan: TradePlan | None = None
    if intent == "ADD":
        final_position = broker.positions().get(symbol)
        plan_quantity = 0.0 if final_position is None else abs(final_position.quantity)
        if final_position is not None and final_position.avg_price > 0.0:
            plan_entry_price = final_position.avg_price
        add_previous_plan = _open_plan_for_symbol(plan_store, symbol)
        if not queue_execute_enabled:
            plan_store.close_symbol(symbol)

    plan = create_trade_plan_from_order(
        symbol=symbol,
        order_side=action,
        quantity=plan_quantity,
        entry_price=plan_entry_price,
        opened_at=opened_at,
        raw_exit_plan=runtime_exit_plan,
        reference_volatility=reference_volatility,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_fallback_reason=llm_fallback_reason,
        llm_confidence=llm_confidence,
    )
    plan = replace(plan, **entry_meta)
    if add_previous_plan is not None and add_previous_plan.last_llm_review is not None:
        plan = replace(plan, last_llm_review=copy.deepcopy(add_previous_plan.last_llm_review))
    if not queue_execute_enabled:
        plan_store.upsert(plan)
    entry["trade_plan_created"] = True
    entry["trade_plan"] = planned_exits.plan_snapshot(plan)


def _apply_reverse_plan_effects(
    *,
    entry: dict,
    broker: PositionReader,
    plan_store: TradePlanStoreLike,
    symbol: str,
    price: float,
    opened_at: str,
    runtime_exit_plan: dict,
    reference_volatility: float | None,
    llm_provider: str | None,
    llm_model: str | None,
    llm_fallback_reason: str | None,
    llm_confidence: float | None,
    queue_execute_enabled: bool,
    entry_meta: dict,
) -> None:
    if queue_execute_enabled:
        entry["trade_plan_created"] = True
        new_plans = [plan for plan in plan_store.open_plans() if plan.symbol == symbol]
        if new_plans:
            entry["trade_plan"] = planned_exits.plan_snapshot(new_plans[-1])
        return

    created_plan = _create_plan_for_final_position(
        broker=broker,
        symbol=symbol,
        price=price,
        opened_at=opened_at,
        raw_exit_plan=runtime_exit_plan,
        reference_volatility=reference_volatility,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_fallback_reason=llm_fallback_reason,
        llm_confidence=llm_confidence,
    )
    entry["trade_plan_created"] = created_plan is not None
    if created_plan is not None:
        created_plan = replace(created_plan, **entry_meta)
        plan_store.upsert(created_plan)
        entry["trade_plan"] = planned_exits.plan_snapshot(created_plan)


def _create_plan_for_final_position(
    *,
    broker: PositionReader,
    symbol: str,
    price: float,
    opened_at: str,
    raw_exit_plan: dict,
    reference_volatility: float | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_fallback_reason: str | None = None,
    llm_confidence: float | None = None,
) -> TradePlan | None:
    position = broker.positions().get(symbol)
    if position is None or position.quantity == 0:
        return None
    return create_trade_plan(
        symbol=symbol,
        side="LONG" if position.quantity > 0 else "SHORT",
        quantity=abs(position.quantity),
        entry_price=position.avg_price or price,
        opened_at=opened_at,
        raw_exit_plan=raw_exit_plan,
        reference_volatility=reference_volatility,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_fallback_reason=llm_fallback_reason,
        llm_confidence=llm_confidence,
    )


def _open_plan_for_symbol(plan_store: TradePlanStoreLike, symbol: str) -> TradePlan | None:
    return next((plan for plan in plan_store.open_plans() if plan.symbol == symbol), None)
