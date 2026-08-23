"""Leakage-aware prequential metrics for immutable world-model shadow events.

The evaluator is deliberately market-only.  It can compare the categorical
baseline with the GRU challenger, but it must not turn fixed-horizon market
returns into Trader P&L: predictions are ``shadow_only`` and horizons may
overlap.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import math
from typing import Any

CLASSES = ("DOWN", "FLAT", "UP")
UNIFORM_BRIER = 2.0 / 3.0

# These are model identities, rather than display labels.  Callers can opt in
# to a different experiment identity through ``evaluate_shadow`` keyword
# arguments, but the production shadow pair should be comparable by default.
BASELINE_MODEL_ID = "hierarchical_dirichlet_world_baseline"
GRU_MODEL_ID = "online_gru_world_challenger"
DEFAULT_MINIMUM_PAIRED_SUPPORT = 20

DIRECTIONAL_SHADOW_DRAWDOWN_CAVEAT = (
    "This is an additive, one-unit directional market proxy from outcome "
    "simple_return. It is not portfolio PnL, has no costs/FX/position sizing, "
    "and fixed horizons may overlap."
)


MARKET_MODEL_VERSION = "v1"
CONTEXT_MODEL_VERSION = "context.v2"


@dataclass(frozen=True)
class _ScoredPrediction:
    """One already-validated, prequential prediction/outcome pairing."""

    episode_id: str
    horizon_id: str
    model_id: str
    model_version: str
    probabilities: tuple[float, float, float]
    target_index: int
    predicted_at: datetime
    ready_at: datetime
    simple_return: float | None
    market_anchor: tuple[str, str, str, str] | None = None
    context_status: str | None = None
    feature_contract_version: str | None = None
    label_evidence: tuple[Any, ...] = ()
    comparison_batch_id: str | None = None
    comparison_cohort_fingerprint: str | None = None
    training_cutoff: datetime | None = None

    @property
    def slot(self) -> tuple[str, str]:
        return (self.episode_id, self.horizon_id)

    @property
    def ablation_slot(self) -> tuple[str, str, str, str, str] | None:
        if self.market_anchor is None:
            return None
        return (*self.market_anchor, self.horizon_id)


def evaluate_shadow(
    predictions: Iterable[object],
    outcomes: Iterable[object],
    *,
    baseline_model_id: str = BASELINE_MODEL_ID,
    gru_model_id: str = GRU_MODEL_ID,
    minimum_paired_support: int = DEFAULT_MINIMUM_PAIRED_SUPPORT,
) -> dict[str, Any]:
    """Evaluate immutable forecasts and compare the baseline/GRU fairly.

    At most one prediction is scored for each
    ``(episode, horizon, model_id, model_version)``. Exact replay duplicates
    are collapsed; divergent duplicates are excluded as ambiguous rather than
    silently improving a model's apparent sample size. Corrected outcomes are
    treated with the same leaf-only rule as before.

    The GRU comparison is paired: each metric uses only slots for which both
    named model identities issued a causal prediction. Its deltas are withheld
    below ``minimum_paired_support`` so a handful of lucky forecasts cannot be
    presented as a challenger win.

    Causal eligibility requires both the frozen logical market cutoff
    (``predicted_at``) and the actual wall-clock availability
    (``ready_at``/``recorded_at``) to be strictly before the outcome label.
    The asynchronous worker makes the logical cutoff alone insufficient.
    """

    if isinstance(minimum_paired_support, bool) or not isinstance(minimum_paired_support, int):
        raise ValueError("minimum_paired_support must be a positive integer")
    if minimum_paired_support < 1:
        raise ValueError("minimum_paired_support must be a positive integer")
    baseline_model_id = _required_model_id(baseline_model_id, "baseline_model_id")
    gru_model_id = _required_model_id(gru_model_id, "gru_model_id")

    prediction_rows = list(predictions)
    outcome_rows = list(outcomes)
    selected_outcomes, ambiguous_outcomes = _select_outcomes(outcome_rows)
    grouped_scores: dict[tuple[str, str, str], list[_ScoredPrediction]] = defaultdict(list)
    excluded: defaultdict[str, int] = defaultdict(int)

    selected_predictions = _select_predictions(prediction_rows, excluded)
    for prediction, episode_id, horizon_id, model_id, model_version in selected_predictions:
        outcome_key = (episode_id, horizon_id)
        if outcome_key in ambiguous_outcomes:
            excluded["ambiguous_outcome_revision"] += 1
            continue
        outcome = selected_outcomes.get(outcome_key)
        if outcome is None:
            excluded["outcome_not_observed"] += 1
            continue
        probabilities = _probabilities(prediction)
        target_index = _target_index(outcome)
        if probabilities is None or target_index is None:
            excluded["invalid_score_payload"] += 1
            continue
        predicted_at = _logical_predicted_at(prediction)
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
        grouped_scores[(model_id, model_version, horizon_id)].append(
            _ScoredPrediction(
                episode_id=episode_id,
                horizon_id=horizon_id,
                model_id=model_id,
                model_version=model_version,
                probabilities=tuple(probabilities),
                target_index=target_index,
                predicted_at=predicted_at,
                ready_at=ready_at,
                simple_return=_simple_return(outcome),
                market_anchor=_market_anchor(prediction),
                context_status=_context_status(prediction),
                feature_contract_version=_feature_contract_version(prediction),
                label_evidence=_label_evidence(outcome, horizon_id),
                comparison_batch_id=_comparison_batch_id(prediction),
                comparison_cohort_fingerprint=_comparison_cohort_fingerprint(prediction),
                training_cutoff=_training_cutoff(prediction),
            )
        )

    groups: list[dict[str, Any]] = []
    for (model_id, model_version, horizon_id), rows in sorted(grouped_scores.items()):
        groups.append(
            {
                "model_id": model_id,
                "model_version": model_version,
                "horizon_id": horizon_id,
                **_metrics(rows),
            }
        )

    comparisons = _paired_comparisons(
        grouped_scores,
        baseline_model_id=baseline_model_id,
        gru_model_id=gru_model_id,
        minimum_paired_support=minimum_paired_support,
    )
    return {
        "schema_version": "world_shadow_evaluation.v2",
        "predictions_total": len(prediction_rows),
        "outcomes_total": len(outcome_rows),
        "matched": sum(group["matched"] for group in groups),
        "groups": groups,
        "comparison_minimum_paired_support": minimum_paired_support,
        "comparisons": comparisons,
        "excluded": dict(sorted(excluded.items())),
        "status": "ready" if groups else "warming_up",
        "context_ablation": evaluate_context_ablation_from_scores(
            grouped_scores,
            minimum_paired_support=minimum_paired_support,
            baseline_model_id=baseline_model_id,
            gru_model_id=gru_model_id,
        ),
    }


def evaluate_context_ablation(
    predictions: Iterable[object],
    outcomes: Iterable[object],
    *,
    minimum_paired_support: int = DEFAULT_MINIMUM_PAIRED_SUPPORT,
    baseline_model_id: str = BASELINE_MODEL_ID,
    gru_model_id: str = GRU_MODEL_ID,
) -> dict[str, Any]:
    """Public ablation entry that reuses the same prequential scoring path."""

    result = evaluate_shadow(
        predictions,
        outcomes,
        baseline_model_id=baseline_model_id,
        gru_model_id=gru_model_id,
        minimum_paired_support=minimum_paired_support,
    )
    ablation = result.get("context_ablation")
    return ablation if isinstance(ablation, dict) else _empty_ablation(minimum_paired_support)


def evaluate_context_ablation_from_scores(
    grouped_scores: Mapping[tuple[str, str, str], list[_ScoredPrediction]],
    *,
    minimum_paired_support: int,
    baseline_model_id: str = BASELINE_MODEL_ID,
    gru_model_id: str = GRU_MODEL_ID,
) -> dict[str, Any]:
    """Compare context V2 vs market V1 within each model family, not across families."""

    excluded: defaultdict[str, int] = defaultdict(int)
    coverage: defaultdict[str, int] = defaultdict(int)
    families: list[dict[str, Any]] = []
    for model_id, family_name in (
        (baseline_model_id, "markov"),
        (gru_model_id, "gru"),
    ):
        market_rows = [
            row
            for (row_model_id, model_version, _horizon_id), rows in grouped_scores.items()
            if row_model_id == model_id and model_version == MARKET_MODEL_VERSION
            for row in rows
        ]
        context_rows = [
            row
            for (row_model_id, model_version, _horizon_id), rows in grouped_scores.items()
            if row_model_id == model_id and model_version == CONTEXT_MODEL_VERSION
            for row in rows
        ]
        for row in context_rows:
            coverage[row.context_status or "unknown"] += 1
        by_horizon: dict[str, dict[str, list[_ScoredPrediction]]] = defaultdict(lambda: {"market": [], "context": []})
        for row in market_rows:
            by_horizon[row.horizon_id]["market"].append(row)
        for row in context_rows:
            by_horizon[row.horizon_id]["context"].append(row)
        for horizon_id in sorted(by_horizon):
            market_by_anchor, market_ambiguous = _group_ablation_anchors(by_horizon[horizon_id]["market"], excluded)
            context_by_anchor, context_ambiguous = _group_ablation_anchors(by_horizon[horizon_id]["context"], excluded)
            ambiguous_slots = market_ambiguous | context_ambiguous
            paired_market: list[_ScoredPrediction] = []
            paired_context: list[_ScoredPrediction] = []
            for slot in sorted(set(market_by_anchor).intersection(context_by_anchor) - ambiguous_slots):
                market_row = market_by_anchor[slot]
                context_row = context_by_anchor[slot]
                if not _market_lane_contract(market_row) or not _context_lane_contract(context_row):
                    excluded["feature_contract_mismatch"] += 1
                    continue
                if (
                    market_row.predicted_at != context_row.predicted_at
                    or not market_row.comparison_batch_id
                    or not context_row.comparison_batch_id
                    or market_row.comparison_batch_id != context_row.comparison_batch_id
                ):
                    excluded["asynchronous_lane_mismatch"] += 1
                    continue
                if (
                    market_row.training_cutoff != context_row.training_cutoff
                    or not market_row.comparison_cohort_fingerprint
                    or not context_row.comparison_cohort_fingerprint
                    or market_row.comparison_cohort_fingerprint != context_row.comparison_cohort_fingerprint
                ):
                    excluded["training_cohort_mismatch"] += 1
                    continue
                if market_row.target_index != context_row.target_index:
                    excluded["label_mismatch"] += 1
                    continue
                if market_row.label_evidence != context_row.label_evidence:
                    excluded["label_evidence_mismatch"] += 1
                    continue
                paired_market.append(market_row)
                paired_context.append(context_row)
            matched_pairs = len(paired_market)
            comparison: dict[str, Any] = {
                "model_family": family_name,
                "market_model_id": model_id,
                "market_model_version": MARKET_MODEL_VERSION,
                "context_model_id": model_id,
                "context_model_version": CONTEXT_MODEL_VERSION,
                "horizon_id": horizon_id,
                "matched_pairs": matched_pairs,
                "minimum_paired_support": minimum_paired_support,
                "primary_delta": "paired_multiclass_log_loss",
                "delta_semantics": (
                    "context_minus_market; lower is better for log_loss, brier, and ece_5_bins, "
                    "higher is better for accuracy. Not a causal or PnL claim."
                ),
                "causal_claim": False,
                "pnl_claim": False,
            }
            if matched_pairs < minimum_paired_support:
                comparison.update(
                    {
                        "status": "insufficient_support",
                        "market": None,
                        "context": None,
                        "context_minus_market": None,
                        "conclusion": None,
                    }
                )
            else:
                market_metrics = _comparison_metrics(_metrics(paired_market))
                context_metrics = _comparison_metrics(_metrics(paired_context))
                comparison.update(
                    {
                        "status": "ready",
                        "market": market_metrics,
                        "context": context_metrics,
                        "context_minus_market": {
                            name: round(float(context_metrics[name]) - float(market_metrics[name]), 8)
                            for name in market_metrics
                        },
                        "conclusion": None,
                    }
                )
            families.append(comparison)
    status = "not_applicable"
    if families:
        status = "ready" if any(item["status"] == "ready" for item in families) else "insufficient_support"
    return {
        "schema_version": "world_context_ablation.v1",
        "status": status,
        "minimum_paired_support": minimum_paired_support,
        "families": families,
        "coverage": dict(sorted(coverage.items())),
        "excluded": dict(sorted(excluded.items())),
        "causal_claim": False,
        "pnl_claim": False,
        "conclusion": None,
    }


def _group_ablation_anchors(
    rows: list[_ScoredPrediction],
    excluded: defaultdict[str, int],
) -> tuple[dict[tuple[str, str, str, str, str], _ScoredPrediction], set[tuple[str, str, str, str, str]]]:
    grouped: dict[tuple[str, str, str, str, str], list[_ScoredPrediction]] = defaultdict(list)
    for row in rows:
        slot = row.ablation_slot
        if slot is None:
            excluded["missing_market_anchor"] += 1
            continue
        grouped[slot].append(row)
    unique: dict[tuple[str, str, str, str, str], _ScoredPrediction] = {}
    ambiguous: set[tuple[str, str, str, str, str]] = set()
    for slot, items in grouped.items():
        if len(items) != 1:
            excluded["ambiguous_market_anchor"] += len(items)
            ambiguous.add(slot)
            continue
        unique[slot] = items[0]
    return unique, ambiguous


def _market_lane_contract(row: _ScoredPrediction) -> bool:
    return row.feature_contract_version == "market_ohlcv_causal.v1" and row.model_version == MARKET_MODEL_VERSION


def _context_lane_contract(row: _ScoredPrediction) -> bool:
    return row.feature_contract_version == "market_ohlcv_context.v2" and row.model_version == CONTEXT_MODEL_VERSION


def _label_evidence(outcome: object, horizon_id: str) -> tuple[Any, ...]:
    direction = (
        _text(_search_outcome_field(outcome, "direction")) or _text(_search_outcome_field(outcome, "move_class")) or ""
    ).upper() or None
    return (
        _text(_search_outcome_field(outcome, "target_at")),
        _text(_search_outcome_field(outcome, "source_raw_sha256")) or _text(_search_outcome_field(outcome, "source")),
        horizon_id,
        _text(_search_outcome_field(outcome, "status")),
        _search_outcome_field(outcome, "training_eligible"),
        direction,
        _simple_return(outcome),
    )


def _empty_ablation(minimum_paired_support: int) -> dict[str, Any]:
    return {
        "schema_version": "world_context_ablation.v1",
        "status": "not_applicable",
        "minimum_paired_support": minimum_paired_support,
        "families": [],
        "coverage": {},
        "excluded": {},
        "causal_claim": False,
        "pnl_claim": False,
        "conclusion": None,
    }


def _prediction_envelopes(prediction: object) -> tuple[object, ...]:
    envelopes: list[object] = [prediction]
    for name in ("prediction_record", "prediction", "payload"):
        nested = _field(prediction, name)
        if nested is not None and nested is not prediction:
            envelopes.append(nested)
            if isinstance(nested, Mapping):
                inner = nested.get("prediction")
                if isinstance(inner, Mapping) and inner is not nested:
                    envelopes.append(inner)
    return tuple(envelopes)


def _observation_payload(prediction: object) -> Mapping[str, Any] | None:
    for envelope in _prediction_envelopes(prediction):
        for name in ("input", "observation"):
            raw = _field(envelope, name)
            if isinstance(raw, Mapping):
                nested = raw.get("observation")
                if isinstance(nested, Mapping):
                    return nested
                return raw
    return None


def _market_anchor(prediction: object) -> tuple[str, str, str, str] | None:
    observation = _observation_payload(prediction)
    venue = _text(_field(observation, "venue") if observation is not None else None) or _text(
        _field(prediction, "venue")
    )
    symbol = _text(_field(observation, "symbol") if observation is not None else None) or _text(
        _field(prediction, "symbol")
    )
    interval = (
        _text(_field(observation, "bar_interval") if observation is not None else None)
        or _text(_field(observation, "interval") if observation is not None else None)
        or _text(_field(prediction, "bar_interval"))
    )
    as_of = _text(_field(observation, "as_of_bar_ts") if observation is not None else None) or _text(
        _field(prediction, "as_of_bar_ts")
    )
    if venue is None or symbol is None or interval is None or as_of is None:
        return None
    return (venue, symbol, interval, as_of)


def _context_status(prediction: object) -> str | None:
    observation = _observation_payload(prediction)
    if observation is None:
        return _text(_field(prediction, "context_status"))
    context = observation.get("context")
    if isinstance(context, Mapping):
        return _text(context.get("status"))
    return _text(_field(prediction, "context_status"))


def _feature_contract_version(prediction: object) -> str | None:
    observation = _observation_payload(prediction)
    if observation is not None:
        version = _text(observation.get("feature_contract_version"))
        if version is not None:
            return version
    return _text(_field(prediction, "feature_contract_version"))


def _select_predictions(
    predictions: list[object],
    excluded: defaultdict[str, int],
) -> list[tuple[object, str, str, str, str]]:
    """Select one exact prediction replay per immutable model-slot identity."""

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
    """Fingerprint only fields that make a scored forecast meaningfully distinct."""

    probabilities = _probabilities(prediction)
    predicted_at = _logical_predicted_at(prediction)
    ready_at = _prediction_ready_at(prediction)
    model_payload = _prediction_payload(prediction)
    payload = {
        "prediction_id": _prediction_id(prediction),
        "probabilities": probabilities,
        "predicted_class": _predicted_class(probabilities),
        "predicted_at": None if predicted_at is None else predicted_at.isoformat(),
        "ready_at": None if ready_at is None else ready_at.isoformat(),
        "training_cutoff": _text_from_payload(prediction, model_payload, "training_cutoff"),
        "model_fingerprint": _text_from_payload(prediction, model_payload, "model_fingerprint"),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _paired_comparisons(
    grouped_scores: Mapping[tuple[str, str, str], list[_ScoredPrediction]],
    *,
    baseline_model_id: str,
    gru_model_id: str,
    minimum_paired_support: int,
) -> list[dict[str, Any]]:
    """Return paired GRU-vs-baseline comparisons, separated by horizon/version."""

    baseline_groups = [
        (model_version, horizon_id, rows)
        for (model_id, model_version, horizon_id), rows in grouped_scores.items()
        if model_id == baseline_model_id
    ]
    gru_groups = [
        (model_version, horizon_id, rows)
        for (model_id, model_version, horizon_id), rows in grouped_scores.items()
        if model_id == gru_model_id
    ]
    comparisons: list[dict[str, Any]] = []
    for baseline_version, horizon_id, baseline_rows in sorted(baseline_groups):
        baseline_by_slot = {row.slot: row for row in baseline_rows}
        for gru_version, gru_horizon_id, gru_rows in sorted(gru_groups):
            if gru_horizon_id != horizon_id:
                continue
            gru_by_slot = {row.slot: row for row in gru_rows}
            paired_slots = sorted(set(baseline_by_slot).intersection(gru_by_slot))
            paired_baseline = [baseline_by_slot[slot] for slot in paired_slots]
            paired_gru = [gru_by_slot[slot] for slot in paired_slots]
            matched_pairs = len(paired_slots)
            comparison: dict[str, Any] = {
                "baseline_model_id": baseline_model_id,
                "baseline_model_version": baseline_version,
                "gru_model_id": gru_model_id,
                "gru_model_version": gru_version,
                "horizon_id": horizon_id,
                "matched_pairs": matched_pairs,
                "minimum_paired_support": minimum_paired_support,
                "delta_semantics": (
                    "gru_minus_baseline; lower is better for brier, log_loss, and ece_5_bins, "
                    "higher is better for accuracy"
                ),
            }
            if matched_pairs < minimum_paired_support:
                comparison.update(
                    {
                        "status": "insufficient_support",
                        "baseline": None,
                        "gru": None,
                        "gru_minus_baseline": None,
                    }
                )
            else:
                baseline_metrics = _comparison_metrics(_metrics(paired_baseline))
                gru_metrics = _comparison_metrics(_metrics(paired_gru))
                comparison.update(
                    {
                        "status": "ready",
                        "baseline": baseline_metrics,
                        "gru": gru_metrics,
                        "gru_minus_baseline": {
                            name: round(float(gru_metrics[name]) - float(baseline_metrics[name]), 8)
                            for name in baseline_metrics
                        },
                    }
                )
            comparisons.append(comparison)
    return comparisons


def _comparison_metrics(metrics: Mapping[str, Any]) -> dict[str, float]:
    return {name: float(metrics[name]) for name in ("brier", "log_loss", "accuracy", "ece_5_bins")}


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
        if _text(_field(outcome, "status")) != "observed":
            continue
        eligible = _field(outcome, "training_eligible")
        if eligible is False:
            continue
        event_id = _text(
            _field(
                outcome,
                "event_id",
                _field(outcome, "outcome_event_id", _field(outcome, "outcome_id")),
            )
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


def _metrics(rows: list[_ScoredPrediction]) -> dict[str, Any]:
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
        "matched": count,
        "brier": round(brier, 8),
        "uniform_brier": round(UNIFORM_BRIER, 8),
        "brier_skill_vs_uniform": round(1.0 - brier / UNIFORM_BRIER, 8),
        "log_loss": round(log_loss_sum / count, 8),
        "accuracy": round(correct / count, 8),
        "ece_5_bins": round(ece, 8),
        "directional_shadow_non_portfolio_drawdown": _directional_shadow_drawdown(rows),
    }


def _directional_shadow_drawdown(rows: list[_ScoredPrediction]) -> dict[str, Any]:
    """Describe directional market returns without claiming a portfolio curve."""

    return_rows: list[tuple[datetime, float]] = []
    missing_simple_return = 0
    for row in rows:
        if row.simple_return is None:
            missing_simple_return += 1
            continue
        direction = _direction_multiplier(CLASSES[_predicted_index(row.probabilities)])
        return_rows.append((row.ready_at, direction * row.simple_return))
    if not return_rows:
        return {
            "metric": "directional_shadow_non_portfolio_drawdown",
            "status": "unavailable",
            "matched_simple_returns": 0,
            "missing_simple_return": missing_simple_return,
            "cumulative_directional_simple_return": None,
            "mean_directional_simple_return": None,
            "max_drawdown": None,
            "unit": "simple_return_per_one_unit_directional_forecast",
            "non_portfolio": True,
            "horizons_may_overlap": True,
            "caveat": DIRECTIONAL_SHADOW_DRAWDOWN_CAVEAT,
        }
    returns = [value for _timestamp, value in sorted(return_rows, key=lambda item: item[0])]
    return {
        "metric": "directional_shadow_non_portfolio_drawdown",
        "status": "available",
        "matched_simple_returns": len(returns),
        "missing_simple_return": missing_simple_return,
        "cumulative_directional_simple_return": round(sum(returns), 8),
        "mean_directional_simple_return": round(sum(returns) / len(returns), 8),
        "max_drawdown": round(_additive_drawdown(returns), 8),
        "unit": "simple_return_per_one_unit_directional_forecast",
        "non_portfolio": True,
        "horizons_may_overlap": True,
        "caveat": DIRECTIONAL_SHADOW_DRAWDOWN_CAVEAT,
    }


def _additive_drawdown(values: list[float]) -> float:
    running = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        maximum = max(maximum, peak - running)
    return maximum


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
    raw = _search_outcome_field(outcome, "direction", "move_class")
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


def _model_identity(value: object) -> tuple[str, str]:
    payload = _prediction_payload(value)
    model_id = (
        _text(_field(value, "model_id"))
        or _text(_field(value, "model_kind"))
        or _text(_field(payload, "model_id"))
        or _text(_field(payload, "model_kind"))
        or "unknown-model"
    )
    model_version = (
        _text(_field(value, "model_version")) or _text(_field(payload, "model_version")) or "unknown-version"
    )
    return model_id, model_version


def _prediction_payload(value: object) -> object | None:
    for name in ("prediction", "prediction_record", "payload"):
        payload = _field(value, name)
        if payload is not None and payload is not value:
            return payload
    return None


def _prediction_id(value: object) -> str | None:
    direct = _text(_field(value, "prediction_id", _field(value, "id")))
    if direct is not None:
        return direct
    payload = _prediction_payload(value)
    return _text(_field(payload, "prediction_id", _field(payload, "id")))


def _text_from_payload(value: object, payload: object | None, name: str) -> str | None:
    return _text(_field(value, name)) or _text(_field(payload, name))


def _predicted_index(probabilities: tuple[float, float, float] | list[float]) -> int:
    highest = max(probabilities)
    return (
        CLASSES.index("FLAT")
        if probabilities[CLASSES.index("FLAT")] == highest
        else max(range(len(CLASSES)), key=probabilities.__getitem__)
    )


def _predicted_class(probabilities: list[float] | None) -> str | None:
    return None if probabilities is None else CLASSES[_predicted_index(probabilities)]


def _simple_return(outcome: object) -> float | None:
    for name in ("simple_return", "forward_return"):
        candidate = _outcome_value(outcome, name)
        parsed = _finite_float(candidate)
        if parsed is not None:
            return parsed
    anchor_close = _finite_float(_outcome_value(outcome, "anchor_close"))
    endpoint_close = _finite_float(_outcome_value(outcome, "endpoint_close"))
    if anchor_close is None or endpoint_close is None or anchor_close <= 0.0:
        return None
    return endpoint_close / anchor_close - 1.0


def _outcome_value(outcome: object, name: str) -> object:
    found = _search_outcome_field(outcome, name)
    return None if found is None else found


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _direction_multiplier(predicted_class: str) -> float:
    return {"DOWN": -1.0, "FLAT": 0.0, "UP": 1.0}[predicted_class]


def _logical_predicted_at(prediction: object) -> datetime | None:
    """Return the frozen market cutoff, never a later persistence timestamp."""

    for envelope in _prediction_envelopes(prediction):
        parsed = _event_time(envelope, "predicted_at", "created_at")
        if parsed is not None:
            return parsed
    return None


def _prediction_ready_at(prediction: object) -> datetime | None:
    """Return evidence of real wall-clock availability for a prediction."""

    for envelope in _prediction_envelopes(prediction):
        parsed = _event_time(envelope, "ready_at", "recorded_at")
        if parsed is not None:
            return parsed
    return None


def _comparison_batch_id(prediction: object) -> str | None:
    for envelope in _prediction_envelopes(prediction):
        text = _text(_field(envelope, "comparison_batch_id"))
        if text is not None:
            return text
    return None


def _comparison_cohort_fingerprint(prediction: object) -> str | None:
    for envelope in _prediction_envelopes(prediction):
        text = _text(_field(envelope, "comparison_cohort_fingerprint"))
        if text is not None:
            return text
    return None


def _training_cutoff(prediction: object) -> datetime | None:
    for envelope in _prediction_envelopes(prediction):
        raw = _field(envelope, "training_cutoff")
        if raw in (None, ""):
            continue
        parsed = _event_time({"training_cutoff": raw}, "training_cutoff")
        if parsed is not None:
            return parsed
    return None


def _search_outcome_field(outcome: object, *names: str) -> Any:
    nested_names = ("outcome", "label", "evidence", "payload", "target_bar")
    stack: list[object] = [outcome]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        for name in names:
            found = _field(current, name)
            if found is not None and found != "":
                return found
        if isinstance(current, Mapping):
            for nested_name in nested_names:
                nested = current.get(nested_name)
                if isinstance(nested, Mapping):
                    stack.append(nested)
            continue
        for nested_name in nested_names:
            nested = getattr(current, nested_name, None)
            if nested is not None and nested is not current:
                stack.append(nested)
    return None


def _event_time(value: object, *names: str) -> datetime | None:
    for name in names:
        raw = _field(value, name)
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else None
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return parsed
    return None


def _required_model_id(value: object, name: str) -> str:
    normalized = _text(value)
    if normalized is None:
        raise ValueError(f"{name} must be a non-empty string")
    return normalized


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = [
    "BASELINE_MODEL_ID",
    "CLASSES",
    "DEFAULT_MINIMUM_PAIRED_SUPPORT",
    "DIRECTIONAL_SHADOW_DRAWDOWN_CAVEAT",
    "GRU_MODEL_ID",
    "UNIFORM_BRIER",
    "CONTEXT_MODEL_VERSION",
    "MARKET_MODEL_VERSION",
    "evaluate_context_ablation",
    "evaluate_shadow",
]
