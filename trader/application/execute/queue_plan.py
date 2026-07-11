"""Build atomic trade-plan payloads for queued order execution."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Mapping, Protocol

import trader.application.execute.order_admission as order_admission
from trader.planning.trade_plan import TradePlan, create_trade_plan_from_order


class OpenPlanReader(Protocol):
    def open_plans(self) -> list[TradePlan]:
        ...


@dataclass(frozen=True)
class ExecuteQueuePlanPayload:
    plan_to_upsert: dict | None
    symbol_to_close: str | None
    symbol_to_sync_quantity: str | None = None


def build_execute_queue_plan_payload(
    *,
    plan_reader: OpenPlanReader,
    symbol: str,
    action: str,
    intent: str | None,
    quantity: float,
    price: float,
    opened_at: str,
    runtime_exit_plan: dict | None,
    reference_volatility: float | None,
    rationale: str,
    entry_context: Mapping[str, object] | None,
    position_quantity: float,
    position_avg_price: float,
    llm_provider: str | None,
    llm_model: str | None,
    llm_fallback_reason: str | None,
    llm_confidence: float | None,
    entry_decision_id: str | None = None,
) -> ExecuteQueuePlanPayload:
    """Prepare the queue UoW payload without enqueuing or mutating stores."""
    symbol_to_close = symbol if intent in {"CLOSE", "FLIP", "SCALE_IN"} else None
    symbol_to_sync_quantity = symbol if intent == "REDUCE" else None
    if not runtime_exit_plan:
        return ExecuteQueuePlanPayload(
            plan_to_upsert=None,
            symbol_to_close=symbol_to_close,
            symbol_to_sync_quantity=symbol_to_sync_quantity,
        )

    if intent in {"OPEN_LONG", "OPEN_SHORT"}:
        plan = _build_plan(
            symbol=symbol,
            action=action,
            quantity=quantity,
            entry_price=price,
            opened_at=opened_at,
            runtime_exit_plan=runtime_exit_plan,
            reference_volatility=reference_volatility,
            rationale=rationale,
            entry_context=entry_context,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_fallback_reason=llm_fallback_reason,
            llm_confidence=llm_confidence,
            entry_decision_id=entry_decision_id,
        )
        return ExecuteQueuePlanPayload(
            plan_to_upsert=plan.model_dump(),
            symbol_to_close=symbol_to_close,
            symbol_to_sync_quantity=symbol_to_sync_quantity,
        )

    if intent == "SCALE_IN":
        total_quantity, avg_price = order_admission.projected_scale_in_risk_basis(
            action=action,
            scale_in_quantity=quantity,
            scale_in_price=price,
            position_quantity=position_quantity,
            position_avg_price=position_avg_price,
        )
        if total_quantity <= 0:
            return ExecuteQueuePlanPayload(
                plan_to_upsert=None,
                symbol_to_close=symbol_to_close,
                symbol_to_sync_quantity=symbol_to_sync_quantity,
            )
        plan = _build_plan(
            symbol=symbol,
            action=action,
            quantity=total_quantity,
            entry_price=avg_price,
            opened_at=opened_at,
            runtime_exit_plan=runtime_exit_plan,
            reference_volatility=reference_volatility,
            rationale=rationale,
            entry_context=entry_context,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_fallback_reason=llm_fallback_reason,
            llm_confidence=llm_confidence,
            entry_decision_id=entry_decision_id,
        )
        previous_plan = _open_plan_for_symbol(plan_reader, symbol)
        if previous_plan is not None and previous_plan.last_llm_review is not None:
            plan = plan.model_copy(update={"last_llm_review": copy.deepcopy(previous_plan.last_llm_review)})
        return ExecuteQueuePlanPayload(
            plan_to_upsert=plan.model_dump(),
            symbol_to_close=symbol_to_close,
            symbol_to_sync_quantity=symbol_to_sync_quantity,
        )

    if intent == "FLIP":
        open_quantity = order_admission.flip_open_quantity(
            action=action,
            quantity=quantity,
            position_quantity=position_quantity,
        )
        if open_quantity <= 0:
            return ExecuteQueuePlanPayload(
                plan_to_upsert=None,
                symbol_to_close=symbol_to_close,
                symbol_to_sync_quantity=symbol_to_sync_quantity,
            )
        plan = _build_plan(
            symbol=symbol,
            action=action,
            quantity=open_quantity,
            entry_price=price,
            opened_at=opened_at,
            runtime_exit_plan=runtime_exit_plan,
            reference_volatility=reference_volatility,
            rationale=rationale,
            entry_context=entry_context,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_fallback_reason=llm_fallback_reason,
            llm_confidence=llm_confidence,
            entry_decision_id=entry_decision_id,
        )
        return ExecuteQueuePlanPayload(
            plan_to_upsert=plan.model_dump(),
            symbol_to_close=symbol_to_close,
            symbol_to_sync_quantity=symbol_to_sync_quantity,
        )

    return ExecuteQueuePlanPayload(
        plan_to_upsert=None,
        symbol_to_close=symbol_to_close,
        symbol_to_sync_quantity=symbol_to_sync_quantity,
    )


def _build_plan(
    *,
    symbol: str,
    action: str,
    quantity: float,
    entry_price: float,
    opened_at: str,
    runtime_exit_plan: dict,
    reference_volatility: float | None,
    rationale: str,
    entry_context: Mapping[str, object] | None,
    llm_provider: str | None,
    llm_model: str | None,
    llm_fallback_reason: str | None,
    llm_confidence: float | None,
    entry_decision_id: str | None,
) -> TradePlan:
    plan = create_trade_plan_from_order(
        symbol=symbol,
        order_side=action,
        quantity=quantity,
        entry_price=entry_price,
        opened_at=opened_at,
        raw_exit_plan=runtime_exit_plan,
        reference_volatility=reference_volatility,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_fallback_reason=llm_fallback_reason,
        llm_confidence=llm_confidence,
    )
    return plan.model_copy(
        update={
            "entry_thesis": rationale,
            "entry_context": copy.deepcopy(dict(entry_context or {})),
            "entry_decision_id": entry_decision_id,
        }
    )


def _open_plan_for_symbol(plan_reader: OpenPlanReader, symbol: str) -> TradePlan | None:
    return next((plan for plan in plan_reader.open_plans() if plan.symbol == symbol), None)
