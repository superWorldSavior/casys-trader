"""Numeric confidence calibration over realised, decision-linked round trips."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    compute_round_trips,
)

_UNKNOWN = "unknown"
_LEVELS = (
    ("setup", "regime", "side", "horizon"),
    ("setup", "side", "horizon"),
    ("side", "horizon"),
    ("side",),
    (),
)


def _read_decision_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "decisions.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _text(value: object) -> str:
    parsed = str(value or "").strip()
    return parsed if parsed else _UNKNOWN


def _dimensions(row: Mapping[str, object] | None, *, side: object) -> dict[str, str]:
    source = row or {}
    stored = source.get("entry_dimensions")
    stored = stored if isinstance(stored, Mapping) else {}
    thesis = source.get("thesis")
    thesis = thesis if isinstance(thesis, Mapping) else {}
    return {
        "setup": _text(stored.get("setup") or thesis.get("setup")),
        "regime": _text(stored.get("regime")),
        "side": _text(stored.get("side") or side).lower(),
        "horizon": _text(stored.get("horizon") or thesis.get("horizon")),
    }


def _joined_dimensions(
    trip: Mapping[str, object],
    decisions_by_id: Mapping[str, Mapping[str, object]],
) -> dict[str, str]:
    ids = trip.get("entry_decision_ids")
    decision_ids = (
        [str(value) for value in ids if str(value)]
        if isinstance(ids, list)
        else []
    )
    if not decision_ids and trip.get("entry_decision_id"):
        decision_ids = [str(trip["entry_decision_id"])]
    joined = [
        _dimensions(decisions_by_id.get(decision_id), side=trip.get("side"))
        for decision_id in decision_ids
    ]
    if not joined:
        return _dimensions(None, side=trip.get("side"))
    result: dict[str, str] = {}
    for name in ("setup", "regime", "side", "horizon"):
        values = {item[name] for item in joined}
        result[name] = values.pop() if len(values) == 1 else _UNKNOWN
    return result


def _wilson_interval(wins: int, n: int, *, z: float = 1.96) -> list[float | None]:
    if n <= 0:
        return [None, None]
    probability = wins / n
    denominator = 1.0 + (z * z / n)
    centre = probability + (z * z / (2.0 * n))
    margin = z * math.sqrt(
        probability * (1.0 - probability) / n + z * z / (4.0 * n * n)
    )
    return [
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    ]


def _cohort_key(
    dimensions: Mapping[str, str],
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(dimensions[field] for field in fields)


def _metric(
    observations: Iterable[Mapping[str, object]],
    *,
    level: tuple[str, ...],
    key: tuple[str, ...],
) -> dict:
    rows = list(observations)
    confidences = [float(row["confidence"]) for row in rows]
    pnls = [float(row["pnl"]) for row in rows]
    wins = sum(pnl > 0.0 for pnl in pnls)
    n = len(rows)
    mean_confidence = sum(confidences) / n
    win_rate = wins / n
    return {
        "level": "+".join(level) if level else "all",
        "dimensions": dict(zip(level, key, strict=True)),
        "n": n,
        "mean_confidence": mean_confidence,
        "win_rate": win_rate,
        "calibration_gap": win_rate - mean_confidence,
        "avg_pnl": sum(pnls) / n,
        "win_rate_interval_95": _wilson_interval(wins, n),
    }


def build_confidence_calibration_from_rows(
    trips: Iterable[Mapping[str, object]],
    decision_rows: Iterable[Mapping[str, object]],
    *,
    min_cohort_size: int = 20,
) -> dict:
    """Build calibrated cohorts without inferring historical entry regimes."""

    if min_cohort_size <= 0:
        raise ValueError("min_cohort_size must be positive")
    decisions_by_id = {
        str(row.get("decision_id")): row
        for row in decision_rows
        if row.get("decision_id")
    }
    observations: list[dict] = []
    for trip in aggregate_position_cycles(trips):
        confidence = trip.get("entry_confidence")
        pnl = trip.get("pnl")
        if not isinstance(confidence, (int, float)) or not isinstance(
            pnl, (int, float)
        ):
            continue
        if not math.isfinite(float(confidence)) or not math.isfinite(float(pnl)):
            continue
        if not 0.0 <= float(confidence) <= 1.0:
            continue
        observations.append(
            {
                "confidence": float(confidence),
                "pnl": float(pnl),
                "dimensions": _joined_dimensions(trip, decisions_by_id),
            }
        )

    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict]] = defaultdict(list)
    for observation in observations:
        dimensions = observation["dimensions"]
        for level in _LEVELS:
            grouped[(level, _cohort_key(dimensions, level))].append(observation)

    metrics = {
        group: _metric(rows, level=group[0], key=group[1])
        for group, rows in grouped.items()
    }
    fine_level = _LEVELS[0]
    assignments: list[dict] = []
    fine_groups = sorted(
        (group for group in metrics if group[0] == fine_level),
        key=lambda group: group[1],
    )
    for _, fine_key in fine_groups:
        dimensions = dict(zip(fine_level, fine_key, strict=True))
        selected = None
        for level in _LEVELS:
            group = (level, _cohort_key(dimensions, level))
            metric = metrics.get(group)
            if metric is not None and (
                metric["n"] >= min_cohort_size or level == ()
            ):
                selected = metric
                break
        assignments.append(
            {
                "requested": dimensions,
                "selected_level": None if selected is None else selected["level"],
                "selected": selected,
            }
        )

    ordered_metrics = sorted(
        metrics.values(),
        key=lambda item: (
            _LEVELS.index(
                () if item["level"] == "all" else tuple(item["level"].split("+"))
            ),
            tuple(item["dimensions"].values()),
        ),
    )
    return {
        "n": len(observations),
        "min_cohort_size": min_cohort_size,
        "cohorts": ordered_metrics,
        "fallbacks": assignments,
        "policy": {
            "levels": ["+".join(level) if level else "all" for level in _LEVELS],
            "historical_missing_dimensions": _UNKNOWN,
            "outcome": "flat_to_flat_net_pnl_positive",
        },
    }


def build_confidence_calibration(
    state_dir: str | Path,
    *,
    min_cohort_size: int = 20,
) -> dict:
    state_path = Path(state_dir)
    return build_confidence_calibration_from_rows(
        compute_round_trips(state_path),
        _read_decision_rows(state_path),
        min_cohort_size=min_cohort_size,
    )


def compact_confidence_calibration(
    calibration: Mapping[str, object],
    *,
    max_cohorts: int = 8,
) -> dict:
    """Project a bounded digest suitable for push context."""

    cohorts = calibration.get("cohorts")
    cohort_rows = [row for row in cohorts if isinstance(row, dict)] if isinstance(cohorts, list) else []
    selected = sorted(
        cohort_rows,
        key=lambda row: (-int(row.get("n") or 0), str(row.get("level") or "")),
    )[: max(0, max_cohorts)]
    return {
        "n": int(calibration.get("n") or 0),
        "min_cohort_size": calibration.get("min_cohort_size"),
        "cohorts": selected,
        "detail_scope": "confidence_calibration",
    }


__all__ = [
    "build_confidence_calibration",
    "build_confidence_calibration_from_rows",
    "compact_confidence_calibration",
]
