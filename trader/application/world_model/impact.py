"""Honest shadow-impact metrics for the action-free World model.

This module deliberately separates two questions that are often conflated:

* Did a shadow forecast describe the later *market* transition well?
* Did that forecast cause, or improve, a Trader result?

The first question is measurable from immutable World predictions and fixed
horizon labels.  The second is **not** implied by a coincident decision or
P&L: World predictions are ``NO_GO`` / ``shadow_only`` and World episodes do
not contain a decision, order, fill, position, or portfolio state.  A caller
may supply a future durable ``prediction -> decision -> flat-to-flat cycle``
join, in which case this module reports a descriptive association only.  It
never promotes that association into causal contribution or a counterfactual
Trader result.

All inputs are duck-typed mappings/domain records so this is a pure
application service.  It deliberately does not read a store, invoke a model,
or alter the daemon's decision path.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
import math
from typing import Any


CLASSES = ("DOWN", "FLAT", "UP")
UNIFORM_BRIER = 2.0 / 3.0
DEFAULT_MINIMUM_MARKET_SAMPLES = 20
DEFAULT_MINIMUM_TRADER_CYCLES = 20

_PRE_DECISION_LINK_KINDS = frozenset(
    {
        "pre_decision_reference",
        "predecision_reference",
        "pre_decision_input",
    }
)


def evaluate_world_shadow_impact(
    predictions: Iterable[object],
    outcomes: Iterable[object],
    *,
    decision_rows: Iterable[object] = (),
    decision_links: Iterable[object] = (),
    position_cycles: Iterable[object] = (),
    minimum_market_samples: int = DEFAULT_MINIMUM_MARKET_SAMPLES,
    minimum_trader_cycles: int = DEFAULT_MINIMUM_TRADER_CYCLES,
) -> dict[str, Any]:
    """Return prequential World metrics and strictly non-causal Trader views.

    ``position_cycles`` must already be canonical *completed flat-to-flat*
    cycles (the output of ``aggregate_position_cycles``), not individual
    fills or partial exit legs.  A join is accepted only when the caller
    supplies an explicit durable ``decision_links`` record such as::

        {
            "prediction_id": "world-prediction:v1:…",
            "decision_id": "<cycle-ts>|<sequence>|<symbol>",
            "link_kind": "pre_decision_reference",
        }

    and both the prediction's logical cutoff (``predicted_at``/``created_at``)
    and durable wall-clock ``ready_at`` prove that the exact record existed
    before the decision.  The link's ``linked_at`` must also precede the
    durable ``decision_dispatch_at`` boundary.  The logical cutoff alone is
    not enough because the daemon worker is asynchronous.  This is
    intentionally more restrictive than a symbol/time heuristic.  The resulting Trader group is labelled
    ``descriptive_only``: the current World-model contract has
    ``decision_effect='none'``, so there is no realised causal contribution to
    calculate.  Counterfactual Trader P&L remains unavailable until a
    separately persisted, pre-registered execution/simulation ledger exists.
    """

    if minimum_market_samples <= 0:
        raise ValueError("minimum_market_samples must be positive")
    if minimum_trader_cycles <= 0:
        raise ValueError("minimum_trader_cycles must be positive")

    prediction_rows = list(predictions)
    outcome_rows = list(outcomes)
    decision_row_list = list(decision_rows)
    decision_link_list = list(decision_links)
    cycle_rows = list(position_cycles)

    market = _market_metrics(
        prediction_rows,
        outcome_rows,
        minimum_samples=minimum_market_samples,
    )
    trader = _trader_observation(
        prediction_rows,
        decision_row_list,
        decision_link_list,
        cycle_rows,
        minimum_cycles=minimum_trader_cycles,
    )
    return {
        "schema_version": "world_shadow_impact.v1",
        "authority": "shadow_only",
        "decision_effect": "none",
        "market": market,
        "trader": trader,
        "actual_contribution": {
            "status": "not_attributable",
            "reason": "world_model_decision_effect_none",
            "value": None,
            "claim": "No causal Trader contribution is inferred from a shadow forecast.",
        },
        "counterfactual_contribution": {
            "status": "not_available",
            "reason": "counterfactual_execution_ledger_missing",
            "value": None,
            "required_contract": [
                "pre_registered_policy_id_and_version",
                "durable_prediction_to_decision_reference",
                "causal_execution_or_simulated_fill_ledger",
                "fees_fx_and_flat_to_flat_cycle_accounting",
            ],
        },
    }


def _market_metrics(
    predictions: list[object],
    outcomes: list[object],
    *,
    minimum_samples: int,
) -> dict[str, Any]:
    selected_outcomes, ambiguous_outcomes = _select_outcomes(outcomes)
    grouped: dict[tuple[str, str, str], list[_MarketPair]] = defaultdict(list)
    excluded: Counter[str] = Counter()

    selected_predictions = _select_market_predictions(predictions, excluded)
    for prediction, episode_id, horizon_id, model_id, model_version in selected_predictions:
        key = (episode_id, horizon_id)
        if key in ambiguous_outcomes:
            excluded["ambiguous_outcome_revision"] += 1
            continue
        outcome = selected_outcomes.get(key)
        if outcome is None:
            excluded["outcome_not_observed"] += 1
            continue
        probabilities = _probabilities(prediction)
        target_index = _target_index(outcome)
        if probabilities is None or target_index is None:
            excluded["invalid_score_payload"] += 1
            continue
        predicted_at = _prediction_logical_at(prediction)
        ready_at = _prediction_ready_at(prediction)
        outcome_available_at = _event_time(
            outcome,
            "available_at",
            "label_available_at",
            "computed_at",
            "sealed_at",
            "recorded_at",
        )
        if (
            predicted_at is None
            or ready_at is None
            or outcome_available_at is None
            or predicted_at >= outcome_available_at
            or ready_at >= outcome_available_at
        ):
            excluded["causal_order_unproven"] += 1
            continue
        grouped[(model_id, model_version, horizon_id)].append(
            _MarketPair(
                prediction=prediction,
                outcome=outcome,
                probabilities=probabilities,
                target_index=target_index,
                predicted_at=predicted_at,
                ready_at=ready_at,
                outcome_available_at=outcome_available_at,
            )
        )

    groups = [
        _market_group(
            model_id=model_id,
            model_version=model_version,
            horizon_id=horizon_id,
            rows=rows,
            minimum_samples=minimum_samples,
        )
        for (model_id, model_version, horizon_id), rows in sorted(grouped.items())
    ]
    matched = sum(group["matched"] for group in groups)
    if not groups:
        status = "insufficient"
    elif any(group["status"] == "ready" for group in groups):
        status = "ready"
    else:
        status = "insufficient"
    return {
        "status": status,
        "prediction_rows": len(predictions),
        "outcome_rows": len(outcomes),
        "matched": matched,
        "minimum_samples": minimum_samples,
        "groups": groups,
        "excluded": dict(sorted(excluded.items())),
        "drawdown_semantics": (
            "A zero-cost, one-unit-notional-per-forecast directional market proxy; "
            "overlapping horizons mean this is not a tradable portfolio equity curve."
        ),
    }


def _select_market_predictions(
    predictions: list[object],
    excluded: Counter[str],
) -> list[tuple[object, str, str, str, str]]:
    """Choose one exact immutable replay for each model-slot prediction.

    A repeated read of the same append-only prediction must not inflate a
    model's support.  Conversely, two different payloads claiming the same
    model-slot identity make the evidence ambiguous: selecting the first one
    would turn input ordering into an apparent model result.
    """

    candidates: dict[tuple[str, str, str, str], list[object]] = defaultdict(list)
    for prediction in predictions:
        episode_id = _text(_field(prediction, "episode_id"))
        horizon_id = _horizon_id(prediction)
        if episode_id is None or horizon_id is None:
            excluded["invalid_prediction_identity"] += 1
            continue
        model_id, model_version = _model_identity(prediction)
        candidates[(episode_id, horizon_id, model_id, model_version)].append(prediction)

    selected: list[tuple[object, str, str, str, str]] = []
    for (episode_id, horizon_id, model_id, model_version), rows in sorted(candidates.items()):
        signatures = {_prediction_replay_signature(row) for row in rows}
        if len(signatures) != 1:
            excluded["ambiguous_prediction_identity"] += len(rows)
            continue
        if len(rows) > 1:
            excluded["duplicate_prediction_replay"] += len(rows) - 1
        selected.append((rows[0], episode_id, horizon_id, model_id, model_version))
    return selected


def _prediction_replay_signature(prediction: object) -> str:
    """Fingerprint every causal/scoring field for fail-closed replay collapse."""

    probabilities = _probabilities(prediction)
    predicted_at = _prediction_logical_at(prediction)
    ready_at = _prediction_ready_at(prediction)
    payload = {
        "prediction_id": _prediction_id(prediction),
        "probabilities": probabilities,
        "predicted_class": (_predicted_class(prediction, probabilities) if probabilities is not None else None),
        "predicted_at": None if predicted_at is None else predicted_at.isoformat(),
        "ready_at": None if ready_at is None else ready_at.isoformat(),
        "feature_hash": _signature_value(_prediction_value(prediction, "feature_hash")),
        "predicted_return": _signature_value(_prediction_value(prediction, "predicted_return")),
        "training_cutoff": _signature_value(_prediction_value(prediction, "training_cutoff")),
        "model_fingerprint": _signature_value(_prediction_value(prediction, "model_fingerprint")),
        "status": _signature_value(_prediction_value(prediction, "status")),
        "tier": _signature_value(_prediction_value(prediction, "tier")),
        "support": _signature_value(_prediction_value(prediction, "support")),
        "exact_support": _signature_value(_prediction_value(prediction, "exact_support")),
        "coarse_support": _signature_value(_prediction_value(prediction, "coarse_support")),
        "global_support": _signature_value(_prediction_value(prediction, "global_support")),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _market_group(
    *,
    model_id: str,
    model_version: str,
    horizon_id: str,
    rows: list["_MarketPair"],
    minimum_samples: int,
) -> dict[str, Any]:
    calibration = _calibration(rows)
    directional_rows = [row for row in rows if (_predicted_class(row.prediction, row.probabilities)) != "FLAT"]
    correct = sum(
        _predicted_class(row.prediction, row.probabilities) == CLASSES[row.target_index] for row in directional_rows
    )
    return_rows: list[tuple[datetime, float]] = []
    return_missing = 0
    for row in rows:
        simple_return = _simple_return(row.outcome)
        if simple_return is None:
            return_missing += 1
            continue
        direction = _direction_multiplier(_predicted_class(row.prediction, row.probabilities))
        return_rows.append((row.ready_at, direction * simple_return))
    proxy = _return_proxy(return_rows, missing_returns=return_missing)
    matched = len(rows)
    return {
        "model_id": model_id,
        "model_version": model_version,
        "horizon_id": horizon_id,
        "status": "ready" if matched >= minimum_samples else "insufficient",
        "matched": matched,
        "minimum_samples": minimum_samples,
        "calibration": calibration,
        "direction": {
            "status": "available" if directional_rows else "insufficient",
            "directional_predictions": len(directional_rows),
            "directional_coverage": round(len(directional_rows) / matched, 8) if matched else None,
            "directional_accuracy": round(correct / len(directional_rows), 8) if directional_rows else None,
            "all_class_accuracy": calibration["accuracy"],
            "flat_predictions": matched - len(directional_rows),
        },
        "directional_market_proxy": proxy,
    }


def _calibration(rows: list["_MarketPair"]) -> dict[str, Any]:
    brier_sum = 0.0
    log_loss_sum = 0.0
    correct = 0
    calibration_bins: list[list[float]] = [[] for _ in range(5)]
    calibration_hits: list[list[float]] = [[] for _ in range(5)]
    for row in rows:
        probabilities = row.probabilities
        target_index = row.target_index
        brier_sum += sum(
            (probability - (1.0 if index == target_index else 0.0)) ** 2
            for index, probability in enumerate(probabilities)
        )
        log_loss_sum += -math.log(max(probabilities[target_index], 1e-15))
        predicted_index = _predicted_index(probabilities)
        hit = 1.0 if predicted_index == target_index else 0.0
        correct += int(hit)
        confidence = probabilities[predicted_index]
        bin_index = min(int(confidence * 5), 4)
        calibration_bins[bin_index].append(confidence)
        calibration_hits[bin_index].append(hit)
    count = len(rows)
    brier = brier_sum / count
    ece = sum(
        (len(confidences) / count)
        * abs(sum(confidences) / len(confidences) - sum(calibration_hits[index]) / len(calibration_hits[index]))
        for index, confidences in enumerate(calibration_bins)
        if confidences
    )
    return {
        "brier": round(brier, 8),
        "uniform_brier": round(UNIFORM_BRIER, 8),
        "brier_skill_vs_uniform": round(1.0 - brier / UNIFORM_BRIER, 8),
        "log_loss": round(log_loss_sum / count, 8),
        "accuracy": round(correct / count, 8),
        "ece_5_bins": round(ece, 8),
    }


def _return_proxy(
    rows: list[tuple[datetime, float]],
    *,
    missing_returns: int,
) -> dict[str, Any]:
    if not rows:
        return {
            "status": "insufficient",
            "reason": "outcome_simple_return_unavailable",
            "observations": 0,
            "missing_returns": missing_returns,
            "gross_return_sum": None,
            "mean_return": None,
            "max_drawdown": None,
        }
    # Equal logical cutoffs are common for two symbols in one snapshot.  A
    # deterministic tie-break keeps the additive proxy independent of input
    # iteration order.
    ordered = sorted(rows, key=lambda item: (item[0], item[1]))
    returns = [item[1] for item in ordered]
    drawdown = _additive_drawdown(returns)
    return {
        "status": "available",
        "observations": len(returns),
        "missing_returns": missing_returns,
        "gross_return_sum": round(sum(returns), 8),
        "mean_return": round(sum(returns) / len(returns), 8),
        "max_drawdown": round(drawdown, 8),
        "unit": "simple_return_per_one_unit_notional_forecast",
        "costs": "excluded",
    }


def _trader_observation(
    predictions: list[object],
    decision_rows: list[object],
    decision_links: list[object],
    position_cycles: list[object],
    *,
    minimum_cycles: int,
) -> dict[str, Any]:
    links, link_excluded = _validated_predecision_links(
        predictions,
        decision_rows,
        decision_links,
    )
    if not links:
        return {
            "status": "non_joinable",
            "reason": "missing_durable_predecision_prediction_link",
            "minimum_cycles": minimum_cycles,
            "groups": [],
            "join_excluded": dict(sorted(link_excluded.items())),
            "required_contract": _join_contract(),
            "drawdown_semantics": "No joined completed-cycle series is available.",
        }

    groups, cycle_excluded = _join_cycles_to_predictions(
        links,
        position_cycles,
        minimum_cycles=minimum_cycles,
    )
    excluded = link_excluded + cycle_excluded
    if not groups:
        status = "non_joinable"
        reason = "no_unambiguous_fee_complete_flat_to_flat_cycle"
    elif any(group["status"] == "descriptive_only" for group in groups):
        status = "descriptive_only"
        reason = None
    else:
        status = "insufficient"
        reason = "joined_cycle_count_below_minimum"
    return {
        "status": status,
        "reason": reason,
        "minimum_cycles": minimum_cycles,
        "groups": groups,
        "join_excluded": dict(sorted(excluded.items())),
        "required_contract": _join_contract(),
        "claim": (
            "Joined results are descriptive association under the existing Trader policy, "
            "not causal impact of a shadow model."
        ),
        "drawdown_semantics": (
            "Exit-order additive net-PnL drawdown across completed flat-to-flat cycles; "
            "it is not a portfolio-equity drawdown because capital allocation and concurrent positions "
            "are not reconstructed here."
        ),
    }


def _validated_predecision_links(
    predictions: list[object],
    decisions: list[object],
    links: list[object],
) -> tuple[list["_DecisionPredictionLink"], Counter[str]]:
    excluded: Counter[str] = Counter()
    predictions_by_id: dict[str, list[object]] = defaultdict(list)
    for prediction in predictions:
        prediction_id = _prediction_id(prediction)
        if prediction_id is not None:
            predictions_by_id[prediction_id].append(prediction)
    decisions_by_id: dict[str, list[object]] = defaultdict(list)
    for decision in decisions:
        decision_id = _text(_field(decision, "decision_id"))
        if decision_id is not None:
            decisions_by_id[decision_id].append(decision)

    accepted: list[_DecisionPredictionLink] = []
    replayed_links = _select_predecision_link_replays(links, excluded)
    for link in replayed_links:
        prediction_id = _text(_field(link, "prediction_id"))
        decision_id = _text(_field(link, "decision_id"))
        # ``_select_predecision_link_replays`` has already checked these
        # identities and the provenance kind.  Keep the guard so this helper
        # remains fail-closed if it is changed independently.
        if prediction_id is None or decision_id is None:
            excluded["invalid_link_identity"] += 1
            continue
        prediction_rows = predictions_by_id.get(prediction_id, [])
        decision_rows = decisions_by_id.get(decision_id, [])
        if len(prediction_rows) != 1:
            excluded["prediction_reference_not_unique"] += 1
            continue
        if len(decision_rows) != 1:
            excluded["decision_reference_not_unique"] += 1
            continue
        prediction = prediction_rows[0]
        decision = decision_rows[0]
        if (_text(_field(decision, "decision_source")) or "").lower() == "infra":
            excluded["infra_decision_not_joinable"] += 1
            continue
        prediction_at = _prediction_logical_at(prediction)
        prediction_ready_at = _prediction_ready_at(prediction)
        linked_at = _event_time(link, "linked_at")
        # ``cycle_ts`` is the beginning of the whole daemon cycle and can be
        # earlier than the post-I/O World snapshot.  Only a durable dispatch /
        # boundary proves that a ready prediction preceded the actual Brain
        # decision request.  A prompt-start fallback is not a dispatch proof.
        decision_at = _event_time(decision, "decision_dispatch_at")
        if prediction_at is None or prediction_ready_at is None or linked_at is None or decision_at is None:
            excluded["predecision_order_unproven"] += 1
            continue
        if prediction_at >= decision_at:
            excluded["prediction_after_decision"] += 1
            continue
        if prediction_ready_at >= linked_at:
            excluded["link_before_prediction_ready"] += 1
            continue
        if linked_at >= decision_at:
            excluded["link_after_decision"] += 1
            continue
        probabilities = _probabilities(prediction)
        if probabilities is None:
            excluded["linked_prediction_invalid_probabilities"] += 1
            continue
        horizon_id = _horizon_id(prediction)
        if horizon_id is None:
            excluded["linked_prediction_missing_horizon"] += 1
            continue
        model_id, model_version = _model_identity(prediction)
        accepted.append(
            _DecisionPredictionLink(
                prediction_id=prediction_id,
                decision_id=decision_id,
                model_id=model_id,
                model_version=model_version,
                horizon_id=horizon_id,
                predicted_class=_predicted_class(prediction, probabilities),
            )
        )

    grouped_by_decision_model_horizon: dict[tuple[str, str, str, str], list[_DecisionPredictionLink]] = defaultdict(
        list
    )
    for link in accepted:
        grouped_by_decision_model_horizon[
            (link.decision_id, link.model_id, link.model_version, link.horizon_id)
        ].append(link)

    unambiguous: list[_DecisionPredictionLink] = []
    for _, group in sorted(grouped_by_decision_model_horizon.items()):
        if len({link.prediction_id for link in group}) > 1:
            excluded["ambiguous_prediction_decision_link"] += len(group)
            continue
        unambiguous.extend(group)
    return unambiguous, excluded


def _select_predecision_link_replays(
    links: list[object],
    excluded: Counter[str],
) -> list[object]:
    """Collapse exact link replays without letting input order choose a link."""

    candidates: dict[tuple[str, str], list[object]] = defaultdict(list)
    for link in links:
        prediction_id = _text(_field(link, "prediction_id"))
        decision_id = _text(_field(link, "decision_id"))
        if prediction_id is None or decision_id is None:
            excluded["invalid_link_identity"] += 1
            continue
        if _link_kind(link) not in _PRE_DECISION_LINK_KINDS:
            excluded["link_not_predecision_provenance"] += 1
            continue
        candidates[(prediction_id, decision_id)].append(link)

    selected: list[object] = []
    for _, rows in sorted(candidates.items()):
        signatures = {_predecision_link_replay_signature(row) for row in rows}
        if len(signatures) != 1:
            excluded["ambiguous_prediction_decision_link"] += len(rows)
            continue
        if len(rows) > 1:
            excluded["duplicate_prediction_decision_link_replay"] += len(rows) - 1
        selected.append(rows[0])
    return selected


def _predecision_link_replay_signature(link: object) -> str:
    linked_at = _event_time(link, "linked_at")
    payload = {
        "prediction_id": _text(_field(link, "prediction_id")),
        "decision_id": _text(_field(link, "decision_id")),
        "link_kind": _link_kind(link),
        "linked_at": None if linked_at is None else linked_at.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _join_cycles_to_predictions(
    links: list["_DecisionPredictionLink"],
    cycles: list[object],
    *,
    minimum_cycles: int,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    excluded: Counter[str] = Counter()
    links_by_decision: dict[str, list[_DecisionPredictionLink]] = defaultdict(list)
    for link in links:
        links_by_decision[link.decision_id].append(link)
    grouped: dict[tuple[str, str, str], list[_JoinedCycle]] = defaultdict(list)
    seen_by_group: set[tuple[tuple[str, str, str], str]] = set()

    for index, cycle in enumerate(cycles):
        if _field(cycle, "position_cycle_closed") is not True:
            excluded["cycle_not_flat_to_flat_completed"] += 1
            continue
        decision_ids = _cycle_decision_ids(cycle)
        if len(decision_ids) != 1:
            excluded["cycle_entry_decision_ambiguous"] += 1
            continue
        matching_links = links_by_decision.get(decision_ids[0], [])
        if not matching_links:
            excluded["cycle_without_predecision_prediction_link"] += 1
            continue
        net_pnl = _finite_float(_field(cycle, "pnl"))
        gross_pnl = _finite_float(_field(cycle, "gross_pnl"))
        quality = _field(cycle, "commission_quality")
        if not isinstance(quality, Mapping) or quality.get("status") != "available" or net_pnl is None:
            excluded["cycle_net_economics_unavailable"] += 1
            continue
        if gross_pnl is None:
            excluded["cycle_gross_economics_unavailable"] += 1
            continue
        exit_at = _event_time(cycle, "exit_ts")
        if exit_at is None:
            excluded["cycle_exit_time_unavailable"] += 1
            continue
        cycle_key = _cycle_key(cycle, index)
        side = _text(_field(cycle, "side"))
        for link in matching_links:
            group_key = (link.model_id, link.model_version, link.horizon_id)
            dedup_key = (group_key, cycle_key)
            if dedup_key in seen_by_group:
                excluded["duplicate_cycle_prediction_join"] += 1
                continue
            seen_by_group.add(dedup_key)
            grouped[group_key].append(
                _JoinedCycle(
                    net_pnl=net_pnl,
                    gross_pnl=gross_pnl,
                    exit_at=exit_at,
                    side=side,
                    predicted_class=link.predicted_class,
                )
            )

    groups = [
        _trader_group(
            model_id=model_id,
            model_version=model_version,
            horizon_id=horizon_id,
            cycles=joined,
            minimum_cycles=minimum_cycles,
        )
        for (model_id, model_version, horizon_id), joined in sorted(grouped.items())
    ]
    return groups, excluded


def _trader_group(
    *,
    model_id: str,
    model_version: str,
    horizon_id: str,
    cycles: list["_JoinedCycle"],
    minimum_cycles: int,
) -> dict[str, Any]:
    ordered = sorted(
        cycles,
        key=lambda cycle: (
            cycle.exit_at,
            cycle.net_pnl,
            cycle.gross_pnl,
            cycle.side or "",
            cycle.predicted_class,
        ),
    )
    net_pnls = [cycle.net_pnl for cycle in ordered]
    gross_pnls = [cycle.gross_pnl for cycle in ordered]
    alignment = Counter(_alignment(cycle.predicted_class, cycle.side) for cycle in ordered)
    count = len(ordered)
    return {
        "model_id": model_id,
        "model_version": model_version,
        "horizon_id": horizon_id,
        "status": "descriptive_only" if count >= minimum_cycles else "insufficient",
        "claim": "association_only_under_existing_trader_policy",
        "joined_completed_cycles": count,
        "minimum_cycles": minimum_cycles,
        "net_pnl": round(sum(net_pnls), 8),
        "gross_pnl": round(sum(gross_pnls), 8),
        "win_rate_net": round(sum(value > 0.0 for value in net_pnls) / count, 8),
        "mean_net_pnl": round(sum(net_pnls) / count, 8),
        "max_drawdown_net_pnl": round(_additive_drawdown(net_pnls), 8),
        "direction_alignment": {
            "aligned": alignment["aligned"],
            "opposed": alignment["opposed"],
            "neutral_or_unactionable": alignment["neutral_or_unactionable"],
        },
    }


def _select_outcomes(
    outcomes: list[object],
) -> tuple[dict[tuple[str, str], object], set[tuple[str, str]]]:
    superseded_ids = {
        value
        for outcome in outcomes
        if (
            value := _text(
                _field(
                    outcome,
                    "supersedes_event_id",
                    _field(outcome, "supersedes_outcome_event_id"),
                )
            )
        )
        is not None
    }
    candidates: dict[tuple[str, str], list[object]] = defaultdict(list)
    for outcome in outcomes:
        if (_text(_field(outcome, "status")) or "").lower() != "observed":
            continue
        if _field(outcome, "training_eligible") is False:
            continue
        event_id = _text(
            _field(outcome, "event_id", _field(outcome, "outcome_event_id", _field(outcome, "outcome_id")))
        )
        if event_id is not None and event_id in superseded_ids:
            continue
        episode_id = _text(_field(outcome, "episode_id"))
        horizon_id = _horizon_id(outcome)
        if episode_id is not None and horizon_id is not None:
            candidates[(episode_id, horizon_id)].append(outcome)
    selected: dict[tuple[str, str], object] = {}
    ambiguous: set[tuple[str, str]] = set()
    for key, rows in candidates.items():
        if len(rows) == 1:
            selected[key] = rows[0]
        else:
            ambiguous.add(key)
    return selected, ambiguous


def _model_identity(prediction: object) -> tuple[str, str]:
    payload = _field(prediction, "prediction")
    model_id = (
        _text(_field(prediction, "model_id"))
        or _text(_field(prediction, "model_kind"))
        or _text(_field(payload, "model_id"))
        or "unknown-model"
    )
    model_version = (
        _text(_field(prediction, "model_version")) or _text(_field(payload, "model_version")) or "unknown-version"
    )
    return model_id, model_version


def _prediction_value(prediction: object, name: str) -> object:
    direct = _field(prediction, name)
    if direct is not None:
        return direct
    for nested_name in ("prediction", "prediction_record", "payload"):
        payload = _field(prediction, nested_name)
        nested = _field(payload, name) if payload is not None and payload is not prediction else None
        if nested is not None:
            return nested
    return None


def _prediction_event_time(prediction: object, *names: str) -> datetime | None:
    direct = _event_time(prediction, *names)
    if direct is not None:
        return direct
    for nested_name in ("prediction", "prediction_record", "payload"):
        payload = _field(prediction, nested_name)
        if payload is not None and payload is not prediction:
            nested = _event_time(payload, *names)
            if nested is not None:
                return nested
    return None


def _prediction_logical_at(prediction: object) -> datetime | None:
    """Return the frozen market cutoff, never a persistence wall clock."""

    return _prediction_event_time(prediction, "predicted_at", "created_at")


def _prediction_ready_at(prediction: object) -> datetime | None:
    """Return the durable wall-clock availability time for a prediction."""

    # Store projections expose their append timestamp as ``ready_at``.  Keep
    # the indexed ``recorded_at`` fallback for callers that pass a raw store
    # row, where it has the same durable-availability meaning.
    return _prediction_event_time(prediction, "ready_at", "recorded_at")


def _signature_value(value: object) -> object:
    """Make optional prediction metadata deterministic and JSON-safe."""

    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo is not None else "<naive-datetime>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(key): _signature_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_signature_value(item) for item in value]
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        return _signature_value(enum_value)
    return repr(value)


def _probabilities(value: object) -> list[float] | None:
    raw = _field(value, "probabilities")
    if isinstance(raw, Mapping):
        try:
            probabilities = [float(raw[label]) for label in CLASSES]
        except (KeyError, TypeError, ValueError):
            return None
    elif isinstance(raw, (list, tuple)) and len(raw) == len(CLASSES):
        try:
            probabilities = [float(item) for item in raw]
        except (TypeError, ValueError):
            return None
    else:
        for nested_name in ("prediction", "prediction_record", "payload"):
            payload = _field(value, nested_name)
            if payload is not None and payload is not value:
                found = _probabilities(payload)
                if found is not None:
                    return found
        distribution = _field(value, "distribution")
        if distribution is not None and distribution is not value:
            return _probabilities({"probabilities": distribution})
        return None
    if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in probabilities):
        return None
    if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-9):
        return None
    return probabilities


def _target_index(outcome: object) -> int | None:
    raw = _field(outcome, "direction", _field(outcome, "move_class"))
    if raw is None:
        for nested_name in ("label", "outcome", "payload"):
            payload = _field(outcome, nested_name)
            raw = _field(payload, "direction", _field(payload, "move_class")) if payload is not None else None
            if raw is not None:
                break
    label = (_text(getattr(raw, "value", raw)) or "").upper()
    try:
        return CLASSES.index(label)
    except ValueError:
        return None


def _horizon_id(value: object) -> str | None:
    direct = _text(_field(value, "horizon_id", _field(value, "horizon_code")))
    if direct is not None:
        return direct
    raw = _field(value, "horizon")
    if isinstance(raw, Mapping):
        return _text(raw.get("id", raw.get("horizon_id")))
    nested = _text(getattr(raw, "horizon_id", getattr(raw, "id", raw)))
    if nested is not None:
        return nested
    for nested_name in ("prediction", "prediction_record", "label", "outcome", "payload"):
        payload = _field(value, nested_name)
        if payload is not None and payload is not value:
            nested = _horizon_id(payload)
            if nested is not None:
                return nested
    return None


def _prediction_id(value: object) -> str | None:
    direct = _text(_field(value, "prediction_id", _field(value, "id")))
    if direct is not None:
        return direct
    for nested_name in ("prediction", "prediction_record", "payload"):
        payload = _field(value, nested_name)
        if payload is not None and payload is not value:
            nested = _prediction_id(payload)
            if nested is not None:
                return nested
    return None


def _predicted_index(probabilities: list[float]) -> int:
    highest = max(probabilities)
    return (
        CLASSES.index("FLAT")
        if probabilities[CLASSES.index("FLAT")] == highest
        else max(range(len(CLASSES)), key=probabilities.__getitem__)
    )


def _predicted_class(prediction: object, probabilities: list[float]) -> str:
    raw = _field(prediction, "predicted_class")
    if raw is None:
        for nested_name in ("prediction", "prediction_record", "payload"):
            payload = _field(prediction, nested_name)
            raw = _field(payload, "predicted_class") if payload is not None else None
            if raw is not None:
                break
    candidate = (_text(getattr(raw, "value", raw)) or "").upper()
    return candidate if candidate in CLASSES else CLASSES[_predicted_index(probabilities)]


def _simple_return(outcome: object) -> float | None:
    for candidate in _outcome_values(outcome, "simple_return", "forward_return"):
        parsed = _finite_float(candidate)
        if parsed is not None:
            return parsed
    anchor_close = _finite_float(_outcome_value(outcome, "anchor_close"))
    endpoint_close = _finite_float(_outcome_value(outcome, "endpoint_close"))
    if anchor_close is None or endpoint_close is None or anchor_close <= 0.0:
        return None
    return endpoint_close / anchor_close - 1.0


def _outcome_values(outcome: object, *names: str) -> list[object]:
    values: list[object] = []
    for name in names:
        values.append(_outcome_value(outcome, name))
    return values


def _outcome_value(outcome: object, name: str) -> object:
    direct = _field(outcome, name)
    if direct is not None:
        return direct
    for nested_name in ("label", "outcome", "payload"):
        payload = _field(outcome, nested_name)
        nested = _field(payload, name) if payload is not None else None
        if nested is not None:
            return nested
    return None


def _event_time(value: object, *names: str) -> datetime | None:
    for name in names:
        raw = _field(value, name)
        if isinstance(raw, datetime):
            return raw.astimezone(timezone.utc) if raw.tzinfo is not None else None
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
    return None


def _cycle_decision_ids(cycle: object) -> list[str]:
    values = _field(cycle, "entry_decision_ids")
    candidates = values if isinstance(values, list) else []
    if not candidates:
        candidate = _field(cycle, "entry_decision_id")
        candidates = [candidate] if candidate is not None else []
    result: list[str] = []
    for candidate in candidates:
        value = _text(candidate)
        if value is not None and value not in result:
            result.append(value)
    return result


def _cycle_key(cycle: object, index: int) -> str:
    cycle_id = _text(_field(cycle, "position_cycle_id"))
    symbol = _text(_field(cycle, "symbol")) or "unknown"
    return f"{symbol}:{cycle_id}" if cycle_id is not None else f"legacy:{index}"


def _link_kind(link: object) -> str | None:
    raw = _text(_field(link, "link_kind", _field(link, "relation", _field(link, "kind"))))
    return raw.lower() if raw is not None else None


def _direction_multiplier(predicted_class: str) -> float:
    return {"DOWN": -1.0, "FLAT": 0.0, "UP": 1.0}[predicted_class]


def _alignment(predicted_class: str, side: str | None) -> str:
    normalized_side = (side or "").upper()
    if predicted_class == "UP" and normalized_side == "LONG":
        return "aligned"
    if predicted_class == "DOWN" and normalized_side == "SHORT":
        return "aligned"
    if predicted_class == "FLAT" or normalized_side not in {"LONG", "SHORT"}:
        return "neutral_or_unactionable"
    return "opposed"


def _additive_drawdown(values: list[float]) -> float:
    running = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        maximum = max(maximum, peak - running)
    return maximum


def _join_contract() -> dict[str, Any]:
    return {
        "decision_link": {
            "required_fields": [
                "prediction_id",
                "decision_id",
                "link_kind=pre_decision_reference",
                "linked_at",
            ],
            "meaning": "Durable evidence that this exact shadow prediction was available before the decision.",
        },
        "prediction": {
            "required_fields": ["prediction_id", "predicted_at_or_created_at", "ready_at"],
            "clock_note": (
                "Both the frozen market cutoff and ready_at must be strictly before the "
                "later market label; ready_at must be strictly before linked_at."
            ),
        },
        "decision": {
            "required_fields": ["decision_id", "decision_dispatch_at"],
            "clock_note": (
                "linked_at must be strictly before decision_dispatch_at; cycle_ts and "
                "brain_prompt_started_at are insufficient for this join."
            ),
            "excluded": ["decision_source=infra"],
        },
        "position_cycle": {
            "required_fields": [
                "position_cycle_closed=true",
                "exactly_one_entry_decision_id",
                "exit_ts",
                "gross_pnl",
                "pnl",
                "commission_quality.status=available",
            ],
            "grain": "flat_to_flat_position_cycle",
        },
    }


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


class _MarketPair:
    def __init__(
        self,
        *,
        prediction: object,
        outcome: object,
        probabilities: list[float],
        target_index: int,
        predicted_at: datetime,
        ready_at: datetime,
        outcome_available_at: datetime,
    ) -> None:
        self.prediction = prediction
        self.outcome = outcome
        self.probabilities = probabilities
        self.target_index = target_index
        self.predicted_at = predicted_at
        self.ready_at = ready_at
        self.outcome_available_at = outcome_available_at


class _DecisionPredictionLink:
    def __init__(
        self,
        *,
        prediction_id: str,
        decision_id: str,
        model_id: str,
        model_version: str,
        horizon_id: str,
        predicted_class: str,
    ) -> None:
        self.prediction_id = prediction_id
        self.decision_id = decision_id
        self.model_id = model_id
        self.model_version = model_version
        self.horizon_id = horizon_id
        self.predicted_class = predicted_class


class _JoinedCycle:
    def __init__(
        self,
        *,
        net_pnl: float,
        gross_pnl: float,
        exit_at: datetime,
        side: str | None,
        predicted_class: str,
    ) -> None:
        self.net_pnl = net_pnl
        self.gross_pnl = gross_pnl
        self.exit_at = exit_at
        self.side = side
        self.predicted_class = predicted_class


__all__ = [
    "CLASSES",
    "DEFAULT_MINIMUM_MARKET_SAMPLES",
    "DEFAULT_MINIMUM_TRADER_CYCLES",
    "UNIFORM_BRIER",
    "evaluate_world_shadow_impact",
]
