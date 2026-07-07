import pytest

from trader.application.execute.risk_admission import (
    FinalRiskGateRequest,
    RiskAdmissionRequest,
    assess_final_risk_gate,
    assess_risk_admission,
)
from trader.execution.risk import RiskGate, RiskLimits


def _gate(*, confidence_gate_enabled: bool = True) -> RiskGate:
    return RiskGate(
        RiskLimits(
            max_position_value=100_000.0,
            max_gross_exposure=100_000.0,
            max_order_value=100_000.0,
            min_equity=10_000.0,
            max_risk_per_trade_pct=0.01,
            min_trade_confidence=0.7,
            full_risk_confidence=0.9,
            confidence_gate_enabled=confidence_gate_enabled,
        )
    )


def test_assess_risk_admission_derives_quantity_from_risk_pct() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="BUY",
            intent="OPEN_LONG",
            quantity=0.0,
            price=100.0,
            equity=100_000.0,
            confidence=0.95,
            runtime_exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
            risk_pct_target=0.005,
            position_quantity=0.0,
            position_avg_price=0.0,
            require_hard_stop=True,
            fx_rate=1.0,
        ),
        gate=_gate(),
    )

    assert result.approved is True
    assert result.quantity == pytest.approx(100.0)
    assert result.reason is None
    assert result.entry_updates["qty"] == pytest.approx(100.0)
    assert result.entry_updates["risk_pct_target"] == pytest.approx(0.005)
    assert result.entry_updates["risk_qty_derived"] is True
    assert result.entry_updates["risk_clamped"] is False
    assert result.entry_updates["risk_unbounded_no_stop"] is False
    assert result.entry_updates["stop_distance"] == pytest.approx(5.0)
    assert result.entry_updates["risk_pct"] == pytest.approx(0.005)
    assert result.entry_updates["max_risk_qty"] == pytest.approx(200.0)


def test_assess_risk_admission_blocks_risk_pct_without_stop() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="BUY",
            intent="OPEN_LONG",
            quantity=0.0,
            price=100.0,
            equity=100_000.0,
            confidence=0.95,
            runtime_exit_plan=None,
            risk_pct_target=0.005,
            position_quantity=0.0,
            position_avg_price=0.0,
            require_hard_stop=True,
            fx_rate=1.0,
        ),
        gate=_gate(),
    )

    assert result.approved is False
    assert result.quantity == 0.0
    assert result.reason == "risk:risk_sizing_needs_stop"
    assert result.context is None
    assert result.entry_updates == {}


def test_assess_risk_admission_warns_projected_add_risk_budget_without_blocking() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="BUY",
            intent="SCALE_IN",
            quantity=200.0,
            price=120.0,
            equity=100_000.0,
            confidence=0.95,
            runtime_exit_plan={"hard_stop": {"type": "price", "price": 100.0}},
            risk_pct_target=None,
            position_quantity=10.0,
            position_avg_price=100.0,
            require_hard_stop=True,
            fx_rate=1.0,
        ),
        gate=_gate(),
    )

    assert result.approved is True
    assert result.reason is None
    assert result.entry_updates["risk_total_position_qty"] == pytest.approx(210.0)
    assert result.entry_updates["risk_entry_price"] == pytest.approx(119.0476190476)
    assert result.entry_updates["stop_distance"] == pytest.approx(19.0476190476)
    assert result.entry_updates["risk_pct"] == pytest.approx(0.04)
    assert result.entry_updates["max_risk_qty"] == pytest.approx(52.5)
    assert result.context is None
    assert result.entry_updates["risk_warnings"] == [
        {
            "code": "risk_per_trade_exceeded",
            "field": "max_risk_per_trade_pct",
            "risk_qty": 210.0,
            "max_qty": pytest.approx(52.5),
            "risk_pct": pytest.approx(0.04),
            "limit": 0.01,
            "context": (
                "risk_qty=210.0 max_qty=52.50000000 risk_pct=0.04000000000000001 "
                "limit=0.01"
            ),
        }
    ]


def test_assess_risk_admission_keeps_add_order_quantity_when_projected_risk_passes() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="BUY",
            intent="SCALE_IN",
            quantity=10.0,
            price=120.0,
            equity=100_000.0,
            confidence=0.95,
            runtime_exit_plan={"hard_stop": {"type": "price", "price": 100.0}},
            risk_pct_target=None,
            position_quantity=10.0,
            position_avg_price=100.0,
            require_hard_stop=True,
            fx_rate=1.0,
        ),
        gate=_gate(),
    )

    assert result.approved is True
    assert result.quantity == pytest.approx(10.0)
    assert result.entry_updates["risk_total_position_qty"] == pytest.approx(20.0)
    assert result.entry_updates["risk_entry_price"] == pytest.approx(110.0)
    assert result.entry_updates["risk_pct"] == pytest.approx(0.002)


def test_assess_risk_admission_traces_reverse_without_confidence_or_max_risk_gate() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="SELL",
            intent="FLIP",
            quantity=300.0,
            price=100.0,
            equity=100_000.0,
            confidence=0.01,
            runtime_exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
            risk_pct_target=None,
            position_quantity=100.0,
            position_avg_price=100.0,
            require_hard_stop=True,
            fx_rate=1.0,
        ),
        gate=_gate(),
    )

    assert result.approved is True
    assert result.quantity == pytest.approx(300.0)
    assert result.entry_updates["risk_clamped"] is False
    assert result.entry_updates["risk_unbounded_no_stop"] is False
    assert result.entry_updates["stop_distance"] == pytest.approx(5.0)
    assert result.entry_updates["risk_pct"] == pytest.approx(0.01)
    assert "max_risk_qty" not in result.entry_updates


def test_assess_risk_admission_blocks_missing_hard_stop_when_required() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="BUY",
            intent="OPEN_LONG",
            quantity=10.0,
            price=100.0,
            equity=100_000.0,
            confidence=0.95,
            runtime_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 1.0}]},
            risk_pct_target=None,
            position_quantity=0.0,
            position_avg_price=0.0,
            require_hard_stop=True,
            fx_rate=1.0,
        ),
        gate=_gate(),
    )

    assert result.approved is False
    assert result.reason == "risk:missing_hard_stop"
    assert result.entry_updates["risk_unbounded_no_stop"] is True
    assert result.entry_updates["stop_distance"] is None
    assert result.entry_updates["risk_pct"] is None


def test_assess_risk_admission_keeps_unbounded_open_when_stop_optional() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="BUY",
            intent="OPEN_LONG",
            quantity=10.0,
            price=100.0,
            equity=100_000.0,
            confidence=0.05,
            runtime_exit_plan=None,
            risk_pct_target=None,
            position_quantity=0.0,
            position_avg_price=0.0,
            require_hard_stop=False,
            fx_rate=1.0,
        ),
        gate=_gate(confidence_gate_enabled=False),
    )

    assert result.approved is True
    assert result.quantity == 10.0
    assert result.entry_updates["risk_unbounded_no_stop"] is True
    assert result.entry_updates["risk_pct"] is None


def test_assess_risk_admission_blocks_low_confidence_with_context() -> None:
    result = assess_risk_admission(
        RiskAdmissionRequest(
            action="BUY",
            intent="OPEN_LONG",
            quantity=10.0,
            price=100.0,
            equity=100_000.0,
            confidence=0.1,
            runtime_exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
            risk_pct_target=None,
            position_quantity=0.0,
            position_avg_price=0.0,
            require_hard_stop=True,
            fx_rate=1.0,
        ),
        gate=_gate(),
    )

    assert result.approved is False
    assert result.reason == "risk:confidence_below_required"
    assert result.context == "confidence=0.1 required=0.7100 planned_risk_pct=0.0005"


def test_assess_final_risk_gate_maps_order_value_rejection() -> None:
    result = assess_final_risk_gate(
        FinalRiskGateRequest(
            symbol="SPY",
            action="BUY",
            quantity=20.0,
            rationale="too large",
            intent="OPEN_LONG",
            price=100.0,
            position_quantity=0.0,
            gross_exposure=0.0,
            equity=100_000.0,
            fx_rate=1.0,
        ),
        gate=RiskGate(
            RiskLimits(
                max_position_value=100_000.0,
                max_gross_exposure=100_000.0,
                max_order_value=1_000.0,
                min_equity=10_000.0,
            )
        ),
    )

    assert result.approved is False
    assert result.order.symbol == "SPY"
    assert result.order.side == "BUY"
    assert result.order.quantity == pytest.approx(20.0)
    assert result.reason == "risk:order_value_exceeded"
    assert result.code == "order_value_exceeded"
    assert result.context == "2000.00 > 1000.0"


def test_assess_final_risk_gate_allows_reduce_through_order_value_cap() -> None:
    result = assess_final_risk_gate(
        FinalRiskGateRequest(
            symbol="SPY",
            action="SELL",
            quantity=10.0,
            rationale="reduce risk",
            intent="REDUCE",
            price=100.0,
            position_quantity=20.0,
            gross_exposure=2_000.0,
            equity=100_000.0,
            fx_rate=1.0,
        ),
        gate=RiskGate(
            RiskLimits(
                max_position_value=100_000.0,
                max_gross_exposure=100_000.0,
                max_order_value=100.0,
                min_equity=10_000.0,
            )
        ),
    )

    assert result.approved is True
    assert result.reason is None
    assert result.order.side == "SELL"
    assert result.order.quantity == pytest.approx(10.0)


def test_assess_final_risk_gate_uses_fx_for_existing_position_value() -> None:
    result = assess_final_risk_gate(
        FinalRiskGateRequest(
            symbol="2379.TW",
            action="BUY",
            quantity=100.0,
            rationale="would exceed position cap after fx",
            intent="SCALE_IN",
            price=800.0,
            position_quantity=100.0,
            gross_exposure=2_480.0,
            equity=100_000.0,
            fx_rate=0.031,
        ),
        gate=RiskGate(
            RiskLimits(
                max_position_value=4_000.0,
                max_gross_exposure=100_000.0,
                max_order_value=100_000.0,
                min_equity=10_000.0,
            )
        ),
    )

    assert result.approved is False
    assert result.reason == "risk:position_value_exceeded"
    assert result.context == "4960.00 > 4000.0"
