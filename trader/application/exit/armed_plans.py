"""Application service for resolving armed plan trigger execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from trader.application.execute.trade_plan_evaluation import (
    candidate_from_decision,
)
from trader.domain.decisions import Decision
from trader.domain.planning.armed_order import armed_order_price_coherent
from trader.domain.planning.exit_plan_spec import InvalidExitPlanError
from trader.domain.planning.trade_plan import resolve_exit_plan


class PositionLike(Protocol):
    quantity: float


class ReferenceVolatilityProvider(Protocol):
    def __call__(
        self,
        symbol: str,
        *,
        entry_price: float,
        cockpit: dict,
        tradable_bars_by_symbol: dict[str, list],
    ) -> float | None: ...


@dataclass(frozen=True)
class ArmedPlanEvent:
    name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ArmedPlanProgressLog:
    message: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class ArmedPlanResolution:
    decisions: dict[str, Decision]
    plan_ids: dict[str, str]
    plan_orders: dict[str, dict]
    stale_plans: dict[str, dict]
    reference_volatilities: dict[str, float | None]
    events: list[ArmedPlanEvent]
    progress_logs: list[ArmedPlanProgressLog]
    consume_watch_ids: list[str] = field(default_factory=list)


_CONFLICT_LOG = "[armed_plan] %s conflit (%d plans d\u00e9clench\u00e9s) \u2014 r\u00e9veil planificateur"
_CANCEL_LOG = "[armed_plan] %s %s plan=%s \u2014 r\u00e9veil planificateur"
_TRIGGERED_LOG = "[armed_plan] %s d\u00e9clench\u00e9 plan=%s \u2014 ex\u00e9cution sans LLM"
_TERMINAL_CANCEL_REASONS = {
    "armed_plan_cancelled:position_exists",
    "armed_plan_cancelled:stop_incoherent",
}


def _is_terminal_cancel(reason: str) -> bool:
    return reason in _TERMINAL_CANCEL_REASONS or reason.startswith(
        "armed_plan_cancelled:exit_unresolved"
    ) or reason.startswith("armed_plan_cancelled:trade_evaluation")


def resolve_armed_plan_triggers(
    *,
    indicator_triggers: list[dict],
    symbols_to_decide: list[str],
    prices: Mapping[str, float],
    stale_market_data: Mapping[str, object],
    positions: Mapping[str, PositionLike],
    cockpit: dict,
    tradable_bars_by_symbol: dict[str, list],
    reference_volatility_for_symbol: ReferenceVolatilityProvider,
    trade_plan_evaluator_provider: Callable[[str], object | None] | None = None,
) -> ArmedPlanResolution:
    """Resolve EXECUTE_ORDER triggers into deterministic decisions or planner wakeups."""
    decisions: dict[str, Decision] = {}
    plan_ids_by_symbol: dict[str, str] = {}
    plan_orders_by_symbol: dict[str, dict] = {}
    stale_plans_by_symbol: dict[str, dict] = {}
    reference_volatilities_by_symbol: dict[str, float | None] = {}
    events: list[ArmedPlanEvent] = []
    progress_logs: list[ArmedPlanProgressLog] = []
    consume_watch_ids: list[str] = []

    armed_by_symbol: dict[str, list[dict]] = {}
    for trigger in indicator_triggers:
        if str(trigger.get("on_trigger")) == "EXECUTE_ORDER" and isinstance(trigger.get("order"), dict):
            armed_by_symbol.setdefault(str(trigger.get("symbol")), []).append(trigger)

    armed_conflicts = {symbol for symbol, items in armed_by_symbol.items() if len(items) > 1}
    for symbol in armed_conflicts:
        plan_ids = [str(trigger.get("watch_id") or "") for trigger in armed_by_symbol[symbol]]
        progress_logs.append(ArmedPlanProgressLog(_CONFLICT_LOG, (symbol, len(plan_ids))))
        events.append(ArmedPlanEvent("armed_plan_conflict", {"symbol": symbol, "plan_ids": plan_ids}))
        consume_watch_ids.extend(plan_id for plan_id in plan_ids if plan_id)
        for trigger in armed_by_symbol[symbol]:
            trigger["armed_conflict"] = True

    eligible_symbols = set(symbols_to_decide)
    for symbol, symbol_triggers in armed_by_symbol.items():
        if symbol in armed_conflicts or symbol not in eligible_symbols:
            continue

        trigger = symbol_triggers[0]
        order = trigger["order"]
        plan_id = str(trigger.get("watch_id") or "")
        position = positions.get(symbol)
        cancel_reason = None
        if symbol in stale_market_data or symbol not in prices:
            cancel_reason = "armed_plan_cancelled:stale"
        elif position is not None and position.quantity:
            cancel_reason = "armed_plan_cancelled:position_exists"
        else:
            intent_side = {"OPEN_LONG": "LONG", "OPEN_SHORT": "SHORT"}.get(str(order.get("intent")))
            if intent_side is None:
                cancel_reason = "armed_plan_cancelled:exit_unresolved:side_unsupported"
            else:
                ref_vol = reference_volatility_for_symbol(
                    symbol,
                    entry_price=prices[symbol],
                    cockpit=cockpit,
                    tradable_bars_by_symbol=tradable_bars_by_symbol,
                )
                try:
                    resolved_exit_plan, trace = resolve_exit_plan(
                        order.get("exit_plan"),
                        entry_price=prices[symbol],
                        side=intent_side,  # type: ignore[arg-type]
                        reference_volatility=ref_vol,
                        bars=tradable_bars_by_symbol.get(symbol),
                    )
                except InvalidExitPlanError as exc:
                    cancel_reason = f"armed_plan_cancelled:exit_unresolved:{exc}"
                else:
                    order = {**order, "exit_plan": resolved_exit_plan}
                    trigger["order"] = order
                    reference_volatilities_by_symbol[symbol] = ref_vol
                    events.append(
                        ArmedPlanEvent(
                            "armed_plan_resolved",
                            {
                                "symbol": symbol,
                                "plan_id": plan_id,
                                "trace": trace,
                                "exit_plan": order.get("exit_plan"),
                            },
                        )
                    )
                    if not armed_order_price_coherent(order, price=prices[symbol]):
                        cancel_reason = "armed_plan_cancelled:stop_incoherent"
                    elif not str(order.get("trade_evaluation_id") or "").strip():
                        cancel_reason = (
                            "armed_plan_cancelled:trade_evaluation_required"
                        )
                    else:
                        evaluator = (
                            None
                            if trade_plan_evaluator_provider is None
                            else trade_plan_evaluator_provider(symbol)
                        )
                        if evaluator is None:
                            cancel_reason = (
                                "armed_plan_cancelled:trade_evaluation_unavailable"
                            )
                        else:
                            candidate_decision = Decision(
                                symbol=symbol,
                                action=str(order["action"]),  # type: ignore[arg-type]
                                quantity=float(order["qty"]),
                                confidence=float(order["confidence"]),
                                rationale=str(order.get("rationale") or "armed_entry"),
                                intent=str(order["intent"]),  # type: ignore[arg-type]
                                exit_plan=order.get("exit_plan"),
                                thesis=order.get("thesis"),
                            )
                            candidate = candidate_from_decision(candidate_decision)
                            evaluation = (
                                None
                                if candidate is None
                                else evaluator.evaluate(candidate)  # type: ignore[attr-defined]
                            )
                            if evaluation is None or not evaluation.valid:
                                reason = (
                                    "invalid"
                                    if evaluation is None
                                    else (
                                        evaluation.reasons[0]
                                        if evaluation.reasons
                                        else "invalid"
                                    )
                                )
                                cancel_reason = (
                                    "armed_plan_cancelled:"
                                    f"trade_evaluation_invalid:{reason}"
                                )
                            else:
                                order = {
                                    **order,
                                    "trade_evaluation_id": evaluation.evaluation_id,
                                }
                                trigger["order"] = order
                                events.append(
                                    ArmedPlanEvent(
                                        "armed_plan_trade_evaluated",
                                        {
                                            "symbol": symbol,
                                            "plan_id": plan_id,
                                            "evaluation": evaluation.to_tool_payload(),
                                        },
                                    )
                                )

        if cancel_reason is not None:
            progress_logs.append(ArmedPlanProgressLog(_CANCEL_LOG, (symbol, cancel_reason, plan_id)))
            events.append(
                ArmedPlanEvent(
                    "armed_plan_cancelled",
                    {
                        "symbol": symbol,
                        "plan_id": plan_id,
                        "reason": cancel_reason,
                        "exit_plan": order.get("exit_plan"),
                    },
                )
            )
            trigger["armed_cancelled"] = cancel_reason
            if cancel_reason == "armed_plan_cancelled:stale":
                stale_plans_by_symbol[symbol] = {"id": plan_id, "order": dict(order)}
            elif _is_terminal_cancel(cancel_reason) and plan_id:
                consume_watch_ids.append(plan_id)
            continue

        decisions[symbol] = Decision(
            symbol=symbol,
            action=str(order["action"]),  # type: ignore[arg-type]
            quantity=float(order["qty"]),
            confidence=float(order["confidence"]),
            rationale=f"armed_plan:{plan_id} \u2014 {order.get('rationale') or ''}".strip(" \u2014"),
            intent=str(order["intent"]),  # type: ignore[arg-type]
            exit_plan=order.get("exit_plan"),
            decision_reason_code="ARMED_PLAN",
            thesis=order.get("thesis"),
            trade_evaluation_id=str(order["trade_evaluation_id"]),
        )
        plan_ids_by_symbol[symbol] = plan_id
        plan_orders_by_symbol[symbol] = dict(order)
        progress_logs.append(ArmedPlanProgressLog(_TRIGGERED_LOG, (symbol, plan_id)))

    return ArmedPlanResolution(
        decisions=decisions,
        plan_ids=plan_ids_by_symbol,
        plan_orders=plan_orders_by_symbol,
        stale_plans=stale_plans_by_symbol,
        reference_volatilities=reference_volatilities_by_symbol,
        events=events,
        progress_logs=progress_logs,
        consume_watch_ids=consume_watch_ids,
    )
