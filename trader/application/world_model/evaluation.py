"""Leakage-aware prequential metrics for immutable world-model shadow events."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

CLASSES = ("DOWN", "FLAT", "UP")
UNIFORM_BRIER = 2.0 / 3.0


def evaluate_shadow(
    predictions: Iterable[object],
    outcomes: Iterable[object],
) -> dict[str, Any]:
    """Evaluate predictions which were durably made before their outcome.

    Events may be mappings or frozen domain objects. Corrected outcomes remain
    append-only: only one unambiguous, non-superseded observed outcome is scored
    for an ``(episode, horizon)`` pair.
    """

    prediction_rows = list(predictions)
    outcome_rows = list(outcomes)
    selected_outcomes, ambiguous_outcomes = _select_outcomes(outcome_rows)
    grouped_scores: dict[tuple[str, str, str], list[tuple[list[float], int]]] = defaultdict(list)
    excluded = defaultdict(int)

    for prediction in prediction_rows:
        episode_id = _text(_field(prediction, "episode_id"))
        horizon_id = _horizon_id(prediction)
        if episode_id is None or horizon_id is None:
            excluded["invalid_prediction_identity"] += 1
            continue
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
        predicted_at = _event_time(prediction, "created_at", "predicted_at", "recorded_at")
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
            or outcome_available_at is None
            or predicted_at >= outcome_available_at
        ):
            excluded["causal_order_unproven"] += 1
            continue
        model_payload = _field(prediction, "prediction")
        model_id = (
            _text(_field(prediction, "model_id"))
            or _text(_field(prediction, "model_kind"))
            or _text(_field(model_payload, "model_id"))
            or "unknown-model"
        )
        model_version = (
            _text(_field(prediction, "model_version"))
            or _text(_field(model_payload, "model_version"))
            or "unknown-version"
        )
        grouped_scores[(model_id, model_version, horizon_id)].append(
            (probabilities, target_index)
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

    return {
        "schema_version": "world_shadow_evaluation.v1",
        "predictions_total": len(prediction_rows),
        "outcomes_total": len(outcome_rows),
        "matched": sum(group["matched"] for group in groups),
        "groups": groups,
        "excluded": dict(sorted(excluded.items())),
        "status": "ready" if groups else "warming_up",
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


def _metrics(rows: list[tuple[list[float], int]]) -> dict[str, Any]:
    brier_sum = 0.0
    log_loss_sum = 0.0
    correct = 0
    calibration_bins: list[list[float]] = [[] for _ in range(5)]
    calibration_hits: list[list[float]] = [[] for _ in range(5)]
    for probabilities, target_index in rows:
        brier_sum += sum(
            (probability - (1.0 if index == target_index else 0.0)) ** 2
            for index, probability in enumerate(probabilities)
        )
        log_loss_sum += -math.log(max(probabilities[target_index], 1e-15))
        highest = max(probabilities)
        predicted_index = (
            CLASSES.index("FLAT")
            if probabilities[CLASSES.index("FLAT")] == highest
            else max(range(len(CLASSES)), key=probabilities.__getitem__)
        )
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
        * abs(
            sum(confidences) / len(confidences)
            - sum(calibration_hits[index]) / len(calibration_hits[index])
        )
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
    }


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
            raw = (
                _field(payload, "direction", _field(payload, "move_class"))
                if payload is not None
                else None
            )
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
        return _text(raw.get("id"))
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


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["CLASSES", "UNIFORM_BRIER", "evaluate_shadow"]
