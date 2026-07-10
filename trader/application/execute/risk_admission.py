"""Application-level risk admission for order execution decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, cast

import trader.application.execute.order_admission as order_admission
from trader.domain.orders import Side
from trader.domain.contracts import Order

_PURE_OPEN_INTENTS = {"OPEN_LONG", "OPEN_SHORT"}
_RISK_GUARDED_OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "SCALE_IN"}


class _RiskLimitsLike(Protocol):
    max_risk_per_trade_pct: float


class _RiskVerdictLike(Protocol):
    approved: bool
    code: str
    context: str


class RiskAdmissionGate(Protocol):
    limits: _RiskLimitsLike

    def max_quantity_at_risk(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        *,
        fx_rate: float = 1.0,
    ) -> float: ...

    def check_confidence(
        self,
        confidence: float | None,
        planned_risk_pct: float | None,
    ) -> _RiskVerdictLike: ...


class FinalRiskGate(Protocol):
    def check(
        self,
        order: Order,
        price: float,
        *,
        current_position_value: float,
        gross_exposure: float,
        equity: float,
        allow_risk_reduction: bool = False,
        fx_rate: float = 1.0,
    ) -> _RiskVerdictLike: ...


@dataclass(frozen=True)
class RiskAdmissionRequest:
    action: str
    intent: str | None
    quantity: float
    price: float
    equity: float
    confidence: float | None
    runtime_exit_plan: dict | None
    risk_pct_target: float | None
    position_quantity: float
    position_avg_price: float
    require_hard_stop: bool
    fx_rate: float = 1.0


@dataclass(frozen=True)
class RiskAdmissionResult:
    approved: bool
    quantity: float
    reason: str | None = None
    context: str | None = None
    entry_updates: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FinalRiskGateRequest:
    symbol: str
    action: str
    quantity: float
    rationale: str
    intent: str | None
    price: float
    position_quantity: float
    gross_exposure: float
    equity: float
    fx_rate: float = 1.0


@dataclass(frozen=True)
class FinalRiskGateResult:
    approved: bool
    order: Order
    reason: str | None = None
    code: str = "ok"
    context: str = ""


def _set_risk_metrics(
    updates: dict[str, object],
    *,
    quantity: float,
    stop_distance: float | None,
    equity: float,
) -> None:
    updates["stop_distance"] = stop_distance
    updates["risk_pct"] = order_admission.risk_pct_for_quantity(quantity, stop_distance, equity)


def _append_risk_warning(updates: dict[str, object], warning: dict[str, object]) -> None:
    warnings = updates.setdefault("risk_warnings", [])
    if isinstance(warnings, list):
        warnings.append(warning)


def _risk_stop_intent(intent: str | None, action: str) -> str | None:
    if intent != "SCALE_IN":
        return intent
    if action == "BUY":
        return "OPEN_LONG"
    if action == "SELL":
        return "OPEN_SHORT"
    return intent


def assess_risk_admission(
    request: RiskAdmissionRequest,
    *,
    gate: RiskAdmissionGate,
) -> RiskAdmissionResult:
    """Evaluate opening-side risk gates and return entry updates for the daemon."""
    intent = request.intent
    pure_open = intent in _PURE_OPEN_INTENTS
    risk_guarded_open = intent in _RISK_GUARDED_OPENING_INTENTS
    trace_risk = pure_open or intent == "FLIP" or intent == "SCALE_IN"
    if not trace_risk:
        return RiskAdmissionResult(True, request.quantity)

    hard_stop_price = order_admission.hard_stop_price(request.runtime_exit_plan)
    if pure_open and request.risk_pct_target is not None:
        if hard_stop_price is None:
            return RiskAdmissionResult(
                False,
                request.quantity,
                reason="risk:risk_sizing_needs_stop",
            )
        stop_distance_native = abs(request.price - hard_stop_price)
        quantity = order_admission.qty_from_risk_pct(
            request.risk_pct_target,
            request.equity,
            stop_distance_native,
            fx_rate=request.fx_rate,
        )
        updates: dict[str, object] = {
            "qty": quantity,
            "risk_pct_target": request.risk_pct_target,
            "risk_qty_derived": True,
        }
    else:
        quantity = request.quantity
        updates = {}

    risk_quantity = quantity
    risk_entry_price = request.price
    if intent == "FLIP":
        risk_quantity = order_admission.flip_open_quantity(
            action=request.action,
            quantity=quantity,
            position_quantity=request.position_quantity,
        )
    elif intent == "SCALE_IN":
        risk_quantity, risk_entry_price = order_admission.projected_scale_in_risk_basis(
            action=request.action,
            scale_in_quantity=quantity,
            scale_in_price=request.price,
            position_quantity=request.position_quantity,
            position_avg_price=request.position_avg_price,
        )
        updates["risk_total_position_qty"] = risk_quantity
        updates["risk_entry_price"] = risk_entry_price

    updates["risk_clamped"] = False
    updates["risk_unbounded_no_stop"] = hard_stop_price is None
    if hard_stop_price is None:
        _set_risk_metrics(
            updates,
            quantity=risk_quantity,
            stop_distance=None,
            equity=request.equity,
        )
        if risk_guarded_open and request.require_hard_stop:
            return RiskAdmissionResult(
                False,
                quantity,
                reason="risk:missing_hard_stop",
                entry_updates=updates,
            )
    else:
        stop_distance = order_admission.loss_distance_to_stop(
            _risk_stop_intent(intent, request.action),
            risk_entry_price,
            hard_stop_price,
        )
        _set_risk_metrics(
            updates,
            quantity=risk_quantity,
            stop_distance=stop_distance,
            equity=request.equity,
        )
        if risk_guarded_open and stop_distance > 0.0:
            max_risk_quantity = gate.max_quantity_at_risk(
                request.equity,
                risk_entry_price,
                hard_stop_price,
                fx_rate=request.fx_rate,
            )
            updates["max_risk_qty"] = max_risk_quantity
            if max_risk_quantity <= 0:
                return RiskAdmissionResult(
                    False,
                    quantity,
                    reason="zero_risk_quantity",
                    entry_updates=updates,
                )
            if risk_quantity > max_risk_quantity:
                context = (
                    f"risk_qty={risk_quantity} max_qty={max_risk_quantity:.8f} "
                    f"risk_pct={updates.get('risk_pct')} "
                    f"limit={gate.limits.max_risk_per_trade_pct}"
                )
                _append_risk_warning(
                    updates,
                    {
                        "code": "risk_per_trade_exceeded",
                        "field": "max_risk_per_trade_pct",
                        "risk_qty": risk_quantity,
                        "max_qty": max_risk_quantity,
                        "risk_pct": updates.get("risk_pct"),
                        "limit": gate.limits.max_risk_per_trade_pct,
                        "context": context,
                    },
                )

    if risk_guarded_open and quantity == 0:
        return RiskAdmissionResult(
            False,
            quantity,
            reason="zero_risk_quantity",
            entry_updates=updates,
        )

    if risk_guarded_open:
        confidence_verdict = gate.check_confidence(
            request.confidence,
            updates.get("risk_pct"),  # type: ignore[arg-type]
        )
        if not confidence_verdict.approved:
            return RiskAdmissionResult(
                False,
                quantity,
                reason=f"risk:{confidence_verdict.code}",
                context=confidence_verdict.context,
                entry_updates=updates,
            )

    return RiskAdmissionResult(True, quantity, entry_updates=updates)


def assess_final_risk_gate(
    request: FinalRiskGateRequest,
    *,
    gate: FinalRiskGate,
) -> FinalRiskGateResult:
    """Build the executable order and evaluate the final deterministic risk gate."""
    order = Order(
        symbol=request.symbol,
        side=cast(Side, request.action),
        quantity=request.quantity,
        rationale=request.rationale,
    )
    current_position_value = request.position_quantity * request.price * request.fx_rate
    allow_risk_reduction = request.intent in {"REDUCE", "CLOSE"}
    verdict = gate.check(
        order,
        request.price,
        current_position_value=current_position_value,
        gross_exposure=request.gross_exposure,
        equity=request.equity,
        allow_risk_reduction=allow_risk_reduction,
        fx_rate=request.fx_rate,
    )
    if not verdict.approved:
        return FinalRiskGateResult(
            False,
            order,
            reason=f"risk:{verdict.code}",
            code=verdict.code,
            context=verdict.context,
        )
    return FinalRiskGateResult(True, order)
