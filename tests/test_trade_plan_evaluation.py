import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trader import agent_tools
from trader.agent.protocol import parsing
from trader.agent.tools import AgentToolCall, ToolContext, validate_tool_call
from trader.application.execute.trade_plan_evaluation import (
    TradePlanCandidate,
    TradePlanEvaluationContext,
    TradePlanEvaluator,
    validate_decision_trade_evaluations,
    validate_trade_evaluation_reference,
)
from trader.domain.contracts import Commission
from trader.domain.decisions import Decision
from trader.domain.execution.risk_gate import RiskGate
from trader.domain.risk import RiskLimits
from trader.infrastructure.brokers.commission_models import IbkrCommissionModel


def _context(**overrides) -> TradePlanEvaluationContext:
    values = {
        "cycle_id": "2026-08-18T03:00:00+00:00",
        "as_of": "2026-08-18T02:59:00+00:00",
        "price": 100.0,
        "fx_rate": 1.0,
        "equity": 100_000.0,
        "gross_exposure": 0.0,
        "position_quantity": 0.0,
        "position_avg_price": 0.0,
        "require_hard_stop": True,
        "gate": RiskGate(
            RiskLimits(
                max_position_value=50_000.0,
                max_gross_exposure=100_000.0,
                max_order_value=25_000.0,
                min_equity=1_000.0,
                max_risk_per_trade_pct=0.01,
                confidence_gate_enabled=False,
            )
        ),
        "max_quantity": 250.0,
        "commission_model": IbkrCommissionModel(),
    }
    values.update(overrides)
    return TradePlanEvaluationContext(**values)


def test_evaluate_long_bracket_computes_net_economics_and_stable_id() -> None:
    candidate = TradePlanCandidate(
        symbol="AAPL",
        direction="long",
        confidence=0.60,
        quantity=100.0,
        exit_plan={
            "hard_stop": 95.0,
            "take_profits": [{"price": 110.0, "fraction": 1.0}],
        },
    )
    evaluator = TradePlanEvaluator(_context())

    result = evaluator.evaluate(candidate)
    repeated = evaluator.evaluate(candidate)

    assert result.valid is True
    assert result.intent == "OPEN_LONG"
    assert result.order_quantity == 100.0
    assert result.risk_pct == 0.005
    assert result.economics.status == "positive"
    assert result.economics.cost_scope == "broker_commission_only"
    assert result.economics.cost_estimate_is_all_in is False
    assert result.economics.p_break_even is not None
    assert 0.33 < result.economics.p_break_even < 0.34
    assert result.evaluation_id == repeated.evaluation_id
    assert result.evaluation_id and result.evaluation_id.startswith("tpe_")
    assert any(
        warning["code"] == "transaction_cost_estimate_incomplete"
        and warning["cost_scope"] == "broker_commission_only"
        for warning in result.warnings
    )


@pytest.mark.parametrize(
    "amount",
    [float("nan"), float("inf"), float("-inf"), -0.01],
)
def test_economics_invalid_commission_amount_fail_closed(amount: float) -> None:
    class InvalidAmountModel:
        def calculate(self, order, price):
            return Commission(amount=amount, currency="USD", model="invalid_amount")

    result = TradePlanEvaluator(
        _context(commission_model=InvalidAmountModel())
    ).evaluate(
        TradePlanCandidate(
            symbol="AAPL",
            direction="long",
            confidence=0.60,
            quantity=100.0,
            exit_plan={
                "hard_stop": 95.0,
                "take_profits": [{"price": 110.0, "fraction": 1.0}],
            },
        )
    )

    assert result.valid is True
    assert result.economics.status == "unknown"
    assert {warning["code"] for warning in result.warnings} == {
        "commission_unavailable"
    }


def test_economics_third_commission_currency_fail_closed() -> None:
    class ThirdCurrencyModel:
        def calculate(self, order, price):
            return Commission(amount=1.0, currency="EUR", model="third_currency")

    result = TradePlanEvaluator(
        _context(commission_model=ThirdCurrencyModel())
    ).evaluate(
        TradePlanCandidate(
            symbol="AAPL",
            direction="long",
            confidence=0.60,
            quantity=100.0,
            exit_plan={
                "hard_stop": 95.0,
                "take_profits": [{"price": 110.0, "fraction": 1.0}],
            },
        )
    )

    assert result.valid is True
    assert result.economics.status == "unknown"
    assert {warning["code"] for warning in result.warnings} == {
        "commission_unavailable"
    }


def test_multi_target_fractions_fees_and_fx_use_actual_sizing() -> None:
    result = TradePlanEvaluator(_context(fx_rate=0.9)).evaluate(
        TradePlanCandidate(
            symbol="AAPL",
            direction="long",
            confidence=0.55,
            quantity=100.0,
            exit_plan={
                "hard_stop": 95.0,
                "take_profits": [
                    {"price": 105.0, "fraction": 0.5},
                    {"price": 115.0, "fraction": 0.5},
                ],
            },
        )
    )

    assert result.economics.gross_gain_usd == 900.0
    assert result.economics.gross_loss_usd == 450.0
    assert result.economics.fees_if_win_usd is not None
    assert result.economics.fees_if_win_usd > 0.0
    assert result.economics.expected_value_usd is not None


def test_non_usd_risk_pct_is_fx_aware_without_changing_sizing_or_gates() -> None:
    fx_rate = 0.03140506342913764
    equity = 99_672.76
    stop_distance_native = 20.0
    requested_risk_pct = 0.003
    result = TradePlanEvaluator(
        _context(
            price=1_070.0,
            fx_rate=fx_rate,
            equity=equity,
            max_quantity=2_000.0,
        )
    ).evaluate(
        TradePlanCandidate(
            symbol="2404.TW",
            direction="long",
            confidence=0.70,
            risk_pct=requested_risk_pct,
            exit_plan={
                "hard_stop": 1_050.0,
                "take_profits": [{"price": 1_110.0, "fraction": 1.0}],
            },
        )
    )

    expected_quantity = requested_risk_pct * equity / (stop_distance_native * fx_rate)
    expected_max_risk_quantity = 0.01 * equity / (stop_distance_native * fx_rate)
    assert result.order_quantity == pytest.approx(expected_quantity)
    assert result.max_risk_quantity == pytest.approx(expected_max_risk_quantity)
    # Omitting FX here produced 0.095526 (9.55%) instead of the requested 0.30%.
    assert result.risk_pct == pytest.approx(0.003)
    assert result.risk_approved is True
    assert result.final_gate_approved is True
    assert result.executable_by_gates is True


def test_evaluate_short_risk_sizing_resolves_quantity_and_r_multiple() -> None:
    candidate = TradePlanCandidate(
        symbol="AAPL",
        direction="short",
        confidence=0.55,
        risk_pct=0.005,
        exit_plan={
            "hard_stop": {"type": "percent", "percent": 0.05},
            "take_profits": [
                {"type": "risk_multiple", "r": 2.0, "fraction": 1.0}
            ],
        },
    )

    result = TradePlanEvaluator(_context()).evaluate(candidate)

    assert result.valid is True
    assert result.intent == "OPEN_SHORT"
    assert result.order_quantity == 100.0
    assert result.resolved_exit_plan["hard_stop"]["price"] == 105.0
    assert result.resolved_exit_plan["take_profits"][0]["price"] == 90.0
    assert result.economics.status == "positive"


def test_evaluate_incomplete_targets_returns_unknown_economics() -> None:
    result = TradePlanEvaluator(_context()).evaluate(
        TradePlanCandidate(
            symbol="AAPL",
            direction="long",
            confidence=0.8,
            quantity=10.0,
            exit_plan={
                "hard_stop": 95.0,
                "take_profits": [{"price": 110.0, "fraction": 0.5}],
            },
        )
    )

    assert result.valid is True
    assert result.economics.status == "unknown"
    assert any(
        warning["code"] == "take_profit_coverage_incomplete"
        for warning in result.warnings
    )


def test_evaluate_rejects_wrong_side_stop() -> None:
    result = TradePlanEvaluator(_context()).evaluate(
        TradePlanCandidate(
            symbol="AAPL",
            direction="long",
            confidence=0.8,
            quantity=10.0,
            exit_plan={"hard_stop": 105.0},
        )
    )

    assert result.valid is False
    assert result.reasons == ("hard_stop_wrong_side",)
    assert result.evaluation_id is None


def test_evaluation_id_changes_with_confidence_or_cycle() -> None:
    candidate = TradePlanCandidate(
        symbol="AAPL",
        direction="long",
        confidence=0.6,
        quantity=10.0,
        exit_plan={
            "hard_stop": 95.0,
            "take_profits": [{"price": 110.0, "fraction": 1.0}],
        },
    )
    base = TradePlanEvaluator(_context()).evaluate(candidate)
    changed_confidence = TradePlanEvaluator(_context()).evaluate(
        TradePlanCandidate(**{**candidate.__dict__, "confidence": 0.61})
    )
    changed_cycle = TradePlanEvaluator(
        _context(cycle_id="2026-08-18T03:01:00+00:00")
    ).evaluate(candidate)

    assert base.evaluation_id != changed_confidence.evaluation_id
    assert base.evaluation_id != changed_cycle.evaluation_id


def test_evaluate_trade_plan_tool_returns_compact_evaluation() -> None:
    context = ToolContext(
        now=datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc),
        allowed_symbols=frozenset({"AAPL"}),
        trade_plan_evaluator=TradePlanEvaluator(_context()),
    )

    result, trace = agent_tools.execute_tool_call(
        AgentToolCall(
            id="eval-1",
            tool="evaluate_trade_plan",
            args={
                "symbol": "AAPL",
                "direction": "long",
                "confidence": 0.6,
                "qty": 100,
                "exit": {"stop": 95, "limit": 110},
            },
        ),
        context,
    )

    assert trace.outcome == "ok"
    assert result.ok is True
    assert result.result["valid"] is True
    assert result.result["evaluation_id"].startswith("tpe_")
    assert result.result["economics"]["p_break_even"] is not None
    assert result.result["economics"]["cost_scope"] == "broker_commission_only"
    assert result.result["economics"]["cost_estimate_is_all_in"] is False


def test_tool_evaluation_matches_parsed_limit_entry_and_normalized_thesis() -> None:
    evaluator = TradePlanEvaluator(_context())
    context = ToolContext(
        now=datetime(2026, 8, 18, 3, 0, tzinfo=timezone.utc),
        allowed_symbols=frozenset({"AAPL"}),
        trade_plan_evaluator=evaluator,
    )
    args = {
        "symbol": "AAPL",
        "direction": "long",
        "confidence": 0.6,
        "qty": 10,
        "exit": {"stop": 95.0, "limit": 110.0},
        "thesis": {
            "setup": "  breakout  ",
            "horizon": "SWING",
            "invalidation": "  range failure  ",
        },
    }
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="eval-limit", tool="evaluate_trade_plan", args=args),
        context=context,
    )
    payload = json.dumps(
        {
            "decisions": [
                {
                    "symbol": "AAPL",
                    "confidence": 0.6,
                    "rationale": "same plan",
                    "calls": [
                        {
                            "tool": "strategy_entry",
                            "args": {
                                key: value
                                for key, value in args.items()
                                if key != "symbol"
                            }
                            | {"evaluation_id": result.result["evaluation_id"]},
                        }
                    ],
                }
            ]
        }
    )

    decision = parsing.parse_batch(
        payload,
        ["AAPL"],
        allow_context_request=False,
    )["AAPL"]

    assert validate_trade_evaluation_reference(decision, evaluator).approved


def test_public_armed_strategy_entry_reference_is_normalized_before_validation() -> None:
    evaluator = TradePlanEvaluator(_context())
    candidate = TradePlanCandidate(
        symbol="AAPL",
        direction="long",
        confidence=0.6,
        quantity=10,
        exit_plan={
            "hard_stop": 95.0,
            "take_profits": [
                {
                    "type": "price",
                    "price": 110.0,
                    "fraction": 1.0,
                }
            ],
        },
        thesis={
            "setup": "breakout",
            "horizon": "swing",
            "invalidation": "range failure",
        },
    )
    issued = evaluator.evaluate(candidate)
    decision = replace(
        Decision.hold("AAPL", "armed"),
        indicator_watch={
            "on_trigger": "EXECUTE_ORDER",
            "order": {
                "tool": "strategy_entry",
                "args": {
                    "direction": "long",
                    "qty": 10,
                    "confidence": 0.6,
                    "exit": {"stop": 95.0, "limit": 110.0},
                    "thesis": {
                        "setup": "  breakout  ",
                        "horizon": "SWING",
                        "invalidation": "  range failure  ",
                    },
                    "evaluation_id": issued.evaluation_id,
                },
            },
        },
    )

    assert validate_decision_trade_evaluations(decision, evaluator).approved


def test_flip_target_quantity_includes_existing_position_once() -> None:
    evaluator = TradePlanEvaluator(
        _context(position_quantity=5.0, position_avg_price=98.0)
    )
    candidate = TradePlanCandidate(
        symbol="AAPL",
        direction="short",
        confidence=0.6,
        quantity=3.0,
        exit_plan={"hard_stop": 105.0},
    )
    issued = evaluator.evaluate(candidate)
    decision = Decision(
        symbol="AAPL",
        action="SELL",
        quantity=3.0,
        confidence=0.6,
        rationale="flip to short three",
        intent="OPEN_SHORT",
        resolve_from_position=True,
        exit_plan={"hard_stop": 105.0},
        trade_evaluation_id=issued.evaluation_id,
    )

    assert issued.intent == "FLIP"
    assert issued.order_quantity == 8.0
    assert validate_trade_evaluation_reference(decision, evaluator).approved


def test_unresolved_relative_flip_cannot_bypass_evaluation() -> None:
    evaluator = TradePlanEvaluator(
        _context(position_quantity=5.0, position_avg_price=98.0)
    )
    candidate = TradePlanCandidate(
        symbol="AAPL",
        direction="short",
        confidence=0.6,
        quantity=3.0,
        exit_plan={"hard_stop": 105.0},
    )
    issued = evaluator.evaluate(candidate)
    pending = Decision(
        symbol="AAPL",
        action="HOLD",
        quantity=3.0,
        confidence=0.6,
        rationale="relative flip",
        intent="FLIP",
        resolve_from_position=True,
        exit_plan={"hard_stop": 105.0},
    )

    missing = validate_trade_evaluation_reference(pending, evaluator)
    approved = validate_trade_evaluation_reference(
        replace(pending, trade_evaluation_id=issued.evaluation_id),
        evaluator,
    )

    assert missing.reason == "trade_evaluation_required"
    assert approved.approved


def test_evaluate_trade_plan_tool_rejects_ambiguous_sizing() -> None:
    trace = validate_tool_call(
        {
            "id": "eval-1",
            "tool": "evaluate_trade_plan",
            "args": {
                "symbol": "AAPL",
                "direction": "long",
                "confidence": 0.6,
                "qty": 10,
                "risk_pct": 0.005,
            },
        },
        allowed_tools=frozenset({"evaluate_trade_plan"}),
    )

    assert trace.outcome == "rejected"
    assert trace.detail["reason"] == "invalid_args"


def test_strategy_entry_parser_preserves_evaluation_reference() -> None:
    payload = json.dumps(
        {
            "decisions": [
                {
                    "symbol": "SPY",
                    "confidence": 0.8,
                    "rationale": "evaluated entry",
                    "decision_reason_code": "ENTRY_SIGNAL",
                    "calls": [
                        {
                            "tool": "strategy_entry",
                            "args": {
                                "direction": "long",
                                "qty": 2,
                                "evaluation_id": "tpe_parser",
                            },
                        }
                    ],
                }
            ]
        }
    )

    decision = parsing.parse_batch(
        payload,
        ["SPY"],
        allow_context_request=False,
    )["SPY"]

    assert decision.trade_evaluation_id == "tpe_parser"


def test_reference_distinguishes_modified_candidate_from_stale_cycle() -> None:
    evaluator = TradePlanEvaluator(_context())
    candidate = TradePlanCandidate(
        symbol="AAPL",
        direction="long",
        confidence=0.6,
        quantity=10,
        exit_plan={
            "hard_stop": 95.0,
            "take_profits": [{"price": 110.0, "fraction": 1.0}],
        },
    )
    issued = evaluator.evaluate(candidate)
    modified = Decision(
        symbol="AAPL",
        action="BUY",
        quantity=11,
        confidence=0.6,
        rationale="modified",
        intent="OPEN_LONG",
        exit_plan=candidate.exit_plan,
        trade_evaluation_id=issued.evaluation_id,
    )

    mismatch = validate_trade_evaluation_reference(modified, evaluator)
    stale = validate_trade_evaluation_reference(
        modified,
        TradePlanEvaluator(_context(cycle_id="replacement-cycle")),
    )

    assert mismatch.reason == "trade_evaluation_mismatch"
    assert stale.reason == "trade_evaluation_stale"
