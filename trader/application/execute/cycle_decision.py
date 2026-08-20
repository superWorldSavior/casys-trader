"""Application service for applying one per-symbol cycle decision."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

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
from trader.application.cycle.market_snapshot import MissingFxRate
from trader.application.portfolio import snapshot as portfolio
from trader.domain.execution.risk_gate import RiskGate
from trader.domain.market import sessions as market
from trader.domain.market import volatility as reference_volatility_service
from trader.domain.market.execution_eligibility import execution_blocked_reason
from trader.domain.planning.exit_plan_spec import InvalidExitPlanError, validate_exit_plan
from trader.domain.planning.trade_plan import apply_exit_update, resolve_exit_plan
from trader.domain.decisions import Decision
from trader.domain.planning.protocols import SchedulerLike
from trader.support.metadata import experiment as experiment_metadata

_OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "FLIP", "SCALE_IN"}
_PURE_OPEN_INTENTS = {"OPEN_LONG", "OPEN_SHORT"}
_RISK_GUARDED_OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "SCALE_IN"}
_RELATIVE_ORDER_INTENTS = order_admission.RELATIVE_ORDER_INTENTS
_EXECUTE_POLL_BUDGET_S: float = 10.0


def _fx_rate_or_none(rate_for_symbol: Callable[[str], float], symbol: str) -> float | None:
    try:
        return rate_for_symbol(symbol)
    except MissingFxRate:
        return None


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
    # Compatibility fallback for application-level callers; the daemon always
    # supplies the stable ledger identity reserved before execution.
    decision_id_for_symbol: Callable[[str], str] = lambda symbol: symbol
    process_identity_for_symbol: Callable[[str], dict[str, str]] = lambda _symbol: {}
    append_event: Callable[..., None] = _noop_event
    append_model_performance: Callable[..., None] = _noop_model_performance
    logger: logging.Logger = logging.getLogger("casys-trader")
    trade_plan_evaluator_provider: Callable[[str], object | None] | None = None
    reference_volatilities: dict[str, float | None] = field(default_factory=dict)
    experiment_context: dict | None = None


def _capture_exit_plan_trace(entry: dict, trace: dict) -> None:
    if not trace:
        return
    entry["exit_plan_trace"] = copy.deepcopy(trace)
    hard_stop = trace.get("hard_stop") if isinstance(trace, dict) else None
    warnings = hard_stop.get("warnings") if isinstance(hard_stop, dict) else None
    if isinstance(warnings, list) and warnings:
        entry["exit_plan_warnings"] = copy.deepcopy(warnings)


def _capture_plan_effect_readback(*, entry: dict, plan_store: object, symbol: str) -> None:
    """Attach a semantic plan-store receipt after a plan mutation."""
    try:
        plans = [plan for plan in plan_store.open_plans() if plan.symbol == symbol]
    except Exception as exc:  # noqa: BLE001 - missing proof must stay explicit
        entry["plan_effect"] = {"status": "unavailable", "error": type(exc).__name__}
        return
    observed_plans = [fill_plan_effects.plan_snapshot(plan) for plan in plans]
    effect = {
        "status": "unverified",
        "open_plans": [
            {
                "plan_id": plan.id,
                "entry_decision_id": plan.entry_decision_id,
                "remaining_quantity": plan.remaining_quantity,
            }
            for plan in plans
        ],
    }
    expectation = entry.get("plan_receipt_expectation")
    if not isinstance(expectation, dict):
        return _set_plan_effect(entry, effect)
    effect["expected_mutation"] = copy.deepcopy(expectation)
    kind = expectation.get("kind")
    if kind == "upsert" and isinstance(expectation.get("plan"), dict):
        expected_plan = expectation["plan"]
        effect["expected_plan"] = copy.deepcopy(expected_plan)
        matched = next((plan for plan in observed_plans if plan == expected_plan), None)
        if matched is None:
            effect["status"] = "mismatch"
            effect["reason"] = "plan_receipt_mismatch"
            return _set_plan_effect(entry, effect)
        effect["observed_plan"] = matched
        effect["status"] = "verified"
        return _set_plan_effect(entry, effect)
    if kind == "no_open_plans":
        effect["expected_open_plan_count"] = 0
        if plans:
            effect["status"] = "mismatch"
            effect["reason"] = "plan_receipt_mismatch"
        else:
            effect["status"] = "verified"
        return _set_plan_effect(entry, effect)
    if kind == "remaining_quantity":
        expected_quantity = expectation.get("quantity")
        effect["expected_remaining_quantity"] = expected_quantity
        if expected_quantity == 0 and not plans:
            effect["status"] = "verified"
            return _set_plan_effect(entry, effect)
        if (
            isinstance(expected_quantity, (int, float))
            and len(plans) == 1
            and plans[0].remaining_quantity == float(expected_quantity)
        ):
            effect["status"] = "verified"
        else:
            effect["status"] = "mismatch"
            effect["reason"] = "plan_receipt_mismatch"
        return _set_plan_effect(entry, effect)
    if kind in {"unchanged", "exact_plans"} and isinstance(expectation.get("plans"), list):
        expected_plans = expectation["plans"]
        effect["expected_plans"] = copy.deepcopy(expected_plans)
        effect["observed_plans"] = copy.deepcopy(observed_plans)
        if observed_plans == expected_plans:
            effect["status"] = "verified"
        else:
            effect["status"] = "mismatch"
            effect["reason"] = "plan_receipt_mismatch"
        return _set_plan_effect(entry, effect)
    if kind == "last_llm_review" and isinstance(expectation.get("review"), dict):
        expected_review = expectation["review"]
        observed_reviews = [
            {
                "plan_id": plan.id,
                "last_llm_review": copy.deepcopy(plan.last_llm_review),
            }
            for plan in plans
        ]
        effect["expected_last_llm_review"] = copy.deepcopy(expected_review)
        effect["observed_last_llm_reviews"] = observed_reviews
        if plans and all(plan.last_llm_review == expected_review for plan in plans):
            effect["status"] = "verified"
        else:
            effect["status"] = "mismatch"
            effect["reason"] = "plan_receipt_mismatch"
        return _set_plan_effect(entry, effect)
    _set_plan_effect(entry, effect)


def _set_plan_effect(entry: dict, effect: dict) -> None:
    entry["plan_effect"] = effect
    entry.setdefault("plan_effects", []).append(copy.deepcopy(effect))


def _set_plan_receipt_expectation(entry: dict, *, kind: str, **payload: object) -> None:
    entry["plan_receipt_expectation"] = {"kind": kind, **payload}


def _expected_exit_update_plan_snapshot(
    *,
    plan_store: object,
    symbol: str,
    exit_update: dict,
    bars: list | None,
    current_price: float | None,
) -> dict | None:
    """Derive the pure expected post-update snapshot before the store mutation."""
    try:
        plan = next(plan for plan in plan_store.open_plans() if plan.symbol == symbol)
        expected = apply_exit_update(
            plan,
            exit_update,
            bars=bars,
            reference_price=current_price,
        )
    except (InvalidExitPlanError, StopIteration, ValueError):
        return None
    return fill_plan_effects.plan_snapshot(expected)


def _entry_dimensions(decision: Decision, cockpit: dict) -> dict[str, str]:
    thesis = decision.thesis if isinstance(decision.thesis, dict) else {}
    regime = "unknown"
    cols = cockpit.get("cols")
    rows = cockpit.get("rows")
    if isinstance(cols, list) and isinstance(rows, list) and "reg" in cols:
        symbol_index = cols.index("s") if "s" in cols else 0
        regime_index = cols.index("reg")
        for row in rows:
            if (
                isinstance(row, list)
                and len(row) > max(symbol_index, regime_index)
                and str(row[symbol_index]) == decision.symbol
            ):
                regime = str(row[regime_index] or "unknown")
                break
    side = "long" if decision.action == "BUY" else "short"
    return {
        "setup": str(thesis.get("setup") or "unknown"),
        "horizon": str(thesis.get("horizon") or "unknown"),
        "side": side,
        "regime": regime,
    }


def execute_one_cycle_decision(
    *,
    sym: str,
    index: int,
    total: int,
    decision: Decision,
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

    # Validate the agent-authored target before relative intents are expanded
    # into broker order quantities. In particular, a FLIP target quantity must
    # not be charged the existing position twice.
    from trader.application.execute.trade_plan_evaluation import (
        validate_decision_trade_evaluations,
    )

    evaluator = (
        None
        if ctx.trade_plan_evaluator_provider is None
        else ctx.trade_plan_evaluator_provider(sym)
    )
    evaluation_validation = validate_decision_trade_evaluations(decision, evaluator)

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
    armed_order = ctx.armed_plan_orders.get(sym)
    inherited = experiment_metadata.inherited_experiment(
        armed_order.get("experiment") if isinstance(armed_order, dict) else None
    )
    decision_experiment = inherited or experiment_metadata.decision_experiment(
        ctx.experiment_context,
        provider=decision.llm_provider,
        model=decision.llm_model,
    )
    entry["experiment_id"] = decision_experiment["experiment_id"]
    entry["experiment"] = decision_experiment
    # La même identité suit le learning, le ledger et les fills associés. Elle
    # est réservée avant tout side effect d'exécution.
    entry["decision_id"] = ctx.decision_id_for_symbol(sym)
    if decision.intent in _OPENING_INTENTS:
        entry["entry_dimensions"] = _entry_dimensions(decision, ctx.cockpit)
    process_identity = dict(ctx.process_identity_for_symbol(sym))
    entry.update(process_identity)

    def record_outcome(payload: dict) -> None:
        if "admission_status" not in payload:
            if payload.get("action") == "HOLD":
                payload["admission_status"] = "not_required"
            elif payload.get("queue_task_id") is not None or payload.get("reason") == "ok":
                payload["admission_status"] = "admitted"
            elif payload.get("executed") is False:
                payload["admission_status"] = "rejected"
        ctx.record_decision(payload)

    if evaluation_validation.evaluation is not None:
        entry["trade_plan_evaluation"] = (
            evaluation_validation.evaluation.to_tool_payload()
        )
    if not evaluation_validation.approved:
        entry["trade_evaluation_rejection"] = evaluation_validation.reason
        record_outcome(
            {
                **entry,
                "executed": False,
                "reason": evaluation_validation.reason
                or "trade_evaluation_invalid",
            }
        )
        return state

    if sym in ctx.prices:
        entry["price"] = ctx.prices[sym]
    decision_source = str(entry["decision_source"])
    if decision_source == "llm" and sym in ctx.held_symbols and decision_entries.counts_as_llm_review(decision):
        persisted_review = plan_review.persist_last_llm_review(
            plan_store=ctx.plan_store,
            symbol=sym,
            now=ctx.now,
            decision=decision,
            decision_id=str(entry["decision_id"]),
        )
        if persisted_review is not None:
            entry["plan_review_recorded"] = True
            _set_plan_receipt_expectation(
                entry,
                kind="last_llm_review",
                review=persisted_review,
                reason="last_llm_review_persisted",
            )
            _capture_plan_effect_readback(
                entry=entry,
                plan_store=ctx.plan_store,
                symbol=sym,
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
    if (
        pending_indicator_watch is not None
        and decision_experiment["experiment_id"] is not None
        and isinstance(pending_indicator_watch.get("order"), dict)
    ):
        pending_indicator_watch = copy.deepcopy(pending_indicator_watch)
        pending_indicator_watch["order"]["experiment"] = copy.deepcopy(
            decision_experiment
        )

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
        entry["schedule_effect"] = cycle_schedule.read_schedule_effect(
            ctx.sched,
            sym=sym,
            now=ctx.now,
            expected_next_wake=None,
        )

    if decision.action in {"BUY", "SELL"} and effective_quantity == 0 and decision.risk_pct_target is None:
        _log_cycle_progress("[decision %d/%d] %s blocked zero_quantity_order", index, total, sym)
        apply_default_schedule_after_blocked()
        record_outcome({**entry, "executed": False, "reason": "zero_quantity_order"})
        return state

    if decision.action == "HOLD" or (effective_quantity == 0 and decision.risk_pct_target is None):
        apply_decision_schedule()
        if decision.exit_update:
            try:
                plans_before_exit_update = [
                    fill_plan_effects.plan_snapshot(plan) for plan in ctx.plan_store.open_plans() if plan.symbol == sym
                ]
            except Exception:  # noqa: BLE001 - absence de preuve reste explicite
                plans_before_exit_update = None
            expected_exit_plan = _expected_exit_update_plan_snapshot(
                plan_store=ctx.plan_store,
                symbol=sym,
                exit_update=decision.exit_update,
                bars=ctx.tradable_bars_by_symbol.get(sym),
                current_price=ctx.prices.get(sym),
            )
            exit_update_result = exit_update_service.apply_exit_update_to_open_plan(
                plan_store=ctx.plan_store,
                symbol=sym,
                exit_update=decision.exit_update,
                bars=ctx.tradable_bars_by_symbol.get(sym),
                entry=entry,
                current_price=ctx.prices.get(sym),
            )
            if expected_exit_plan is not None:
                _set_plan_receipt_expectation(
                    entry,
                    kind="upsert",
                    plan=expected_exit_plan,
                )
            elif exit_update_result.reason == "no_open_plan":
                _set_plan_receipt_expectation(entry, kind="no_open_plans")
            elif plans_before_exit_update is not None:
                _set_plan_receipt_expectation(
                    entry,
                    kind="unchanged",
                    plans=plans_before_exit_update,
                    reason=exit_update_result.reason,
                )
            _capture_plan_effect_readback(
                entry=entry,
                plan_store=ctx.plan_store,
                symbol=sym,
            )
        hold_reason = decision_entries.hold_reason_for_decision(
            decision_source=decision_source,
            rationale=decision.rationale,
        )
        record_outcome({**entry, "executed": False, "reason": hold_reason})
        return state

    invalid_intent = order_admission.invalid_intent_reason(
        action=decision.action,
        quantity=decision.quantity,
        intent=decision.intent,
    )
    if invalid_intent is not None:
        _log_cycle_progress("[decision %d/%d] %s blocked %s", index, total, sym, invalid_intent)
        apply_default_schedule_after_blocked()
        record_outcome({**entry, "executed": False, "reason": invalid_intent})
        return state

    execution_blocked = execution_blocked_reason(
        ctx.execution_eligibility,
        sym,
        fail_closed=decision.intent in _OPENING_INTENTS,
    )
    if execution_blocked is not None:
        _log_cycle_progress("[execution] %s ordre bloqué (%s) — watch/wake conservés", sym, execution_blocked)
        apply_decision_schedule()
        record_outcome({**entry, "executed": False, "reason": execution_blocked})
        return state

    fx_rate = _fx_rate_or_none(ctx.rate_for_symbol, sym)
    if fx_rate is None:
        _log_cycle_progress("[execution] %s ordre bloqué (missing_fx_rate) — watch/wake conservés", sym)
        apply_decision_schedule()
        record_outcome({**entry, "executed": False, "reason": "missing_fx_rate"})
        return state

    if runtime_exit_plan and decision.intent in _OPENING_INTENTS:
        if sym in ctx.armed_reference_volatilities:
            reference_volatility = ctx.armed_reference_volatilities[sym]
        elif sym in ctx.reference_volatilities:
            reference_volatility = ctx.reference_volatilities[sym]
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
            record_outcome({**entry, "executed": False, "reason": f"invalid_exit_plan:{exc}"})
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
            record_outcome({**entry, "executed": False, "reason": "invalid_exit_plan:hard_stop_wrong_side"})
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
        record_outcome({**entry, "executed": False, "reason": exit_block_reason})
        return state
    if clamped_quantity != effective_quantity:
        entry.setdefault("requested_qty", effective_quantity)
        effective_quantity = clamped_quantity
        entry["qty"] = effective_quantity
    if effective_quantity == 0 and decision.risk_pct_target is None:
        _log_cycle_progress("[decision %d/%d] %s hold zero_exit_quantity", index, total, sym)
        apply_default_schedule_after_blocked()
        record_outcome({**entry, "executed": False, "reason": "zero_exit_quantity"})
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
            fx_rate=fx_rate,
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
        record_outcome(blocked_entry)
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
            fx_rate=fx_rate,
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
        record_outcome(
            {
                **entry,
                "executed": False,
                "reason": final_risk_outcome.reason,
                "context": final_risk_outcome.context,
            }
        )
        return state

    if process_identity:
        order = order.model_copy(
            update={
                "process_instance_id": process_identity.get("process_instance_id"),
                "attempt_id": process_identity.get("attempt_id"),
                "decision_id": str(entry["decision_id"]),
            }
        )

    # Admis (gates execution + risk passés) : consommer avant submit/queue
    # pour ne pas re-déclencher le même EXECUTE_ORDER si la file timeout.
    armed_watch_id = ctx.armed_plan_ids.get(sym)
    if ctx.sched is not None and armed_watch_id:
        ctx.sched.remove_indicator_watch(armed_watch_id)

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
            entry_decision_id=str(entry["decision_id"]),
        )
        _exec_outcome = execute_queue_dispatch.dispatch_execute_order_via_queue(
            ledger=ctx.execute_ledger,
            symbol=sym,
            side=decision.action,
            quantity=effective_quantity,
            rationale=decision.rationale,
            price=ctx.prices[sym],
            ts=ctx.now.isoformat(),
            fx_rate=fx_rate,
            dry_run=ctx.dry_run,
            plan_to_upsert=_exec_plan_payload.plan_to_upsert,
            symbol_to_close=_exec_plan_payload.symbol_to_close,
            symbol_to_sync_quantity=_exec_plan_payload.symbol_to_sync_quantity,
            cycle_id=ctx.now.isoformat(),
            intent=decision.intent,
            budget_s=_EXECUTE_POLL_BUDGET_S,
            process_instance_id=process_identity.get("process_instance_id"),
            attempt_id=process_identity.get("attempt_id"),
            decision_id=(str(entry["decision_id"]) if process_identity else None),
        )
        entry.update(
            {
                "queue_task_id": _exec_outcome.task_id,
                "queue_terminal": _exec_outcome.terminal,
                "queue_late_execution_risk": _exec_outcome.late_execution_risk,
                "queue_abandoned": _exec_outcome.abandoned,
                "queue_fill_verified": _exec_outcome.verified,
            }
        )
        if _exec_outcome.reason is not None:
            if decision.intent in _OPENING_INTENTS:
                state.deferred_opening_symbols.add(sym)
                if _exec_outcome.terminal == "timeout":
                    state.opening_batch_timed_out = True
            apply_default_schedule_after_blocked()
            entry["effect_status"] = (
                "unknown"
                if _exec_outcome.late_execution_risk
                or "no_fill" in _exec_outcome.reason
                or "mismatch" in _exec_outcome.reason
                else "not_applied"
            )
            if entry["effect_status"] == "unknown":
                entry["process_state"] = "recovery_required"
            record_outcome({**entry, "executed": False, "reason": _exec_outcome.reason})
            return state
        fill = _exec_outcome.fill
    else:
        fill = ctx.broker.submit(
            order,
            ctx.prices[sym],
            ctx.now.isoformat(),
            dry_run=ctx.dry_run,
            fx_rate=fx_rate,
        )
    if fill is None and not ctx.dry_run:
        apply_default_schedule_after_blocked()
        entry.update(
            {
                "effect_status": "unknown",
                "process_state": "recovery_required",
            }
        )
        record_outcome(
            {
                **entry,
                "executed": False,
                "reason": "broker_fill_missing",
                "price": ctx.prices[sym],
            }
        )
        return state
    if fill is not None and (
        fill.symbol != sym
        or (
            process_identity
            and any(
                (
                    fill.process_instance_id != process_identity.get("process_instance_id"),
                    fill.attempt_id != process_identity.get("attempt_id"),
                    fill.decision_id != str(entry["decision_id"]),
                )
            )
        )
    ):
        apply_default_schedule_after_blocked()
        entry.update(
            {
                "effect_status": "unknown",
                "process_state": "recovery_required",
            }
        )
        record_outcome(
            {
                **entry,
                "executed": False,
                "reason": "broker_fill_correlation_mismatch",
                "price": ctx.prices[sym],
            }
        )
        return state
    if not ctx.dry_run:
        state.gross = risk_capacity.gross_exposure(
            ctx.broker,
            ctx.prices,
            rate_of=lambda symbol: _fx_rate_or_none(ctx.rate_for_symbol, symbol),
        )
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
                decision_id=str(entry["decision_id"]),
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
            entry["effect_status"] = "verified"
            entry["effect_refs"] = {
                "broker_fill": {
                    "symbol": fill.symbol,
                    "process_instance_id": fill.process_instance_id,
                    "attempt_id": fill.attempt_id,
                    "decision_id": fill.decision_id,
                    "ts": fill.ts,
                },
                "portfolio_readback": {
                    "cash": latest.cash,
                    "equity": latest.equity,
                    "position_quantity": (0.0 if final_position is None else final_position.quantity),
                },
            }
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
                entry_decision_id=str(entry["decision_id"]),
            )
            if entry.get("trade_plan_created") and isinstance(entry.get("trade_plan"), dict):
                _set_plan_receipt_expectation(
                    entry,
                    kind="upsert",
                    plan=copy.deepcopy(entry.get("trade_plan")),
                )
            elif decision.intent in {"CLOSE", "FLIP"}:
                _set_plan_receipt_expectation(entry, kind="no_open_plans")
            elif decision.intent == "REDUCE":
                final_position = ctx.broker.positions().get(sym)
                expected_remaining_quantity = 0.0 if final_position is None else abs(final_position.quantity)
                _set_plan_receipt_expectation(
                    entry,
                    kind="remaining_quantity",
                    quantity=expected_remaining_quantity,
                )
            _capture_plan_effect_readback(
                entry=entry,
                plan_store=ctx.plan_store,
                symbol=sym,
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
    if ctx.dry_run:
        entry["effect_status"] = "not_applied_dry_run"
        entry["execution_mode"] = "dry_run"
    record_outcome({**entry, "executed": not ctx.dry_run, "reason": "ok", "price": ctx.prices[sym]})
    return state
