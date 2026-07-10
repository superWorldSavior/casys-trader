"""Application service for applying one per-symbol cycle decision."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from trader.agent import client as codex_client
import trader.application.cycle.schedule as cycle_schedule
import trader.application.execute.entry_context as entry_context
import trader.application.execute.queue_dispatch as execute_queue_dispatch
import trader.application.execute.fill_outcome as fill_outcome
import trader.application.execute.order_admission as order_admission
import trader.application.execute.queue_plan as queue_plan
import trader.application.execute.risk_admission as risk_admission
import trader.application.execute.risk_capacity as risk_capacity
import trader.application.exit.exit_update as exit_update_service
import trader.application.exit.fill_plan_effects as fill_plan_effects
import trader.application.record.decision_entries as decision_entries
import trader.application.record.decision_watches as decision_watches
import trader.application.record.plan_review as plan_review
from trader.application.portfolio import snapshot as portfolio
from trader.domain.execution.risk_gate import RiskGate
import trader.market.execution_eligibility as execution_eligibility_service
from trader.market import market_data as market
import trader.market.volatility as reference_volatility_service
from trader.planning.protocols import SchedulerLike
from trader.planning.trade_plan import InvalidExitPlanError, resolve_exit_plan, validate_exit_plan

_OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "FLIP", "SCALE_IN"}
_PURE_OPEN_INTENTS = {"OPEN_LONG", "OPEN_SHORT"}
_RISK_GUARDED_OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "SCALE_IN"}
_RELATIVE_ORDER_INTENTS = order_admission.RELATIVE_ORDER_INTENTS
_EXECUTE_POLL_BUDGET_S: float = 10.0


def _noop_event(_event: str, **_payload: object) -> None:
    return None


def _noop_model_performance(**_payload: object) -> None:
    return None


@dataclass
class DecisionExecutionState:
    snap: object
    gross: float
    opening_batch_timed_out: bool = False
    deferred_opening_symbols: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class DecisionExecutionContext:
    now: datetime
    min_wake_minutes: float | None
    max_wake_minutes: float | None
    macro_next: dict | None
    broker: object
    plan_store: object
    gate: RiskGate
    sched: SchedulerLike | None
    prices: dict[str, float]
    execution_eligibility: dict[str, dict]
    tradable_bars_by_symbol: dict[str, list]
    data_age_by_symbol: dict[str, float]
    runtime_data_source_by_sym: dict[str, object]
    armed_plan_ids: dict[str, str]
    armed_plan_orders: dict[str, dict]
    armed_reference_volatilities: dict[str, float | None]
    held_symbols: set[str]
    cockpit: dict
    runtime_interval: str
    starting_equity: float
    require_hard_stop: bool
    dry_run: bool
    queue_execute_enabled: bool
    execute_ledger: object
    record_decision: Callable[[dict], None]
    rate_for_symbol: Callable[[str], float]
    append_event: Callable[..., None] = _noop_event
    append_model_performance: Callable[..., None] = _noop_model_performance
    logger: logging.Logger = logging.getLogger("casys-trader")


def _capture_exit_plan_trace(entry: dict, trace: dict) -> None:
    if not trace:
        return
    entry["exit_plan_trace"] = copy.deepcopy(trace)
    hard_stop = trace.get("hard_stop") if isinstance(trace, dict) else None
    warnings = hard_stop.get("warnings") if isinstance(hard_stop, dict) else None
    if isinstance(warnings, list) and warnings:
        entry["exit_plan_warnings"] = copy.deepcopy(warnings)


def execute_one_cycle_decision(
    *,
    sym: str,
    index: int,
    total: int,
    decision: codex_client.Decision,
    state: DecisionExecutionState,
    ctx: DecisionExecutionContext,
) -> DecisionExecutionState:
    _log_cycle_progress = ctx.logger.info
    _res_log = ctx.logger.debug if decision.action == "HOLD" else ctx.logger.info
    _res_log(
        "[decision %d/%d] %s result action=%s qty=%s intent=%s wake=%s confidence=%.2f provider=%s model=%s fallback=%s",
        index,
        total,
        sym,
        decision.action,
        decision.quantity,
        decision.intent,
        decision.next_wake_in_minutes,
        decision.confidence,
        decision.llm_provider,
        decision.llm_model,
        decision.llm_fallback_reason,
    )

    next_wake_in_minutes = None
    if decision.next_wake_in_minutes is not None:
        next_wake_in_minutes = cycle_schedule.bounded_wake_minutes(
            decision.next_wake_in_minutes,
            minimum=ctx.min_wake_minutes,
            maximum=ctx.max_wake_minutes,
        )

    if decision.resolve_from_position or decision.intent in _RELATIVE_ORDER_INTENTS:
        raw_pos = ctx.broker.positions().get(sym)
        pos_qty = raw_pos.quantity if raw_pos is not None else 0.0
        decision = order_admission.resolve_position_aware_decision(decision, pos_qty)

    next_wake_event_iso: str | None = None
    if decision.next_wake_event is not None:
        next_wake_event_iso = cycle_schedule.resolve_wake_event(
            decision.next_wake_event,
            sym,
            ctx.now,
            ctx.macro_next,
            market.next_regular_session_open,
        )

    effective_quantity = abs(decision.quantity)

    entry = decision_entries.build_decision_entry(
        symbol=sym,
        decision=decision,
        effective_quantity=effective_quantity,
        next_wake_in_minutes=next_wake_in_minutes,
        next_wake_event_iso=next_wake_event_iso,
        armed_plan_id=ctx.armed_plan_ids.get(sym),
        armed_plan_order=ctx.armed_plan_orders.get(sym),
        runtime_data_source=ctx.runtime_data_source_by_sym.get(sym),
    )
    if sym in ctx.prices:
        entry["price"] = ctx.prices[sym]
    decision_source = str(entry["decision_source"])
    if decision_source == "llm" and sym in ctx.held_symbols and decision_entries.counts_as_llm_review(decision):
        plan_review.persist_last_llm_review(
            plan_store=ctx.plan_store,
            symbol=sym,
            now=ctx.now,
            decision=decision,
        )

    reference_volatility: float | None = None
    runtime_exit_plan = decision.exit_plan
    entry["exit_plan"] = copy.deepcopy(decision.exit_plan) if decision.exit_plan else None
    watch_preparation = decision_watches.prepare_decision_indicator_watch(
        decision.indicator_watch,
        symbol=sym,
        now=ctx.now,
        scheduling_enabled=ctx.sched is not None,
    )
    entry.update(watch_preparation.entry_updates)
    if watch_preparation.rejections:
        ctx.logger.warning(
            "indicator_watch rejets %s: %d conditions %s",
            sym,
            len(watch_preparation.rejections),
            [r["reason"] for r in watch_preparation.rejections],
        )
    pending_indicator_watch = watch_preparation.pending_watch

    def apply_decision_schedule() -> None:
        cycle_schedule.apply_decision_schedule(
            sched=ctx.sched,
            sym=sym,
            now=ctx.now,
            next_wake_in_minutes=next_wake_in_minutes,
            next_wake_iso=next_wake_event_iso,
            cancel_watch_ids=decision.cancel_watch_ids,
            pending_indicator_watch=pending_indicator_watch,
            entry=entry,
            append_event=ctx.append_event,
            logger=ctx.logger,
        )

    def apply_default_schedule_after_blocked() -> None:
        if ctx.sched is not None:
            ctx.sched.clear_symbol_next_wake(sym)

    if (
        decision.action in {"BUY", "SELL"}
        and effective_quantity == 0
        and decision.risk_pct_target is None
    ):
        _log_cycle_progress("[decision %d/%d] %s blocked zero_quantity_order", index, total, sym)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": "zero_quantity_order"})
        return state

    if decision.action == "HOLD" or (effective_quantity == 0 and decision.risk_pct_target is None):
        apply_decision_schedule()
        if decision.exit_update:
            exit_update_service.apply_exit_update_to_open_plan(
                plan_store=ctx.plan_store,
                symbol=sym,
                exit_update=decision.exit_update,
                bars=ctx.tradable_bars_by_symbol.get(sym),
                entry=entry,
                current_price=ctx.prices.get(sym),
            )
        hold_reason = decision_entries.hold_reason_for_decision(
            decision_source=decision_source,
            rationale=decision.rationale,
        )
        ctx.record_decision({**entry, "executed": False, "reason": hold_reason})
        return state

    invalid_intent = order_admission.invalid_intent_reason(
        action=decision.action,
        quantity=decision.quantity,
        intent=decision.intent,
    )
    if invalid_intent is not None:
        _log_cycle_progress("[decision %d/%d] %s blocked %s", index, total, sym, invalid_intent)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": invalid_intent})
        return state

    execution_blocked = execution_eligibility_service.execution_blocked_reason(
        ctx.execution_eligibility,
        sym,
        fail_closed=decision.intent in _OPENING_INTENTS,
    )
    if execution_blocked is not None:
        _log_cycle_progress(
            "[execution] %s ordre bloqué (%s) — watch/wake conservés", sym, execution_blocked
        )
        apply_decision_schedule()
        ctx.record_decision({**entry, "executed": False, "reason": execution_blocked})
        return state

    if runtime_exit_plan and decision.intent in _OPENING_INTENTS:
        if sym in ctx.armed_reference_volatilities:
            reference_volatility = ctx.armed_reference_volatilities[sym]
        else:
            reference_volatility = reference_volatility_service.reference_volatility_for_symbol(
                sym,
                entry_price=ctx.prices[sym],
                cockpit=ctx.cockpit,
                tradable_bars_by_symbol=ctx.tradable_bars_by_symbol,
            )
        try:
            if decision.intent in _PURE_OPEN_INTENTS and sym not in ctx.armed_plan_ids:
                intent_side = "LONG" if decision.intent == "OPEN_LONG" else "SHORT"
                runtime_exit_plan, exit_trace = resolve_exit_plan(
                    runtime_exit_plan,
                    entry_price=ctx.prices[sym],
                    side=intent_side,
                    reference_volatility=reference_volatility,
                    bars=ctx.tradable_bars_by_symbol.get(sym),
                )
                _capture_exit_plan_trace(entry, exit_trace)
            elif decision.intent == "SCALE_IN":
                intent_side = "LONG" if decision.action == "BUY" else "SHORT"
                runtime_exit_plan, exit_trace = resolve_exit_plan(
                    runtime_exit_plan,
                    entry_price=ctx.prices[sym],
                    side=intent_side,
                    reference_volatility=reference_volatility,
                    bars=ctx.tradable_bars_by_symbol.get(sym),
                )
                _capture_exit_plan_trace(entry, exit_trace)
            else:
                validate_exit_plan(
                    runtime_exit_plan,
                    reference_volatility=reference_volatility,
                )
        except InvalidExitPlanError as exc:
            _log_cycle_progress(
                "[decision %d/%d] %s blocked invalid_exit_plan:%s",
                index,
                total,
                sym,
                exc,
            )
            apply_default_schedule_after_blocked()
            ctx.record_decision({**entry, "executed": False, "reason": f"invalid_exit_plan:{exc}"})
            return state
        hard_stop_price = order_admission.hard_stop_price(runtime_exit_plan)
        if (
            decision.intent in _RISK_GUARDED_OPENING_INTENTS
            and hard_stop_price is not None
            and order_admission.hard_stop_wrong_side(
                decision.intent,
                ctx.prices[sym],
                hard_stop_price,
                action=decision.action,
            )
        ):
            _log_cycle_progress(
                "[decision %d/%d] %s blocked invalid_exit_plan:hard_stop_wrong_side",
                index,
                total,
                sym,
            )
            apply_default_schedule_after_blocked()
            ctx.record_decision({**entry, "executed": False, "reason": "invalid_exit_plan:hard_stop_wrong_side"})
            return state

    pos = ctx.broker.positions().get(sym)
    clamped_quantity, exit_block_reason = order_admission.clamp_exit_quantity(
        intent=decision.intent,
        action=decision.action,
        quantity=effective_quantity,
        position_quantity=0.0 if pos is None else pos.quantity,
    )
    if exit_block_reason is not None:
        _log_cycle_progress("[decision %d/%d] %s blocked %s", index, total, sym, exit_block_reason)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": exit_block_reason})
        return state
    if clamped_quantity != effective_quantity:
        entry.setdefault("requested_qty", effective_quantity)
        effective_quantity = clamped_quantity
        entry["qty"] = effective_quantity
    if effective_quantity == 0 and decision.risk_pct_target is None:
        _log_cycle_progress("[decision %d/%d] %s hold zero_exit_quantity", index, total, sym)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": "zero_exit_quantity"})
        return state

    risk_outcome = risk_admission.assess_risk_admission(
        risk_admission.RiskAdmissionRequest(
            action=decision.action,
            intent=decision.intent,
            quantity=effective_quantity,
            price=ctx.prices[sym],
            equity=state.snap.equity,
            confidence=decision.confidence,
            runtime_exit_plan=runtime_exit_plan,
            risk_pct_target=decision.risk_pct_target,
            position_quantity=0.0 if pos is None else pos.quantity,
            position_avg_price=0.0 if pos is None else pos.avg_price,
            require_hard_stop=ctx.require_hard_stop,
            fx_rate=ctx.rate_for_symbol(sym),
        ),
        gate=ctx.gate,
    )
    effective_quantity = risk_outcome.quantity
    entry.update(risk_outcome.entry_updates)
    risk_warnings = entry.get("risk_warnings")
    if isinstance(risk_warnings, list):
        for warning in risk_warnings:
            if isinstance(warning, dict) and warning.get("code") == "risk_per_trade_exceeded":
                _log_cycle_progress(
                    "[risk] %s warning code=risk_per_trade_exceeded qty=%s max_qty=%s",
                    sym,
                    warning.get("risk_qty"),
                    warning.get("max_qty"),
                )
    if not risk_outcome.approved:
        reason = risk_outcome.reason or "risk:rejected"
        if reason == "risk:risk_sizing_needs_stop":
            _log_cycle_progress("[risk] %s rejected code=risk_sizing_needs_stop", sym)
        elif reason == "risk:missing_hard_stop":
            _log_cycle_progress("[risk] %s rejected code=missing_hard_stop", sym)
        elif reason == "risk:risk_per_trade_exceeded":
            _log_cycle_progress(
                "[risk] %s rejected code=risk_per_trade_exceeded qty=%s max_qty=%s",
                sym,
                entry.get("risk_total_position_qty", effective_quantity),
                entry.get("max_risk_qty"),
            )
        elif reason == "zero_risk_quantity":
            _log_cycle_progress("[decision %d/%d] %s hold zero_risk_quantity", index, total, sym)
        elif reason.startswith("risk:"):
            _log_cycle_progress(
                "[risk] %s rejected code=%s confidence=%s",
                sym,
                reason.removeprefix("risk:"),
                decision.confidence,
            )
        else:
            _log_cycle_progress("[decision %d/%d] %s blocked %s", index, total, sym, reason)
        apply_default_schedule_after_blocked()
        blocked_entry = {**entry, "executed": False, "reason": reason}
        if risk_outcome.context is not None:
            blocked_entry["context"] = risk_outcome.context
        ctx.record_decision(blocked_entry)
        return state

    final_risk_outcome = risk_admission.assess_final_risk_gate(
        risk_admission.FinalRiskGateRequest(
            symbol=sym,
            action=decision.action,
            quantity=effective_quantity,
            rationale=decision.rationale,
            intent=decision.intent,
            price=ctx.prices[sym],
            position_quantity=0.0 if pos is None else pos.quantity,
            gross_exposure=state.gross,
            equity=state.snap.equity,
            fx_rate=ctx.rate_for_symbol(sym),
        ),
        gate=ctx.gate,
    )
    order = final_risk_outcome.order
    if not final_risk_outcome.approved:
        _log_cycle_progress(
            "[risk] %s rejected code=%s qty=%s price=%s",
            sym,
            final_risk_outcome.code,
            order.quantity,
            round(ctx.prices[sym], 6),
        )
        apply_default_schedule_after_blocked()
        ctx.record_decision(
            {
                **entry,
                "executed": False,
                "reason": final_risk_outcome.reason,
                "context": final_risk_outcome.context,
            }
        )
        return state

    if ctx.queue_execute_enabled and ctx.execute_ledger is not None:
        _exec_entry_context = None
        if runtime_exit_plan is not None and decision.intent in {"OPEN_LONG", "OPEN_SHORT", "SCALE_IN", "FLIP"}:
            _exec_entry_age = ctx.data_age_by_symbol.get(sym)
            _exec_entry_context = entry_context.build_trade_entry_context(
                price=ctx.prices[sym],
                runtime_interval=ctx.runtime_interval,
                data_age_minutes=_exec_entry_age,
                session_open=bool(market.session_snapshot(sym, now=ctx.now).get("open")),
                daily_as_of=((ctx.execution_eligibility.get(sym) or {}).get("planning") or {}).get("daily_as_of"),
            )
        _exec_plan_payload = queue_plan.build_execute_queue_plan_payload(
            plan_reader=ctx.plan_store,
            symbol=sym,
            action=decision.action,
            intent=decision.intent,
            quantity=effective_quantity,
            price=ctx.prices[sym],
            opened_at=ctx.now.isoformat(),
            runtime_exit_plan=runtime_exit_plan,
            reference_volatility=reference_volatility,
            rationale=decision.rationale,
            entry_context=_exec_entry_context,
            position_quantity=0.0 if pos is None else pos.quantity,
            position_avg_price=0.0 if pos is None else pos.avg_price,
            llm_provider=decision.llm_provider,
            llm_model=decision.llm_model,
            llm_fallback_reason=decision.llm_fallback_reason,
            llm_confidence=decision.confidence,
        )
        _exec_outcome = execute_queue_dispatch.dispatch_execute_order_via_queue(
            ledger=ctx.execute_ledger,
            symbol=sym,
            side=decision.action,
            quantity=effective_quantity,
            rationale=decision.rationale,
            price=ctx.prices[sym],
            ts=ctx.now.isoformat(),
            fx_rate=ctx.rate_for_symbol(sym),
            dry_run=ctx.dry_run,
            plan_to_upsert=_exec_plan_payload.plan_to_upsert,
            symbol_to_close=_exec_plan_payload.symbol_to_close,
            cycle_id=ctx.now.isoformat(),
            intent=decision.intent,
            budget_s=_EXECUTE_POLL_BUDGET_S,
        )
        entry.update(
            {
                "queue_task_id": _exec_outcome.task_id,
                "queue_terminal": _exec_outcome.terminal,
                "queue_late_execution_risk": _exec_outcome.late_execution_risk,
                "queue_abandoned": _exec_outcome.abandoned,
            }
        )
        if _exec_outcome.reason is not None:
            if decision.intent in _OPENING_INTENTS:
                state.deferred_opening_symbols.add(sym)
                if _exec_outcome.terminal == "timeout":
                    state.opening_batch_timed_out = True
            apply_default_schedule_after_blocked()
            ctx.record_decision({**entry, "executed": False, "reason": _exec_outcome.reason})
            return state
        fill = _exec_outcome.fill
    else:
        fill = ctx.broker.submit(
            order,
            ctx.prices[sym],
            ctx.now.isoformat(),
            dry_run=ctx.dry_run,
            fx_rate=ctx.rate_for_symbol(sym),
        )
    if not ctx.dry_run:
        state.gross = risk_capacity.gross_exposure(ctx.broker, ctx.prices, rate_of=ctx.rate_for_symbol)
        if fill is not None:
            latest = portfolio.snapshot(
                ctx.broker,
                lambda s: ctx.prices.get(s, 0.0),
                ctx.starting_equity,
                fx_rate_of=ctx.rate_for_symbol,
            )
            state.snap = latest
            final_position = ctx.broker.positions().get(sym)
            fill_accounting = fill_outcome.build_fill_accounting(
                fill=fill,
                symbol=sym,
                action=decision.action,
                intent=decision.intent,
                quantity=effective_quantity,
                price=ctx.prices[sym],
                confidence=decision.confidence,
                llm_provider=decision.llm_provider,
                llm_model=decision.llm_model,
                llm_fallback_reason=decision.llm_fallback_reason,
                equity=latest.equity,
                cash=latest.cash,
                position_quantity=0.0 if final_position is None else final_position.quantity,
            )
            ctx.append_model_performance(**fill_accounting.model_performance)
            entry.update(fill_accounting.entry_updates)
            plan_entry_context: dict = {}
            if runtime_exit_plan and decision.intent in _OPENING_INTENTS:
                _entry_age = ctx.data_age_by_symbol.get(sym)
                plan_entry_context = entry_context.build_trade_entry_context(
                    price=ctx.prices[sym],
                    runtime_interval=ctx.runtime_interval,
                    data_age_minutes=_entry_age,
                    session_open=bool(market.session_snapshot(sym, now=ctx.now).get("open")),
                    daily_as_of=((ctx.execution_eligibility.get(sym) or {}).get("planning") or {}).get("daily_as_of"),
                )
            fill_plan_effects.apply_filled_plan_effects(
                entry=entry,
                broker=ctx.broker,
                plan_store=ctx.plan_store,
                symbol=sym,
                action=decision.action,
                intent=decision.intent,
                quantity=effective_quantity,
                price=ctx.prices[sym],
                opened_at=fill.ts,
                runtime_exit_plan=runtime_exit_plan,
                reference_volatility=reference_volatility,
                llm_provider=decision.llm_provider,
                llm_model=decision.llm_model,
                llm_fallback_reason=decision.llm_fallback_reason,
                llm_confidence=decision.confidence,
                queue_execute_enabled=ctx.queue_execute_enabled,
                entry_thesis=decision.rationale,
                entry_context=plan_entry_context,
            )
        if fill is not None and decision.intent in _OPENING_INTENTS:
            post_entry_wake = market.freshness_budget_minutes(ctx.runtime_interval, grace_minutes=0.0)
            if next_wake_in_minutes is None or next_wake_in_minutes > post_entry_wake:
                next_wake_in_minutes = post_entry_wake
                entry["next_wake_in_minutes"] = next_wake_in_minutes
                entry["post_entry_review_scheduled"] = True
    _log_cycle_progress(
        "[order] %s %s qty=%s price=%s executed=%s plan=%s",
        sym,
        decision.action,
        effective_quantity,
        round(ctx.prices[sym], 6),
        not ctx.dry_run,
        entry["trade_plan_created"],
    )
    apply_decision_schedule()
    ctx.record_decision({**entry, "executed": not ctx.dry_run, "reason": "ok", "price": ctx.prices[sym]})
    return state
