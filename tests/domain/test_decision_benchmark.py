from __future__ import annotations

import math

import pytest

from trader.domain import decision_benchmark
from trader.reporting.audit import decision_quality


@pytest.mark.parametrize(
    ("action", "future_return", "expected"),
    [
        ("BUY", 0.5, "good"),
        ("BUY", -0.5, "bad"),
        ("BUY", 0.499999, "neutral"),
        ("BUY", -0.499999, "neutral"),
        ("SELL", -0.5, "good"),
        ("SELL", 0.5, "bad"),
        ("SELL", 0.499999, "neutral"),
        ("SELL", -0.499999, "neutral"),
    ],
)
def test_direct_actions_use_inclusive_thresholds(
    action: str,
    future_return: float,
    expected: str,
) -> None:
    verdict, context = decision_benchmark.decision_verdict(
        {"action": f" {action.lower()} "},
        future_return,
        0.5,
    )

    assert verdict == expected
    assert context == {
        "benchmark_basis": "direct_action",
        "effective_action": action,
        "intended_action": action,
        "scoreability_reason": None,
    }


@pytest.mark.parametrize(
    ("side", "future_return", "expected"),
    [
        ("long", 0.5, "missed"),
        ("long", -0.5, "good"),
        ("short", -0.5, "missed"),
        ("short", 0.5, "good"),
        ("long", 0.499999, "good"),
        ("short", -0.499999, "good"),
    ],
)
def test_directional_hold_scores_only_the_declared_opportunity(
    side: str,
    future_return: float,
    expected: str,
) -> None:
    verdict, context = decision_benchmark.decision_verdict(
        {"action": "HOLD", "opportunity_side": side},
        future_return,
        0.5,
    )

    expected_action = "BUY" if side == "long" else "SELL"
    assert verdict == expected
    assert context["benchmark_basis"] == f"intended_{side}"
    assert context["effective_action"] is None
    assert context["intended_action"] == expected_action


def test_nested_opportunity_side_is_supported_for_preserved_ledger_decision() -> None:
    verdict, context = decision_benchmark.decision_verdict(
        {"action": "HOLD", "decision": {"opportunity_side": "short"}},
        -0.5,
        0.5,
    )

    assert verdict == "missed"
    assert context["benchmark_basis"] == "intended_short"


@pytest.mark.parametrize(
    ("future_return", "expected"),
    [(0.499999, "good"), (-0.499999, "good"), (0.5, "unknown"), (-0.5, "unknown")],
)
def test_directionless_hold_is_good_only_inside_the_quiet_band(
    future_return: float,
    expected: str,
) -> None:
    verdict, context = decision_benchmark.decision_verdict(
        {"action": "HOLD"},
        future_return,
        0.5,
    )

    assert verdict == expected
    assert context["benchmark_basis"] == "directionless_hold"
    assert context["scoreability_reason"] == "direction_unknown"


@pytest.mark.parametrize(
    ("quantity", "future_return", "expected_basis", "expected_verdict"),
    [
        (10.0, 0.5, "position_long", "good"),
        (10.0, -0.5, "position_long", "bad"),
        (-10.0, -0.5, "position_short", "good"),
        (-10.0, 0.5, "position_short", "bad"),
    ],
)
def test_hold_of_existing_position_scores_retained_exposure(
    quantity: float,
    future_return: float,
    expected_basis: str,
    expected_verdict: str,
) -> None:
    row = {
        "symbol": "SPY",
        "action": "HOLD",
        "opportunity_side": "short" if quantity > 0 else "long",
        "portfolio_snapshot": {
            "holdings": [
                {"symbol": "QQQ", "quantity": 999},
                {"symbol": "spy", "quantity": quantity / 2},
                {"symbol": "SPY", "quantity": quantity / 2},
            ]
        },
    }

    verdict, context = decision_benchmark.decision_verdict(row, future_return, 0.5)

    assert verdict == expected_verdict
    assert context["benchmark_basis"] == expected_basis
    assert context["effective_action"] == ("BUY" if quantity > 0 else "SELL")
    assert context["intended_action"] is None


@pytest.mark.parametrize(
    ("order_container", "expected_action"),
    [
        ({"runtime": {"indicator_watch_order": {"action": "SELL"}}}, "SELL"),
        ({"runtime": {"armed_plan_order": {"intent": "OPEN_LONG"}}}, "BUY"),
        (
            {
                "decision": {
                    "indicator_watch": {
                        "order": {"action": "HOLD", "intent": "OPEN_SHORT"}
                    }
                }
            },
            "SELL",
        ),
    ],
)
def test_conditional_orders_are_directional_but_not_scored_without_trigger_result(
    order_container: dict,
    expected_action: str,
) -> None:
    row = {"action": "HOLD", "opportunity_side": "long", **order_container}

    verdict, context = decision_benchmark.decision_verdict(row, 2.0, 0.5)

    assert verdict == "unknown"
    assert context == {
        "benchmark_basis": "conditional_order",
        "effective_action": None,
        "intended_action": expected_action,
        "scoreability_reason": "conditional_trigger_not_evaluated",
    }


@pytest.mark.parametrize(
    ("row", "expected_reason"),
    [
        ({"action": "HOLD", "decision_reason_code": "DATA_STALE"}, "data_stale"),
        ({"action": "HOLD", "rationale": "market closed"}, "market_closed"),
    ],
)
def test_non_executable_holds_keep_their_reason_even_without_future_price(
    row: dict,
    expected_reason: str,
) -> None:
    verdict, context = decision_benchmark.decision_verdict(row, None, 0.5)

    assert verdict == "unknown"
    assert context == {
        "benchmark_basis": "not_executable",
        "effective_action": None,
        "intended_action": None,
        "scoreability_reason": expected_reason,
    }


@pytest.mark.parametrize(
    "machine_fields",
    [
        {"decision_source": " INFRA "},
        {"decision": {"decision_source": "infra_hold"}},
        {"reason": " QUIET_GATE "},
        {"reason": "stale_market_data"},
        {"llm_error": "timeout"},
        {"decision": {"llm_error": "bad_output"}},
    ],
)
def test_synthetic_decisions_are_machine_regardless_of_action_or_future(
    machine_fields: dict,
) -> None:
    verdict, context = decision_benchmark.decision_verdict(
        {"action": "BUY", **machine_fields},
        None,
        0.5,
    )

    assert verdict == "machine"
    assert context["benchmark_basis"] == "infra"
    assert context["scoreability_reason"] == "not_agent_decision"


@pytest.mark.parametrize("future_return", [math.nan, math.inf, -math.inf])
def test_non_finite_future_return_is_unknown(future_return: float) -> None:
    verdict, context = decision_benchmark.decision_verdict(
        {"action": "BUY"},
        future_return,
        0.5,
    )

    assert verdict == "unknown"
    assert context["scoreability_reason"] == "future_return_invalid"


@pytest.mark.parametrize("threshold", [0.0, -0.5, math.nan, math.inf])
def test_invalid_threshold_never_produces_a_quality_label(threshold: float) -> None:
    verdict, context = decision_benchmark.decision_verdict(
        {"action": "BUY"},
        2.0,
        threshold,
    )

    assert verdict == "unknown"
    assert context["scoreability_reason"] == "invalid_threshold"


def test_reporting_reexports_the_exact_domain_benchmark_contract() -> None:
    assert decision_quality.BENCHMARK_SEMANTICS_VERSION == (
        decision_benchmark.BENCHMARK_SEMANTICS_VERSION
    )
    assert decision_quality.decision_benchmark_context is (
        decision_benchmark.decision_benchmark_context
    )
    assert decision_quality.decision_verdict is decision_benchmark.decision_verdict
