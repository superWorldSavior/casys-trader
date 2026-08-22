from __future__ import annotations

import json
from pathlib import Path

from scripts import brain_flair_ablation as ablation


def _row(
    index: int,
    *,
    verdict: str | None,
    direction: str = "long",
    cycle: str | None = None,
    action: str = "HOLD",
    rationale: str | None = None,
    thesis: dict | None = None,
) -> dict:
    outcome = {"verdict": verdict, "outcome_score": 0.1} if verdict is not None else {"status": "pending"}
    return {
        "schema_version": "brain_trajectory_spike_v1",
        "identity": {
            "task_id": index,
            "cycle_id": cycle or f"2026-08-{index + 1:02d}T10:00:00+00:00",
            "symbol": f"S{index % 3}",
            "decision_id": f"d{index}",
        },
        "state_before": {
            "shared_projection": {
                "portfolio": {"equity": 100_000.0, "gross_exposure_usd": 50_000.0},
                "stale_market_data": {},
            },
            "per_symbol_facts": {
                "structure": {"atr_pct_14": 0.02, "relative_volume_20": 1.0},
                "universe_mandate": {
                    "mandate_ref": {"status": "active"},
                    "symbol_mandate": {
                        "directional_view": direction,
                        "allowed_sides": [direction],
                        "role": "candidate",
                        "posture": "engage",
                        "confidence": 0.8,
                        "family_context": {"family": "energy", "posture": "constructive"},
                    },
                },
            },
        },
        "decision": {
            "decision_id": f"d{index}",
            "action": action,
            "intent": action,
            "confidence": 0.75,
            "decision_reason_code": "SETUP",
            "rationale": rationale,
            "thesis": thesis,
            "domain_tools": {
                "tool_calls": [
                    {"tool": "get_indicator_context", "outcome": "ok"},
                    {"tool": "evaluate_trade_plan", "outcome": "valid"},
                ]
            },
        },
        "learning_outcome": outcome,
    }


def test_chronological_split_keeps_cycles_wholly_on_one_side() -> None:
    rows = [
        _row(0, verdict="WIN", cycle="2026-08-01T10:00:00Z"),
        _row(1, verdict="LOSS", cycle="2026-08-01T10:00:00Z"),
        _row(2, verdict="WIN", cycle="2026-08-02T10:00:00Z"),
        _row(3, verdict="LOSS", cycle="2026-08-03T10:00:00Z"),
        _row(4, verdict="WIN", cycle="2026-08-03T10:00:00Z"),
    ]
    examples, _coverage = ablation.prepare_examples(rows)

    train, test, split = ablation.chronological_group_split(examples, test_fraction=0.4)

    train_cycles = {row.cycle_id for row in train}
    test_cycles = {row.cycle_id for row in test}
    assert train_cycles.isdisjoint(test_cycles)
    assert split["cycle_overlap"] == []
    assert max(train_cycles) < min(test_cycles)
    assert {row.ordinal for row in test} == {3, 4}


def test_split_embargoes_train_rows_inside_counterfactual_horizon() -> None:
    rows = [
        _row(0, verdict="WIN", cycle="2026-08-01T00:00:00Z"),
        _row(1, verdict="LOSS", cycle="2026-08-02T09:00:00Z"),
        _row(2, verdict="WIN", cycle="2026-08-02T20:00:00Z"),
        _row(3, verdict="LOSS", cycle="2026-08-03T10:00:00Z"),
        _row(4, verdict="WIN", cycle="2026-08-04T10:00:00Z"),
    ]
    examples, _coverage = ablation.prepare_examples(rows)

    train, test, split = ablation.chronological_group_split(
        examples,
        test_fraction=0.4,
        embargo_hours=24.0,
    )

    assert {row.ordinal for row in train} == {0, 1}
    assert {row.ordinal for row in test} == {3, 4}
    assert split["embargo_cutoff"] == "2026-08-02T10:00:00+00:00"
    assert split["purged_train_rows"] == {"within_counterfactual_embargo": 1}


def test_split_purges_counterfactual_label_at_exact_embargo_boundary() -> None:
    rows = [
        _row(0, verdict="WIN", cycle="2026-08-01T09:59:59Z"),
        _row(1, verdict="LOSS", cycle="2026-08-02T10:00:00Z"),
        _row(2, verdict="WIN", cycle="2026-08-03T10:00:00Z"),
        _row(3, verdict="LOSS", cycle="2026-08-04T10:00:00Z"),
    ]
    examples, _coverage = ablation.prepare_examples(rows)

    train, test, split = ablation.chronological_group_split(
        examples,
        test_fraction=0.5,
        embargo_hours=24.0,
    )

    assert {row.ordinal for row in train} == {0}
    assert {row.ordinal for row in test} == {2, 3}
    assert split["purged_train_rows"] == {"within_counterfactual_embargo": 1}


def test_split_purges_realized_labels_that_close_after_test_start_or_lack_exit() -> None:
    safe = _row(0, verdict="WIN", cycle="2026-08-01T10:00:00Z", action="BUY")
    safe["decision"]["executed"] = True
    safe["target"] = {
        "execution": {"position_cycles": [{"exit_ts": "2026-08-04T09:00:00Z"}]}
    }
    overlaps = _row(1, verdict="LOSS", cycle="2026-08-02T10:00:00Z", action="BUY")
    overlaps["decision"]["executed"] = True
    overlaps["target"] = {
        "execution": {"position_cycles": [{"exit_ts": "2026-08-06T09:00:00Z"}]}
    }
    missing_exit = _row(2, verdict="WIN", cycle="2026-08-03T10:00:00Z", action="BUY")
    missing_exit["decision"]["executed"] = True
    test_a = _row(3, verdict="LOSS", cycle="2026-08-05T10:00:00Z")
    test_b = _row(4, verdict="WIN", cycle="2026-08-06T10:00:00Z")
    examples, _coverage = ablation.prepare_examples([safe, overlaps, missing_exit, test_a, test_b])

    train, test, split = ablation.chronological_group_split(
        examples,
        test_fraction=0.4,
        embargo_hours=24.0,
    )

    assert {row.ordinal for row in train} == {0}
    assert {row.ordinal for row in test} == {3, 4}
    assert split["purged_train_rows"] == {
        "realized_exit_at_or_after_test_start": 1,
        "realized_execution_target_missing": 1,
    }


def test_prepare_examples_excludes_non_binary_labels_and_reports_coverage() -> None:
    rows = [
        _row(0, verdict="WIN", rationale="Breakout with controlled risk", thesis={"side": "long"}),
        _row(1, verdict="LOSS"),
        _row(2, verdict="NEUTRAL"),
        _row(3, verdict="UNKNOWN"),
        _row(4, verdict="PENDING"),
        _row(5, verdict=None),
        _row(6, verdict=None) | {"learning_outcome": {"verdict": None, "forward_return": None}},
    ]

    examples, coverage = ablation.prepare_examples(rows)

    assert [row.label for row in examples] == ["WIN", "LOSS"]
    assert coverage["eligible_win_loss"] == 2
    assert coverage["excluded_labels"] == {"neutral": 1, "pending": 3, "unknown": 1}
    assert coverage["decision_time_fields"]["structured_thesis"]["records"] == 1
    assert coverage["decision_time_fields"]["rationale_text"]["records"] == 1
    assert coverage["decision_time_fields"]["tool_pattern"]["records"] == 7
    assert coverage["decision_time_fields"]["universe_context"]["records"] == 7
    assert coverage["universe_context_provenance"]["observed_records"] == 0
    assert coverage["universe_context_provenance"]["sources"] == {"task_state_before": 7}
    assert coverage["trader_outcome_records"]["records"] == 7
    assert coverage["outcome_basis_labelled"] == {"counterfactual": 2}


def test_universe_ablation_adds_predictive_lift_on_identical_base_features() -> None:
    rows: list[dict] = []
    # Alternating labels keep both classes in chronological train and test.
    # Base features are identical; the Universe direction is the only signal.
    for index in range(40):
        verdict = "WIN" if index % 2 == 0 else "LOSS"
        direction = "long" if verdict == "WIN" else "short"
        day = index // 20 + 1
        hour = index % 20
        rows.append(
            _row(
                index,
                verdict=verdict,
                direction=direction,
                cycle=f"2026-07-{day:02d}T{hour:02d}:00:00Z",
            )
        )

    report = ablation.run_analysis(
        rows,
        test_fraction=0.25,
        min_class_support=4,
        min_train_class_support=2,
        min_test_class_support=2,
    )

    assert report["status"] == "GO"
    assert report["split"]["cycle_overlap"] == []
    base = report["models"]["base"]["metrics"]
    enriched = report["models"]["base_plus_universe"]["metrics"]
    assert enriched["balanced_accuracy"] == 1.0
    assert enriched["macro_f1"] == 1.0
    assert enriched["log_loss"] < base["log_loss"]
    assert report["lift_enriched_vs_base"]["balanced_accuracy"] > 0
    assert report["evaluation_contract"]["outcome_score_used_as_target"] is False
    assert report["evaluation_contract"]["outcome_basis"] == "counterfactual"
    assert report["evaluation_contract"]["basis_pooling"] is False
    assert report["coverage"]["analysis_cohort"] == {
        "outcome_basis": "counterfactual",
        "eligible_rows": 40,
        "excluded_other_basis_rows": 0,
    }
    assert report["readiness"]["by_outcome_basis"]["counterfactual"]["status"] == "GO"
    assert report["readiness"]["by_outcome_basis"]["realized"]["status"] == "NO_GO"


def test_readiness_gate_rejects_insufficient_class_support() -> None:
    rows = [_row(index, verdict="WIN") for index in range(8)] + [_row(8, verdict="LOSS")]

    report = ablation.run_analysis(
        rows,
        min_class_support=3,
        min_train_class_support=1,
        min_test_class_support=1,
    )

    assert report["status"] == "NO_GO"
    assert "total_loss_support<3" in report["readiness"]["reasons"]


def test_default_model_cohort_excludes_realized_rows_instead_of_pooling() -> None:
    rows = [
        _row(index, verdict="WIN" if index % 2 == 0 else "LOSS")
        for index in range(8)
    ]
    for index in range(8, 10):
        realized = _row(index, verdict="WIN" if index == 8 else "LOSS", action="BUY")
        realized["decision"]["executed"] = True
        rows.append(realized)

    report = ablation.run_analysis(
        rows,
        min_class_support=1,
        min_train_class_support=1,
        min_test_class_support=1,
    )

    assert report["coverage"]["analysis_cohort"] == {
        "outcome_basis": "counterfactual",
        "eligible_rows": 8,
        "excluded_other_basis_rows": 2,
    }
    assert report["models"]


def test_outcome_basis_distinguishes_executed_entry_from_blocked_entry() -> None:
    executed = _row(0, verdict="WIN", action="BUY")
    executed["decision"]["executed"] = True
    blocked = _row(1, verdict="LOSS", action="BUY")
    blocked["decision"]["executed"] = False

    examples, coverage = ablation.prepare_examples([executed, blocked])

    assert [row.outcome_basis for row in examples] == ["realized", "counterfactual"]
    assert coverage["outcome_basis_labelled"] == {"counterfactual": 1, "realized": 1}


def test_open_short_intent_overrides_sell_action_for_realized_basis() -> None:
    row = _row(0, verdict="WIN", action="SELL")
    row["decision"]["intent"] = "OPEN_SHORT"
    row["decision"]["executed"] = True

    examples, _coverage = ablation.prepare_examples([row])

    assert examples[0].outcome_basis == "realized"


def test_close_and_reduce_intents_are_counterfactual_regardless_of_physical_side() -> None:
    close_short = _row(0, verdict="WIN", action="BUY")
    close_short["decision"].update({"intent": "CLOSE", "executed": True})
    close_long = _row(1, verdict="LOSS", action="SELL")
    close_long["decision"].update({"intent": "CLOSE", "executed": True})
    reduce_short = _row(2, verdict="WIN", action="BUY")
    reduce_short["decision"].update({"intent": "REDUCE", "executed": True})

    examples, coverage = ablation.prepare_examples([close_short, close_long, reduce_short])

    assert [row.outcome_basis for row in examples] == [
        "counterfactual",
        "counterfactual",
        "counterfactual",
    ]
    assert coverage["outcome_basis_labelled"] == {"counterfactual": 3}


def test_structured_thesis_coverage_is_strict_not_any_structured_hypothesis() -> None:
    row = _row(0, verdict="WIN")
    row["decision"]["entry_dimensions"] = {"setup": "breakout"}

    examples, coverage = ablation.prepare_examples([row])

    assert examples[0].has_structured_thesis is False
    assert coverage["decision_time_fields"]["structured_thesis"]["records"] == 0
    assert coverage["decision_time_fields"]["structured_hypothesis_any"]["records"] == 1


def test_cross_loop_reward_and_context_shape_is_supported(tmp_path: Path) -> None:
    row = {
        "schema_version": "cross_loop_episode_v1",
        "identity": {"cycle_id": "2026-08-01T00:00:00Z", "symbol": "ABC", "decision_id": "d1"},
        "market_state": {"range_position": 0.9},
        "portfolio_state": {"equity": 100.0, "gross_exposure_usd": 10.0},
        "universe_context": {
            "observed_context": {
                "symbol_mandate": {
                    "directional_view": "long",
                    "allowed_sides": ["long"],
                    "role": "leader",
                    "posture": "engage",
                    "confidence": 0.9,
                    "family_context": {"family": "software"},
                }
            },
        },
        "hypotheses": {
            "universe": {
                "directional_view": "long",
                "allowed_sides": ["long"],
                "role": "leader",
                "posture": "engage",
                "confidence": 0.9,
            },
            "trader": {
                "structured_thesis": {"setup": "breakout"},
                "rationale": "Confirmed breakout",
            },
        },
        "trader_decision": {
            "action": "OPEN_LONG",
            "intent": "OPEN_LONG",
            "confidence": 0.9,
            "thesis": {"setup": "breakout"},
            "rationale": "Confirmed breakout",
        },
        "trader_workflow": {
            "domain_steps": [{"tool": "evaluate_trade_plan", "semantic_valid": True}],
        },
        "target": {
            "trader_flair": {"verdict": "WIN", "basis": "realised_flat_to_flat_fee_complete"},
        },
    }
    path = tmp_path / "cross_loop_episodes.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    loaded, malformed = ablation.read_jsonl(path)
    examples, coverage = ablation.prepare_examples(loaded)

    assert malformed == 0
    assert len(examples) == 1
    assert examples[0].label == "WIN"
    assert examples[0].outcome_basis == "realized"
    assert examples[0].has_universe_context is True
    assert examples[0].universe_context_observed is True
    assert examples[0].universe_context_source == "cross_loop_observed_context"
    assert examples[0].has_tool_pattern is True
    assert any(token == "universe.directional_view=long" for token in examples[0].universe_features)
    assert not any("directional_view" in token for token in examples[0].base_features)
    assert coverage["schema_versions"] == {"cross_loop_episode_v1": 1}
