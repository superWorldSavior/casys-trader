"""Deterministic pre-commit evaluation for position-increasing trade plans."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Literal, Protocol

from trader.application.execute import order_admission, risk_admission
from trader.application.execute.fee_estimate import CommissionCalculator
from trader.domain.contracts import Order
from trader.domain.decisions import Decision
from trader.domain.planning.exit_plan_spec import InvalidExitPlanError
from trader.domain.planning.trade_plan import resolve_exit_plan

TradeDirection = Literal["long", "short"]
EconomicsStatus = Literal["positive", "negative", "unknown"]

_UNKNOWN_COMMISSION_MODELS = {"ibkr_unknown", "ibkr_invalid_order"}
_FRACTION_EPSILON = 1e-6


class TradePlanEvaluationGate(
    risk_admission.RiskAdmissionGate,
    risk_admission.FinalRiskGate,
    Protocol,
):
    """Risk capabilities required by the evaluator."""


@dataclass(frozen=True)
class TradePlanCandidate:
    """Agent-authored position intent before it becomes an executable decision."""

    symbol: str
    direction: TradeDirection
    confidence: float
    exit_plan: dict[str, Any] | None
    quantity: float | None = None
    risk_pct: float | None = None
    thesis: dict[str, Any] | None = None
    rationale: str = ""


@dataclass(frozen=True)
class TradePlanEvaluationContext:
    """Immutable daemon-cycle facts used to evaluate one candidate."""

    cycle_id: str
    as_of: str
    price: float
    fx_rate: float
    equity: float
    gross_exposure: float
    position_quantity: float
    position_avg_price: float
    require_hard_stop: bool
    gate: TradePlanEvaluationGate
    reference_volatility: float | None = None
    bars: tuple[object, ...] = ()
    max_quantity: float | None = None
    commission_model: CommissionCalculator | None = None


@dataclass(frozen=True)
class TradePlanEconomics:
    """Payoff metrics for a fully specified bracket."""

    status: EconomicsStatus
    gross_gain_usd: float | None = None
    gross_loss_usd: float | None = None
    fees_if_win_usd: float | None = None
    fees_if_loss_usd: float | None = None
    net_gain_if_win_usd: float | None = None
    net_loss_if_loss_usd: float | None = None
    reward_risk_net: float | None = None
    p_break_even: float | None = None
    expected_value_usd: float | None = None


@dataclass(frozen=True)
class TradePlanEvaluation:
    """Structured evaluation returned to the agent and checked before execution."""

    valid: bool
    symbol: str
    direction: TradeDirection
    cycle_id: str
    as_of: str
    evaluation_id: str | None = None
    action: str | None = None
    intent: str | None = None
    order_quantity: float | None = None
    exposure_quantity: float | None = None
    risk_entry_price: float | None = None
    risk_pct: float | None = None
    max_risk_quantity: float | None = None
    max_quantity: float | None = None
    resolved_exit_plan: dict[str, Any] | None = None
    exit_plan_trace: dict[str, Any] = field(default_factory=dict)
    risk_approved: bool | None = None
    final_gate_approved: bool | None = None
    economics: TradePlanEconomics = field(
        default_factory=lambda: TradePlanEconomics("unknown")
    )
    reasons: tuple[str, ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()

    @property
    def executable_by_gates(self) -> bool:
        return bool(self.valid and self.risk_approved and self.final_gate_approved)

    def to_tool_payload(self) -> dict[str, Any]:
        """Return the compact, JSON-safe contract reinjected into the LLM."""

        economics = {
            "status": self.economics.status,
            "gross_gain_usd": _rounded(self.economics.gross_gain_usd),
            "gross_loss_usd": _rounded(self.economics.gross_loss_usd),
            "fees_if_win_usd": _rounded(self.economics.fees_if_win_usd),
            "fees_if_loss_usd": _rounded(self.economics.fees_if_loss_usd),
            "net_gain_if_win_usd": _rounded(self.economics.net_gain_if_win_usd),
            "net_loss_if_loss_usd": _rounded(self.economics.net_loss_if_loss_usd),
            "reward_risk_net": _rounded(self.economics.reward_risk_net, digits=4),
            "p_break_even": _rounded(self.economics.p_break_even, digits=4),
            "expected_value_usd": _rounded(
                self.economics.expected_value_usd
            ),
        }
        return {
            "valid": self.valid,
            "evaluation_id": self.evaluation_id,
            "cycle_id": self.cycle_id,
            "as_of": self.as_of,
            "symbol": self.symbol,
            "direction": self.direction,
            "action": self.action,
            "intent": self.intent,
            "order_quantity": _rounded(self.order_quantity, digits=8),
            "exposure_quantity": _rounded(self.exposure_quantity, digits=8),
            "risk_entry_price": _rounded(self.risk_entry_price, digits=6),
            "risk_pct": _rounded(self.risk_pct, digits=6),
            "max_risk_quantity": _rounded(self.max_risk_quantity, digits=8),
            "max_quantity": _rounded(self.max_quantity, digits=8),
            "risk_approved": self.risk_approved,
            "final_gate_approved": self.final_gate_approved,
            "executable_by_gates": self.executable_by_gates,
            "resolved_exit_plan": self.resolved_exit_plan,
            "economics": economics,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class TradePlanReferenceValidation:
    approved: bool
    reason: str | None = None
    evaluation: TradePlanEvaluation | None = None


class TradePlanEvaluator:
    """Evaluate candidates against one immutable daemon-cycle snapshot."""

    def __init__(self, context: TradePlanEvaluationContext):
        self._context = context
        self._issued_evaluation_ids: set[str] = set()

    @property
    def cycle_id(self) -> str:
        return self._context.cycle_id

    @property
    def position_quantity(self) -> float:
        return self._context.position_quantity

    def has_issued(self, evaluation_id: str) -> bool:
        return evaluation_id in self._issued_evaluation_ids

    def evaluate(self, candidate: TradePlanCandidate) -> TradePlanEvaluation:
        context = self._context
        base = {
            "symbol": candidate.symbol,
            "direction": candidate.direction,
            "cycle_id": context.cycle_id,
            "as_of": context.as_of,
        }
        input_error = _candidate_error(candidate, context)
        if input_error is not None:
            return TradePlanEvaluation(False, reasons=(input_error,), **base)

        action, intent = _position_aware_intent(
            candidate.direction,
            context.position_quantity,
        )
        if context.position_quantity != 0.0 and candidate.risk_pct is not None:
            return TradePlanEvaluation(
                False,
                action=action,
                intent=intent,
                reasons=("risk_pct_requires_flat_position",),
                **base,
            )

        side = "LONG" if candidate.direction == "long" else "SHORT"
        try:
            resolved_exit_plan, exit_trace = resolve_exit_plan(
                candidate.exit_plan,
                entry_price=context.price,
                side=side,
                reference_volatility=context.reference_volatility,
                bars=list(context.bars) or None,
            )
        except InvalidExitPlanError as exc:
            return TradePlanEvaluation(
                False,
                action=action,
                intent=intent,
                reasons=(f"invalid_exit_plan:{exc}",),
                **base,
            )

        plan_error = _resolved_plan_error(
            resolved_exit_plan,
            direction=candidate.direction,
            entry_price=context.price,
            require_hard_stop=context.require_hard_stop,
        )
        if plan_error is not None:
            return TradePlanEvaluation(
                False,
                action=action,
                intent=intent,
                resolved_exit_plan=resolved_exit_plan,
                exit_plan_trace=exit_trace,
                reasons=(plan_error,),
                **base,
            )

        requested_quantity = float(candidate.quantity or 0.0)
        if intent == "FLIP":
            requested_quantity += abs(context.position_quantity)
        admission = risk_admission.assess_risk_admission(
            risk_admission.RiskAdmissionRequest(
                action=action,
                intent=intent,
                quantity=requested_quantity,
                price=context.price,
                equity=context.equity,
                confidence=candidate.confidence,
                runtime_exit_plan=resolved_exit_plan,
                risk_pct_target=candidate.risk_pct,
                position_quantity=context.position_quantity,
                position_avg_price=context.position_avg_price,
                require_hard_stop=context.require_hard_stop,
                fx_rate=context.fx_rate,
            ),
            gate=context.gate,
        )
        order_quantity = admission.quantity
        risk_updates = admission.entry_updates
        exposure_quantity, risk_entry_price = _risk_basis(
            intent=intent,
            action=action,
            order_quantity=order_quantity,
            price=context.price,
            position_quantity=context.position_quantity,
            position_avg_price=context.position_avg_price,
            updates=risk_updates,
        )

        final_gate = risk_admission.assess_final_risk_gate(
            risk_admission.FinalRiskGateRequest(
                symbol=candidate.symbol,
                action=action,
                quantity=order_quantity,
                rationale=candidate.rationale or "trade_plan_evaluation",
                intent=intent,
                price=context.price,
                position_quantity=context.position_quantity,
                gross_exposure=context.gross_exposure,
                equity=context.equity,
                fx_rate=context.fx_rate,
            ),
            gate=context.gate,
        )

        warnings = _collect_warnings(exit_trace, risk_updates)
        if context.max_quantity is not None and order_quantity > context.max_quantity:
            warnings.append(
                {
                    "code": "risk_capacity_exceeded",
                    "quantity": order_quantity,
                    "max_quantity": context.max_quantity,
                }
            )
        economics, economics_warnings = _compute_economics(
            candidate=candidate,
            context=context,
            resolved_exit_plan=resolved_exit_plan,
            action=action,
            order_quantity=order_quantity,
            exposure_quantity=exposure_quantity,
            risk_entry_price=risk_entry_price,
        )
        warnings.extend(economics_warnings)

        fingerprint = _evaluation_fingerprint(
            candidate=candidate,
            context=context,
            action=action,
            intent=intent,
            resolved_exit_plan=resolved_exit_plan,
            order_quantity=order_quantity,
        )
        reasons = tuple(
            reason
            for reason in (
                admission.reason,
                final_gate.reason,
            )
            if reason
        )
        evaluation = TradePlanEvaluation(
            True,
            evaluation_id=fingerprint,
            action=action,
            intent=intent,
            order_quantity=order_quantity,
            exposure_quantity=exposure_quantity,
            risk_entry_price=risk_entry_price,
            risk_pct=_optional_float(risk_updates.get("risk_pct")),
            max_risk_quantity=_optional_float(
                risk_updates.get("max_risk_qty")
            ),
            max_quantity=context.max_quantity,
            resolved_exit_plan=resolved_exit_plan,
            exit_plan_trace=exit_trace,
            risk_approved=admission.approved,
            final_gate_approved=final_gate.approved,
            economics=economics,
            reasons=reasons,
            warnings=tuple(warnings),
            **base,
        )
        self._issued_evaluation_ids.add(fingerprint)
        return evaluation


def decision_requires_trade_evaluation(decision: Decision) -> bool:
    opening_intent = decision.intent in {
        "OPEN_LONG",
        "OPEN_SHORT",
        "SCALE_IN",
        "FLIP",
    }
    return bool(
        opening_intent
        and (
            decision.action in {"BUY", "SELL"}
            or decision.resolve_from_position
            or decision.intent in {"SCALE_IN", "FLIP"}
        )
    )


def candidate_from_decision(
    decision: Decision,
    *,
    position_quantity: float | None = None,
) -> TradePlanCandidate | None:
    if not decision_requires_trade_evaluation(decision):
        return None
    if decision.intent == "OPEN_LONG":
        direction: TradeDirection = "long"
    elif decision.intent == "OPEN_SHORT":
        direction = "short"
    elif decision.action in {"BUY", "SELL"}:
        direction = "long" if decision.action == "BUY" else "short"
    elif decision.intent == "FLIP" and position_quantity is not None:
        if position_quantity > 0.0:
            direction = "short"
        elif position_quantity < 0.0:
            direction = "long"
        else:
            return None
    elif decision.intent == "SCALE_IN" and position_quantity is not None:
        if position_quantity > 0.0:
            direction = "long"
        elif position_quantity < 0.0:
            direction = "short"
        else:
            return None
    else:
        return None
    return TradePlanCandidate(
        symbol=decision.symbol,
        direction=direction,
        confidence=decision.confidence,
        quantity=(
            None
            if decision.risk_pct_target is not None
            else decision.quantity
        ),
        risk_pct=decision.risk_pct_target,
        exit_plan=decision.exit_plan,
        thesis=decision.thesis,
        rationale=decision.rationale,
    )


def validate_trade_evaluation_reference(
    decision: Decision,
    evaluator: TradePlanEvaluator | None,
) -> TradePlanReferenceValidation:
    if not decision_requires_trade_evaluation(decision):
        return TradePlanReferenceValidation(True)
    if evaluator is None:
        return TradePlanReferenceValidation(
            False,
            "trade_evaluation_unavailable",
        )
    candidate = candidate_from_decision(
        decision,
        position_quantity=getattr(evaluator, "position_quantity", None),
    )
    if candidate is None:
        return TradePlanReferenceValidation(
            False,
            "trade_evaluation_invalid:relative_intent_requires_position",
        )
    evaluation = evaluator.evaluate(candidate)
    if not evaluation.valid:
        reason = evaluation.reasons[0] if evaluation.reasons else "invalid"
        return TradePlanReferenceValidation(
            False,
            f"trade_evaluation_invalid:{reason}",
            evaluation,
        )
    if not decision.trade_evaluation_id:
        return TradePlanReferenceValidation(
            False,
            "trade_evaluation_required",
            evaluation,
        )
    if decision.trade_evaluation_id != evaluation.evaluation_id:
        issued_check = getattr(evaluator, "has_issued", None)
        reason = (
            "trade_evaluation_mismatch"
            if callable(issued_check)
            and issued_check(decision.trade_evaluation_id)
            else "trade_evaluation_stale"
        )
        return TradePlanReferenceValidation(
            False,
            reason,
            evaluation,
        )
    return TradePlanReferenceValidation(True, evaluation=evaluation)


def validate_decision_trade_evaluations(
    decision: Decision,
    evaluator: TradePlanEvaluator | None,
) -> TradePlanReferenceValidation:
    direct = validate_trade_evaluation_reference(decision, evaluator)
    if not direct.approved:
        return direct
    watch = decision.indicator_watch
    if not isinstance(watch, dict) or str(watch.get("on_trigger")) != "EXECUTE_ORDER":
        return direct
    order = watch.get("order")
    if not isinstance(order, dict):
        return TradePlanReferenceValidation(
            False,
            "trade_evaluation_invalid:armed_order_missing",
        )
    from trader.domain.planning.armed_order import normalize_armed_order

    normalized_order = normalize_armed_order(order)
    if normalized_order is None:
        return TradePlanReferenceValidation(
            False,
            "trade_evaluation_invalid:armed_order_malformed",
        )
    try:
        armed_decision = Decision(
            symbol=decision.symbol,
            action=str(normalized_order["action"]),
            quantity=float(normalized_order["qty"]),
            confidence=float(normalized_order["confidence"]),
            rationale=str(normalized_order.get("rationale") or "armed_entry"),
            intent=str(normalized_order["intent"]),
            exit_plan=normalized_order.get("exit_plan"),
            thesis=normalized_order.get("thesis"),
            trade_evaluation_id=normalized_order.get("trade_evaluation_id"),
        )
    except (KeyError, TypeError, ValueError):
        return TradePlanReferenceValidation(
            False,
            "trade_evaluation_invalid:armed_order_malformed",
        )
    return validate_trade_evaluation_reference(armed_decision, evaluator)


def _candidate_error(
    candidate: TradePlanCandidate,
    context: TradePlanEvaluationContext,
) -> str | None:
    if not candidate.symbol or candidate.direction not in {"long", "short"}:
        return "invalid_candidate"
    finite_positive = (
        ("price", context.price),
        ("fx_rate", context.fx_rate),
        ("equity", context.equity),
    )
    for name, value in finite_positive:
        if not math.isfinite(value) or value <= 0.0:
            return f"invalid_{name}"
    if not math.isfinite(candidate.confidence) or not 0.0 <= candidate.confidence <= 1.0:
        return "confidence_out_of_range"
    if candidate.quantity is None and candidate.risk_pct is None:
        return "quantity_or_risk_pct_required"
    if candidate.quantity is not None and (
        not math.isfinite(candidate.quantity) or candidate.quantity <= 0.0
    ):
        return "quantity_must_be_positive"
    if candidate.risk_pct is not None and (
        not math.isfinite(candidate.risk_pct) or candidate.risk_pct <= 0.0
    ):
        return "risk_pct_must_be_positive"
    return None


def _position_aware_intent(
    direction: TradeDirection,
    position_quantity: float,
) -> tuple[str, str]:
    action = "BUY" if direction == "long" else "SELL"
    if position_quantity == 0.0:
        return action, "OPEN_LONG" if direction == "long" else "OPEN_SHORT"
    same_direction = (position_quantity > 0.0) == (direction == "long")
    return (action, "SCALE_IN") if same_direction else (action, "FLIP")


def _resolved_plan_error(
    exit_plan: dict[str, Any] | None,
    *,
    direction: TradeDirection,
    entry_price: float,
    require_hard_stop: bool,
) -> str | None:
    stop = order_admission.hard_stop_price(exit_plan)
    if stop is None:
        return "hard_stop_required" if require_hard_stop else None
    intent = "OPEN_LONG" if direction == "long" else "OPEN_SHORT"
    if order_admission.hard_stop_wrong_side(intent, entry_price, stop):
        return "hard_stop_wrong_side"
    total_fraction = 0.0
    for target in (exit_plan or {}).get("take_profits", []) or []:
        try:
            price = float(target["price"])
            fraction = float(target.get("fraction", 0.0))
        except (KeyError, TypeError, ValueError):
            return "take_profit_invalid"
        if (
            (direction == "long" and price <= entry_price)
            or (direction == "short" and price >= entry_price)
        ):
            return "take_profit_wrong_side"
        total_fraction += fraction
    if total_fraction > 1.0 + _FRACTION_EPSILON:
        return "take_profit_fraction_exceeds_one"
    return None


def _risk_basis(
    *,
    intent: str,
    action: str,
    order_quantity: float,
    price: float,
    position_quantity: float,
    position_avg_price: float,
    updates: dict[str, object],
) -> tuple[float, float]:
    if intent == "SCALE_IN":
        return (
            _optional_float(updates.get("risk_total_position_qty"))
            or order_quantity,
            _optional_float(updates.get("risk_entry_price")) or price,
        )
    if intent == "FLIP":
        return (
            order_admission.flip_open_quantity(
                action=action,
                quantity=order_quantity,
                position_quantity=position_quantity,
            ),
            price,
        )
    return order_quantity, price


def _compute_economics(
    *,
    candidate: TradePlanCandidate,
    context: TradePlanEvaluationContext,
    resolved_exit_plan: dict[str, Any] | None,
    action: str,
    order_quantity: float,
    exposure_quantity: float,
    risk_entry_price: float,
) -> tuple[TradePlanEconomics, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    stop = order_admission.hard_stop_price(resolved_exit_plan)
    targets = list((resolved_exit_plan or {}).get("take_profits", []) or [])
    if stop is None or not targets or exposure_quantity <= 0.0:
        return TradePlanEconomics("unknown"), warnings

    total_fraction = sum(float(item.get("fraction", 0.0)) for item in targets)
    if total_fraction < 1.0 - _FRACTION_EPSILON:
        warnings.append(
            {
                "code": "take_profit_coverage_incomplete",
                "fraction": total_fraction,
            }
        )
        return TradePlanEconomics("unknown"), warnings

    direction_sign = 1.0 if candidate.direction == "long" else -1.0
    gross_gain_native = sum(
        (float(item["price"]) - risk_entry_price)
        * direction_sign
        * exposure_quantity
        * float(item["fraction"])
        for item in targets
    )
    gross_loss_native = (
        abs(risk_entry_price - stop) * exposure_quantity
    )
    gross_gain_usd = gross_gain_native * context.fx_rate
    gross_loss_usd = gross_loss_native * context.fx_rate
    if gross_gain_usd <= 0.0 or gross_loss_usd <= 0.0:
        return TradePlanEconomics("unknown"), warnings

    entry_fee = _commission_usd(
        context.commission_model,
        Order(
            symbol=candidate.symbol,
            side=action,  # type: ignore[arg-type]
            quantity=order_quantity,
        ),
        context.price,
        context.fx_rate,
    )
    exit_side = "SELL" if candidate.direction == "long" else "BUY"
    loss_exit_fee = _commission_usd(
        context.commission_model,
        Order(
            symbol=candidate.symbol,
            side=exit_side,  # type: ignore[arg-type]
            quantity=exposure_quantity,
        ),
        stop,
        context.fx_rate,
    )
    target_fees = [
        _commission_usd(
            context.commission_model,
            Order(
                symbol=candidate.symbol,
                side=exit_side,  # type: ignore[arg-type]
                quantity=exposure_quantity * float(item["fraction"]),
            ),
            float(item["price"]),
            context.fx_rate,
        )
        for item in targets
    ]
    if entry_fee is None or loss_exit_fee is None or any(fee is None for fee in target_fees):
        warnings.append({"code": "commission_unavailable"})
        return TradePlanEconomics("unknown"), warnings

    fees_if_win = entry_fee + sum(fee or 0.0 for fee in target_fees)
    fees_if_loss = entry_fee + loss_exit_fee
    net_gain = gross_gain_usd - fees_if_win
    net_loss = gross_loss_usd + fees_if_loss
    if net_gain <= 0.0 or net_loss <= 0.0:
        return TradePlanEconomics("negative"), warnings
    p_break_even = net_loss / (net_gain + net_loss)
    expected_value = (
        candidate.confidence * net_gain
        - (1.0 - candidate.confidence) * net_loss
    )
    status: EconomicsStatus = "positive" if expected_value > 0.0 else "negative"
    return (
        TradePlanEconomics(
            status,
            gross_gain_usd=gross_gain_usd,
            gross_loss_usd=gross_loss_usd,
            fees_if_win_usd=fees_if_win,
            fees_if_loss_usd=fees_if_loss,
            net_gain_if_win_usd=net_gain,
            net_loss_if_loss_usd=net_loss,
            reward_risk_net=net_gain / net_loss,
            p_break_even=p_break_even,
            expected_value_usd=expected_value,
        ),
        warnings,
    )


def _commission_usd(
    model: CommissionCalculator | None,
    order: Order,
    price: float,
    fx_rate: float,
) -> float | None:
    if model is None:
        return 0.0
    commission = model.calculate(order, price)
    if commission.model in _UNKNOWN_COMMISSION_MODELS:
        return None
    rate = 1.0 if commission.currency == "USD" else fx_rate
    return commission.amount * rate


def _evaluation_fingerprint(
    *,
    candidate: TradePlanCandidate,
    context: TradePlanEvaluationContext,
    action: str,
    intent: str,
    resolved_exit_plan: dict[str, Any] | None,
    order_quantity: float,
) -> str:
    payload = {
        "v": 1,
        "cycle_id": context.cycle_id,
        "as_of": context.as_of,
        "symbol": candidate.symbol,
        "direction": candidate.direction,
        "confidence": float(candidate.confidence),
        "quantity": (
            None if candidate.quantity is None else float(candidate.quantity)
        ),
        "risk_pct": (
            None if candidate.risk_pct is None else float(candidate.risk_pct)
        ),
        "thesis": candidate.thesis,
        "price": float(context.price),
        "fx_rate": float(context.fx_rate),
        "position_quantity": float(context.position_quantity),
        "position_avg_price": float(context.position_avg_price),
        "action": action,
        "intent": intent,
        "order_quantity": float(order_quantity),
        "resolved_exit_plan": resolved_exit_plan,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"tpe_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _collect_warnings(
    exit_trace: dict[str, Any],
    risk_updates: dict[str, object],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for item in risk_updates.get("risk_warnings", []) or []:
        if isinstance(item, dict):
            warnings.append(dict(item))
    hard_stop = exit_trace.get("hard_stop")
    if isinstance(hard_stop, dict):
        for item in hard_stop.get("warnings", []) or []:
            if isinstance(item, dict):
                warnings.append(dict(item))
    return warnings


def _optional_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _rounded(value: float | None, *, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


__all__ = [
    "EconomicsStatus",
    "TradePlanCandidate",
    "TradePlanEconomics",
    "TradePlanEvaluation",
    "TradePlanEvaluationContext",
    "TradePlanEvaluationGate",
    "TradePlanEvaluator",
    "TradePlanReferenceValidation",
    "candidate_from_decision",
    "decision_requires_trade_evaluation",
    "validate_trade_evaluation_reference",
]
