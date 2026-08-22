from __future__ import annotations

import pytest

from trader.application.world_model.impact import evaluate_world_shadow_impact


def _prediction(
    prediction_id: str,
    episode_id: str,
    *,
    probabilities: dict[str, float],
    created_at: str,
    horizon_id: str = "elapsed_4h.v1",
) -> dict[str, object]:
    return {
        "prediction_id": prediction_id,
        "episode_id": episode_id,
        "horizon_id": horizon_id,
        "model_id": "small-gru-shadow",
        "model_version": "v1",
        "probabilities": probabilities,
        "created_at": created_at,
        "ready_at": created_at,
        "authority": "shadow_only",
        "decision_effect": "none",
    }


def _outcome(
    episode_id: str,
    *,
    direction: str,
    simple_return: float,
    available_at: str,
    horizon_id: str = "elapsed_4h.v1",
) -> dict[str, object]:
    return {
        "event_id": f"outcome:{episode_id}:{horizon_id}",
        "episode_id": episode_id,
        "horizon_id": horizon_id,
        "status": "observed",
        "direction": direction,
        "simple_return": simple_return,
        "training_eligible": True,
        "available_at": available_at,
    }


def _cycle(
    decision_id: str,
    *,
    cycle_id: str = "SPY:1",
    side: str = "LONG",
    pnl: float = 12.0,
    gross_pnl: float = 15.0,
    exit_ts: str = "2026-01-01T14:00:00+00:00",
) -> dict[str, object]:
    return {
        "symbol": "SPY",
        "position_cycle_id": cycle_id,
        "position_cycle_closed": True,
        "entry_decision_ids": [decision_id],
        "side": side,
        "pnl": pnl,
        "gross_pnl": gross_pnl,
        "commission_quality": {"status": "available"},
        "exit_ts": exit_ts,
    }


def test_market_metrics_are_prequential_and_drawdown_is_explicit_proxy() -> None:
    predictions = [
        _prediction(
            "p1",
            "e1",
            probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            created_at="2026-01-01T10:00:00+00:00",
        ),
        _prediction(
            "p2",
            "e2",
            probabilities={"DOWN": 0.8, "FLAT": 0.1, "UP": 0.1},
            created_at="2026-01-01T10:05:00+00:00",
        ),
        _prediction(
            "p3",
            "e3",
            probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            created_at="2026-01-01T10:10:00+00:00",
        ),
    ]
    outcomes = [
        _outcome("e1", direction="UP", simple_return=0.10, available_at="2026-01-01T14:00:00+00:00"),
        _outcome("e2", direction="UP", simple_return=0.20, available_at="2026-01-01T14:05:00+00:00"),
        _outcome("e3", direction="UP", simple_return=0.05, available_at="2026-01-01T14:10:00+00:00"),
    ]

    result = evaluate_world_shadow_impact(
        predictions,
        outcomes,
        minimum_market_samples=1,
    )

    group = result["market"]["groups"][0]
    assert result["authority"] == "shadow_only"
    assert result["decision_effect"] == "none"
    assert group["status"] == "ready"
    assert group["calibration"]["accuracy"] == pytest.approx(2 / 3)
    assert group["direction"]["directional_accuracy"] == pytest.approx(2 / 3)
    assert group["directional_market_proxy"] == {
        "status": "available",
        "observations": 3,
        "missing_returns": 0,
        "gross_return_sum": -0.05,
        "mean_return": pytest.approx(-0.05 / 3),
        "max_drawdown": 0.2,
        "unit": "simple_return_per_one_unit_notional_forecast",
        "costs": "excluded",
    }
    assert "not a tradable portfolio equity curve" in result["market"]["drawdown_semantics"]


def test_market_metrics_do_not_score_prediction_created_after_its_label() -> None:
    result = evaluate_world_shadow_impact(
        [
            _prediction(
                "p1",
                "e1",
                probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
                created_at="2026-01-01T15:00:00+00:00",
            )
        ],
        [_outcome("e1", direction="UP", simple_return=0.01, available_at="2026-01-01T14:00:00+00:00")],
    )

    assert result["market"]["status"] == "insufficient"
    assert result["market"]["groups"] == []
    assert result["market"]["excluded"] == {"causal_order_unproven": 1}


@pytest.mark.parametrize(
    ("field", "timestamp"),
    [
        ("created_at", "2026-01-01T14:00:00+00:00"),
        ("ready_at", "2026-01-01T14:00:00+00:00"),
    ],
)
def test_market_metrics_require_both_causal_clocks_strictly_before_label(
    field: str,
    timestamp: str,
) -> None:
    prediction = _prediction(
        "p1",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    prediction[field] = timestamp

    result = evaluate_world_shadow_impact(
        [prediction],
        [_outcome("e1", direction="UP", simple_return=0.01, available_at=timestamp)],
    )

    assert result["market"]["status"] == "insufficient"
    assert result["market"]["matched"] == 0
    assert result["market"]["excluded"] == {"causal_order_unproven": 1}


def test_market_prediction_dedup_is_fail_closed_and_order_independent() -> None:
    replay = _prediction(
        "immutable-prediction",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    replay_copy = _prediction(
        "immutable-prediction",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    outcome = _outcome(
        "e1",
        direction="UP",
        simple_return=0.01,
        available_at="2026-01-01T14:00:00+00:00",
    )

    forward_replay = evaluate_world_shadow_impact(
        [replay, replay_copy],
        [outcome],
        minimum_market_samples=1,
    )
    reversed_replay = evaluate_world_shadow_impact(
        [replay_copy, replay],
        [outcome],
        minimum_market_samples=1,
    )

    assert forward_replay == reversed_replay
    assert forward_replay["market"]["matched"] == 1
    assert forward_replay["market"]["excluded"] == {"duplicate_prediction_replay": 1}

    divergent = _prediction(
        "different-prediction",
        "e1",
        probabilities={"DOWN": 0.8, "FLAT": 0.1, "UP": 0.1},
        created_at="2026-01-01T10:00:00+00:00",
    )
    forward_ambiguous = evaluate_world_shadow_impact(
        [replay, divergent],
        [outcome],
        minimum_market_samples=1,
    )
    reversed_ambiguous = evaluate_world_shadow_impact(
        [divergent, replay],
        [outcome],
        minimum_market_samples=1,
    )

    assert forward_ambiguous == reversed_ambiguous
    assert forward_ambiguous["market"]["status"] == "insufficient"
    assert forward_ambiguous["market"]["matched"] == 0
    assert forward_ambiguous["market"]["excluded"] == {"ambiguous_prediction_identity": 2}


def test_market_proxy_orders_by_ready_at_not_logical_cutoff() -> None:
    predictions = [
        _prediction(
            "p1",
            "e1",
            probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            created_at="2026-01-01T10:00:00+00:00",
        ),
        _prediction(
            "p2",
            "e2",
            probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            created_at="2026-01-01T10:05:00+00:00",
        ),
        _prediction(
            "p3",
            "e3",
            probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            created_at="2026-01-01T10:10:00+00:00",
        ),
    ]
    predictions[0]["ready_at"] = "2026-01-01T10:15:00+00:00"
    predictions[1]["ready_at"] = "2026-01-01T10:10:00+00:00"
    predictions[2]["ready_at"] = "2026-01-01T10:20:00+00:00"
    outcomes = [
        _outcome("e1", direction="UP", simple_return=0.2, available_at="2026-01-01T14:00:00+00:00"),
        _outcome("e2", direction="UP", simple_return=-0.1, available_at="2026-01-01T14:00:00+00:00"),
        _outcome("e3", direction="UP", simple_return=-0.1, available_at="2026-01-01T14:00:00+00:00"),
    ]

    forward = evaluate_world_shadow_impact(predictions, outcomes, minimum_market_samples=1)
    reversed_input = evaluate_world_shadow_impact(
        list(reversed(predictions)),
        list(reversed(outcomes)),
        minimum_market_samples=1,
    )

    assert forward == reversed_input
    assert forward["market"]["groups"][0]["directional_market_proxy"]["max_drawdown"] == 0.1


def test_trader_result_is_non_joinable_without_durable_predecision_link() -> None:
    prediction = _prediction(
        "p1",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    result = evaluate_world_shadow_impact(
        [prediction],
        [],
        decision_rows=[
            {
                "decision_id": "d1",
                "cycle_ts": "2026-01-01T10:05:00+00:00",
                "decision_dispatch_at": "2026-01-01T10:05:00+00:00",
                "decision_source": "llm",
            }
        ],
        position_cycles=[_cycle("d1")],
    )

    assert result["trader"]["status"] == "non_joinable"
    assert result["trader"]["reason"] == "missing_durable_predecision_prediction_link"
    assert result["actual_contribution"] == {
        "status": "not_attributable",
        "reason": "world_model_decision_effect_none",
        "value": None,
        "claim": "No causal Trader contribution is inferred from a shadow forecast.",
    }
    assert result["counterfactual_contribution"]["status"] == "not_available"


def test_durable_link_yields_descriptive_cycle_metrics_not_causal_contribution() -> None:
    prediction = _prediction(
        "p1",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    result = evaluate_world_shadow_impact(
        [prediction],
        [],
        decision_rows=[
            {
                "decision_id": "d1",
                "cycle_ts": "2026-01-01T10:05:00+00:00",
                "decision_dispatch_at": "2026-01-01T10:05:00+00:00",
                "decision_source": "llm",
            }
        ],
        decision_links=[
            {
                "prediction_id": "p1",
                "decision_id": "d1",
                "link_kind": "pre_decision_reference",
                "linked_at": "2026-01-01T10:02:00+00:00",
            }
        ],
        position_cycles=[_cycle("d1")],
        minimum_trader_cycles=1,
    )

    assert result["trader"]["status"] == "descriptive_only"
    group = result["trader"]["groups"][0]
    assert group["status"] == "descriptive_only"
    assert group["claim"] == "association_only_under_existing_trader_policy"
    assert group["net_pnl"] == 12.0
    assert group["gross_pnl"] == 15.0
    assert group["direction_alignment"] == {
        "aligned": 1,
        "opposed": 0,
        "neutral_or_unactionable": 0,
    }
    assert result["actual_contribution"]["status"] == "not_attributable"


def test_cycle_join_fails_closed_for_partial_or_fee_incomplete_cycle() -> None:
    prediction = _prediction(
        "p1",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    partial = _cycle("d1")
    partial["position_cycle_closed"] = False
    fee_incomplete = _cycle("d1", cycle_id="SPY:2")
    fee_incomplete["commission_quality"] = {"status": "unavailable"}
    fee_incomplete["pnl"] = None
    result = evaluate_world_shadow_impact(
        [prediction],
        [],
        decision_rows=[
            {
                "decision_id": "d1",
                "cycle_ts": "2026-01-01T10:05:00+00:00",
                "decision_dispatch_at": "2026-01-01T10:05:00+00:00",
            }
        ],
        decision_links=[
            {
                "prediction_id": "p1",
                "decision_id": "d1",
                "link_kind": "pre_decision_reference",
                "linked_at": "2026-01-01T10:02:00+00:00",
            }
        ],
        position_cycles=[partial, fee_incomplete],
    )

    assert result["trader"]["status"] == "non_joinable"
    assert result["trader"]["join_excluded"] == {
        "cycle_net_economics_unavailable": 1,
        "cycle_not_flat_to_flat_completed": 1,
    }


def test_logical_snapshot_time_cannot_fake_predecision_wall_clock_availability() -> None:
    prediction = _prediction(
        "p1",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    prediction["ready_at"] = "2026-01-01T10:06:00+00:00"

    result = evaluate_world_shadow_impact(
        [prediction],
        [],
        decision_rows=[
            {
                "decision_id": "d1",
                "cycle_ts": "2026-01-01T10:05:00+00:00",
                "decision_dispatch_at": "2026-01-01T10:05:00+00:00",
            }
        ],
        decision_links=[
            {
                "prediction_id": "p1",
                "decision_id": "d1",
                "link_kind": "pre_decision_reference",
                "linked_at": "2026-01-01T10:04:00+00:00",
            }
        ],
        position_cycles=[_cycle("d1")],
        minimum_trader_cycles=1,
    )

    assert result["trader"]["status"] == "non_joinable"
    assert result["trader"]["join_excluded"] == {"link_before_prediction_ready": 1}


@pytest.mark.parametrize(
    ("ready_at", "linked_at", "decision_dispatch_at", "expected_exclusion"),
    [
        (
            "2026-01-01T10:02:00+00:00",
            "2026-01-01T10:02:00+00:00",
            "2026-01-01T10:05:00+00:00",
            "link_before_prediction_ready",
        ),
        (
            "2026-01-01T10:01:00+00:00",
            "2026-01-01T10:05:00+00:00",
            "2026-01-01T10:05:00+00:00",
            "link_after_decision",
        ),
    ],
)
def test_predecision_link_clocks_are_strict(
    ready_at: str,
    linked_at: str,
    decision_dispatch_at: str,
    expected_exclusion: str,
) -> None:
    prediction = _prediction(
        "p1",
        "e1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        created_at="2026-01-01T10:00:00+00:00",
    )
    prediction["ready_at"] = ready_at

    result = evaluate_world_shadow_impact(
        [prediction],
        [],
        decision_rows=[
            {
                "decision_id": "d1",
                "decision_dispatch_at": decision_dispatch_at,
            }
        ],
        decision_links=[
            {
                "prediction_id": "p1",
                "decision_id": "d1",
                "link_kind": "pre_decision_reference",
                "linked_at": linked_at,
            }
        ],
        position_cycles=[_cycle("d1")],
        minimum_trader_cycles=1,
    )

    assert result["trader"]["status"] == "non_joinable"
    assert result["trader"]["join_excluded"] == {expected_exclusion: 1}


def test_predecision_link_group_rejects_multiple_predictions_order_independently() -> None:
    predictions = [
        _prediction(
            "p1",
            "e1",
            probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            created_at="2026-01-01T10:00:00+00:00",
        ),
        _prediction(
            "p2",
            "e2",
            probabilities={"DOWN": 0.8, "FLAT": 0.1, "UP": 0.1},
            created_at="2026-01-01T10:00:00+00:00",
        ),
    ]
    decision_rows = [
        {
            "decision_id": "d1",
            "decision_dispatch_at": "2026-01-01T10:05:00+00:00",
        }
    ]
    links = [
        {
            "prediction_id": "p1",
            "decision_id": "d1",
            "link_kind": "pre_decision_reference",
            "linked_at": "2026-01-01T10:02:00+00:00",
        },
        {
            "prediction_id": "p2",
            "decision_id": "d1",
            "link_kind": "pre_decision_reference",
            "linked_at": "2026-01-01T10:03:00+00:00",
        },
    ]

    forward = evaluate_world_shadow_impact(
        predictions,
        [],
        decision_rows=decision_rows,
        decision_links=links,
        position_cycles=[_cycle("d1")],
        minimum_trader_cycles=1,
    )
    reversed_input = evaluate_world_shadow_impact(
        list(reversed(predictions)),
        [],
        decision_rows=list(reversed(decision_rows)),
        decision_links=list(reversed(links)),
        position_cycles=[_cycle("d1")],
        minimum_trader_cycles=1,
    )

    assert forward == reversed_input
    assert forward["trader"]["status"] == "non_joinable"
    assert forward["trader"]["join_excluded"] == {"ambiguous_prediction_decision_link": 2}
