"""Reconstructible World cohort report: matched sets, contrasts, gates, claims.

The report is a read model. It never creates, migrates, or writes ``world_model.db``.
Authority stays ``shadow_only`` with ``decision_effect=none`` and ``recommendation=NO_GO``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import math
import random

from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    COHORT_TRADER_CONTRIBUTION,
    WORLD_COHORT_REPORT_SCHEMA,
    CohortPhase,
    SupportGateMode,
    WorldCohort,
    WorldCohortSlot,
)
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.infrastructure.state_db.world_model_query import read_world_cohort_ledger
from trader.reporting.read_models.world_prediction_integrity import contains_forbidden_trader_feature

CLASSES = ("DOWN", "FLAT", "UP")
_ONE_WEEK = timedelta(days=8)
_PAIRING_FIELDS = (
    ("study_cohort_id", "study_cohort_id_mismatch"),
    ("manifest_sha256", "manifest_sha256_mismatch"),
    ("comparison_batch_id", "comparison_batch_id_mismatch"),
    ("predicted_at", "predicted_at_mismatch"),
    ("training_cutoff", "training_cutoff_mismatch"),
    ("training_lineage", "training_lineage_mismatch"),
    ("label_move_class", "label_move_class_mismatch"),
    ("label_target_at", "label_target_at_mismatch"),
    ("label_evidence_digest", "label_evidence_digest_mismatch"),
)
_TERMINAL = frozenset({"observed", "missing", "unknown"})


@dataclass(frozen=True)
class _ScoredLane:
    lane_id: str
    study_cohort_id: str
    manifest_sha256: str
    venue: str
    symbol: str
    bar_interval: str
    as_of_bar_ts: datetime
    horizon_id: str
    comparison_batch_id: str
    predicted_at: datetime
    training_cutoff: datetime | None
    training_lineage: str
    label_move_class: str
    label_target_at: datetime | None
    label_evidence_digest: str
    probabilities: tuple[float, float, float]
    target_index: int
    ready_at: datetime
    simple_return: float | None
    prediction_id: str


def read_world_cohort_report(state_dir: str | Path, cohort_id: str) -> dict[str, Any]:
    """Project a reconstructible cohort report without creating the ledger."""

    db_path = Path(state_dir) / "world_model.db"
    ledger = read_world_cohort_ledger(db_path, cohort_id)
    return project_world_cohort_report(ledger, db_path=db_path)


def project_world_cohort_report(
    ledger: Mapping[str, Any],
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Rebuild ``world_cohort_report.v1`` from a read-only ledger snapshot."""

    path = None if db_path is None else str(db_path)
    cohort_id = str(ledger.get("cohort_id") or "")
    report = _bounded_base(path=path, cohort_id=cohort_id)
    status = str(ledger.get("status") or "unavailable")
    report["exists"] = bool(ledger.get("exists"))
    report["status"] = status
    if "missing_tables" in ledger:
        report["missing_tables"] = list(ledger["missing_tables"])
    if "error" in ledger:
        report["error"] = ledger["error"]
    if status != "loaded":
        return report

    try:
        cohort = WorldCohort.reconstruct(ledger.get("manifest") or {}, ledger.get("events") or ())
    except (TypeError, ValueError) as exc:
        report["status"] = "unavailable"
        report["error"] = f"{type(exc).__name__}:{exc}"
        return report

    report.update(
        {
            "cohort_id": cohort.cohort_id,
            "manifest_sha256": cohort.manifest.manifest_sha256,
            "phase": cohort.phase.value,
            "study_kind": cohort.manifest.study_kind.value,
            "lifecycle": {
                "phase": cohort.phase.value,
                "event_types": [event.event_type for event in cohort.events],
                "started_event_id": None if cohort.started_event is None else cohort.started_event.event_id,
            },
            "expected_lanes": [lane.lane_id for lane in cohort.manifest.lanes],
        }
    )
    episodes = list(ledger.get("episodes") or ())
    outcomes = list(ledger.get("outcomes") or ())
    predictions = list(ledger.get("predictions") or ())
    episode_by_id = _index_episodes(episodes)
    outcome_by_key = _active_outcomes(outcomes)
    admitted = {slot.slot_id: slot for slot in cohort.admitted_slots}
    admitted_keys = {_slot_key_from_slot(slot) for slot in cohort.admitted_slots}
    integrity_failures: list[str] = []
    excluded: defaultdict[str, int] = defaultdict(int)
    scored, observed_lanes = _score_predictions(
        predictions,
        cohort=cohort,
        episode_by_id=episode_by_id,
        outcome_by_key=outcome_by_key,
        admitted_keys=admitted_keys,
        excluded=excluded,
        integrity_failures=integrity_failures,
    )
    matched_sets, contrast_rows = _contrasts(
        scored,
        cohort=cohort,
        excluded=excluded,
        integrity_ok=not integrity_failures,
    )
    span = _collection_span(cohort, scored)
    one_week = span is None or span < _ONE_WEEK
    completion = _verify_completion_evidence(
        cohort,
        episodes=episode_by_id,
        outcomes=outcome_by_key,
        slots=admitted,
    )
    if cohort.phase is CohortPhase.COMPLETE and not completion["verified"]:
        integrity_failures.append("completion_evidence_mismatch")
    integrity_passed = not integrity_failures
    if not integrity_passed:
        for row in contrast_rows:
            row["winner"] = False
            if row["conclusion"] == "predictive_lift_observed":
                row["conclusion"] = "inconclusive"
    if cohort.phase is CohortPhase.INVALIDATED:
        for row in contrast_rows:
            row["conclusion"] = "invalidated"
            row["winner"] = False
    report.update(
        {
            "observed_lanes": observed_lanes,
            "matched_sets": matched_sets,
            "contrasts": contrast_rows,
            "exclusions": dict(sorted(excluded.items())),
            "support": _support_summary(cohort, contrast_rows),
            "sensor_coverage": _sensor_coverage(cohort, scored),
            "runtime_gaps": _runtime_gaps(cohort, scored),
            "gates": {
                "integrity": {"passed": integrity_passed, "failures": integrity_failures},
                "support": _support_summary(cohort, contrast_rows),
            },
            "completion_evidence": completion,
            "negative_controls": {
                "status": "offline_only",
                "note": "temporal and entity permutations are versioned offline analyses and never train live lanes",
            },
            "interpretation_limit": "coverage_plumbing_preliminary_trends_only" if one_week else None,
            "claims_limits": {
                **_CLAIM_FIELDS,
                "one_week_is_coverage_plumbing_preliminary_trends_only": one_week,
            },
            "conclusion": _report_conclusion(cohort, contrast_rows, integrity_passed),
        }
    )
    return report


def world_cohort_horizon_leaf_digest(
    *,
    slot_id: str,
    horizon_id: str,
    status: str,
    outcome_event_id: str,
    evidence_sha256: str,
    move_class: str,
) -> str:
    """Return the reconstructible digest of one completion-evidence horizon leaf."""

    return canonical_sha256(
        {
            "slot_id": slot_id,
            "horizon_id": horizon_id,
            "status": status,
            "outcome_event_id": outcome_event_id,
            "evidence_sha256": evidence_sha256,
            "move_class": move_class,
        }
    )


_CLAIM_FIELDS = {
    "authority": COHORT_AUTHORITY,
    "decision_effect": COHORT_DECISION_EFFECT,
    "recommendation": COHORT_RECOMMENDATION,
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": COHORT_TRADER_CONTRIBUTION,
    "winner": False,
}


def _bounded_base(*, path: str | None, cohort_id: str) -> dict[str, Any]:
    return {
        "schema_version": WORLD_COHORT_REPORT_SCHEMA,
        "db_path": path,
        "cohort_id": cohort_id,
        "status": "unavailable",
        "exists": False,
        "matched_sets": [],
        "contrasts": [],
        "exclusions": {},
        **_CLAIM_FIELDS,
    }


def _index_episodes(rows: Sequence[object]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        episode_id = _text(row.get("episode_id"))
        if episode_id is not None:
            indexed[episode_id] = row
    return indexed


def _active_outcomes(rows: Sequence[object]) -> dict[tuple[str, str], Mapping[str, Any]]:
    superseded = {
        _text(item.get("supersedes_outcome_event_id") or item.get("supersedes_event_id"))
        for item in rows
        if isinstance(item, Mapping)
    }
    superseded.discard(None)
    selected: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        event_id = _text(row.get("outcome_event_id") or row.get("event_id"))
        if event_id is not None and event_id in superseded:
            continue
        episode_id = _text(row.get("episode_id"))
        horizon_id = _text(row.get("horizon_id") or row.get("horizon_code"))
        if episode_id is None or horizon_id is None:
            continue
        selected[(episode_id, horizon_id)] = row
    return selected


def _score_predictions(
    predictions: Sequence[object],
    *,
    cohort: WorldCohort,
    episode_by_id: Mapping[str, Mapping[str, Any]],
    outcome_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    admitted_keys: set[tuple[str, str, str, datetime]],
    excluded: defaultdict[str, int],
    integrity_failures: list[str],
) -> tuple[list[_ScoredLane], list[str]]:
    scored: list[_ScoredLane] = []
    observed: set[str] = set()
    for prediction in predictions:
        if not isinstance(prediction, Mapping):
            excluded["invalid_prediction_identity"] += 1
            continue
        if contains_forbidden_trader_feature(prediction):
            integrity_failures.append("trader_feature_key")
        lane_id = _text(prediction.get("lane_id"))
        episode_id = _text(prediction.get("episode_id"))
        horizon_id = _text(prediction.get("horizon_id") or prediction.get("horizon_code"))
        if lane_id is None or episode_id is None or horizon_id is None:
            excluded["invalid_prediction_identity"] += 1
            continue
        observed.add(lane_id)
        slot = _market_slot(prediction, episode_by_id.get(episode_id))
        study_id = _text(prediction.get("study_cohort_id")) or ""
        if slot is None:
            excluded["missing_market_anchor"] += 1
            continue
        if study_id != cohort.cohort_id and slot not in admitted_keys:
            continue
        probabilities = _probabilities(prediction)
        if probabilities is None:
            excluded["invalid_score_payload"] += 1
            continue
        outcome = outcome_by_key.get((episode_id, horizon_id))
        if outcome is None or str(outcome.get("status") or "").lower() != "observed":
            excluded["outcome_not_observed"] += 1
            continue
        if outcome.get("training_eligible") is False:
            excluded["outcome_not_observed"] += 1
            continue
        target_index = _target_index(outcome)
        if target_index is None:
            excluded["invalid_score_payload"] += 1
            continue
        predicted_at = _timestamp(prediction, "predicted_at", "created_at")
        ready_at = _timestamp(prediction, "ready_at", "recorded_at")
        available_at = _timestamp(outcome, "label_available_at", "available_at", "sealed_at")
        if (
            predicted_at is None
            or ready_at is None
            or available_at is None
            or predicted_at >= available_at
            or ready_at >= available_at
        ):
            excluded["causal_order_unproven"] += 1
            continue
        move_class = CLASSES[target_index]
        evidence_digest = _evidence_digest(outcome)
        target_at = _timestamp(outcome, "target_at") or _nested_timestamp(outcome, "label", "target_at")
        scored.append(
            _ScoredLane(
                lane_id=lane_id,
                study_cohort_id=study_id,
                manifest_sha256=_text(prediction.get("manifest_sha256")) or "",
                venue=slot[0],
                symbol=slot[1],
                bar_interval=slot[2],
                as_of_bar_ts=slot[3],
                horizon_id=horizon_id,
                comparison_batch_id=_text(prediction.get("comparison_batch_id")) or "",
                predicted_at=predicted_at,
                training_cutoff=_timestamp(prediction, "training_cutoff"),
                training_lineage=_text(prediction.get("comparison_cohort_fingerprint")) or "",
                label_move_class=move_class,
                label_target_at=target_at,
                label_evidence_digest=evidence_digest,
                probabilities=tuple(probabilities),
                target_index=target_index,
                ready_at=ready_at,
                simple_return=_simple_return(outcome),
                prediction_id=_text(prediction.get("prediction_id")) or "",
            )
        )
    return scored, sorted(observed)


def _contrasts(
    scored: Sequence[_ScoredLane],
    *,
    cohort: WorldCohort,
    excluded: defaultdict[str, int],
    integrity_ok: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_horizon: dict[str, dict[tuple[str, str, str, datetime], dict[str, list[_ScoredLane]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for row in scored:
        by_horizon[row.horizon_id][_anchor_key(row)][row.lane_id].append(row)
    matched_sets: list[dict[str, Any]] = []
    contrast_rows: list[dict[str, Any]] = []
    protocol = cohort.manifest.statistical_protocol
    gates = cohort.manifest.support_gates
    for contrast in cohort.manifest.contrasts:
        expected = tuple(term.lane_id for term in contrast.terms)
        coefficients = {term.lane_id: term.coefficient for term in contrast.terms}
        for horizon_id in cohort.manifest.horizons:
            deltas: list[tuple[str, float, _ScoredLane]] = []
            members_by_anchor: list[list[_ScoredLane]] = []
            anchors = by_horizon.get(horizon_id, {})
            slot_keys = sorted(anchors)
            for slot_key in slot_keys:
                lanes = anchors[slot_key]
                members: list[_ScoredLane] = []
                incomplete = False
                for lane_id in expected:
                    rows = lanes.get(lane_id, [])
                    if not rows:
                        excluded["absent_member"] += 1
                        incomplete = True
                        break
                    if len(rows) > 1:
                        excluded["ambiguous_lane_member"] += len(rows)
                        incomplete = True
                        break
                    members.append(rows[0])
                if incomplete:
                    continue
                mismatch = _pairing_mismatch(members)
                if mismatch is not None:
                    excluded[mismatch] += 1
                    continue
                matched_sets.append(
                    {
                        "contrast_id": contrast.contrast_id,
                        "horizon_id": horizon_id,
                        "venue": members[0].venue,
                        "symbol": members[0].symbol,
                        "bar_interval": members[0].bar_interval,
                        "as_of_bar_ts": members[0].as_of_bar_ts.isoformat(),
                        "lane_ids": [item.lane_id for item in members],
                        "expected_lane_ids": list(expected),
                    }
                )
                delta = sum(coefficients[item.lane_id] * _log_loss(item) for item in members)
                block = _venue_session(members[0])
                deltas.append((block, delta, members[0]))
                members_by_anchor.append(members)
            unique_anchors = len({_anchor_key(item[2]) for item in deltas})
            descriptive_ready = unique_anchors >= gates.descriptive_minimum_unique_anchors
            formal_ready = (
                gates.mode is SupportGateMode.FIXED_MINIMUM
                and gates.formal_minimum_unique_anchors is not None
                and unique_anchors >= gates.formal_minimum_unique_anchors
            )
            values = [item[1] for item in deltas]
            mean_delta = None if not values else round(sum(values) / len(values), 8)
            median_delta = None if not values else round(_median(values), 8)
            ci_low, ci_high = _block_bootstrap_ci(
                deltas,
                resamples=protocol.bootstrap_resamples,
                seed=protocol.seed,
                ci_level=protocol.ci_level,
            )
            conclusion = _contrast_conclusion(
                mean_delta=mean_delta,
                ci_low=ci_low,
                ci_high=ci_high,
                descriptive_ready=descriptive_ready,
                integrity_ok=integrity_ok,
                invalidated=cohort.phase is CohortPhase.INVALIDATED,
            )
            contrast_rows.append(
                {
                    "contrast_id": contrast.contrast_id,
                    "horizon_id": horizon_id,
                    "role": contrast.role.value,
                    "primary_metric": contrast.primary_metric,
                    "terms": [term.to_dict() for term in contrast.terms],
                    "matched_sets": len(values),
                    "unique_anchors": unique_anchors,
                    "pair_unit": protocol.pair_unit,
                    "block_key": protocol.block_key,
                    "ci_method": protocol.ci_method,
                    "ci_level": protocol.ci_level,
                    "mean_delta": mean_delta,
                    "median_delta": median_delta,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "secondary": _secondary(members_by_anchor, coefficients),
                    "gates": {"descriptive_ready": descriptive_ready, "formal_ready": formal_ready},
                    "winner": False,
                    "conclusion": conclusion,
                    "causal_claim": False,
                    "pnl_claim": False,
                }
            )
    return matched_sets, contrast_rows


def _pairing_mismatch(members: Sequence[_ScoredLane]) -> str | None:
    reference = members[0]
    for member in members[1:]:
        for field, reason in _PAIRING_FIELDS:
            if getattr(member, field) != getattr(reference, field):
                return reason
    return None


def _contrast_conclusion(
    *,
    mean_delta: float | None,
    ci_low: float | None,
    ci_high: float | None,
    descriptive_ready: bool,
    integrity_ok: bool,
    invalidated: bool,
) -> str:
    if invalidated:
        return "invalidated"
    if not integrity_ok or not descriptive_ready or mean_delta is None or ci_low is None or ci_high is None:
        return "inconclusive"
    if ci_high < 0.0:
        return "predictive_lift_observed"
    if ci_low > 0.0:
        return "degraded"
    return "inconclusive"


def _block_bootstrap_ci(
    deltas: Sequence[tuple[str, float, _ScoredLane]],
    *,
    resamples: int,
    seed: int,
    ci_level: float,
) -> tuple[float | None, float | None]:
    if not deltas:
        return None, None
    grouped: dict[str, list[float]] = defaultdict(list)
    for block, value, _row in deltas:
        grouped[block].append(value)
    blocks = [(key, grouped[key]) for key in sorted(grouped)]
    rng = random.Random(seed)
    stats: list[float] = []
    count = len(blocks)
    for _ in range(resamples):
        sample: list[float] = []
        for _index in range(count):
            sample.extend(blocks[rng.randrange(count)][1])
        if sample:
            stats.append(sum(sample) / len(sample))
    if not stats:
        return None, None
    stats.sort()
    alpha = (1.0 - ci_level) / 2.0
    return round(_percentile(stats, alpha), 8), round(_percentile(stats, 1.0 - alpha), 8)


def _secondary(
    members_by_anchor: Sequence[Sequence[_ScoredLane]],
    coefficients: Mapping[str, int],
) -> dict[str, Any]:
    if not members_by_anchor:
        return {
            "mean_brier_delta": None,
            "accuracy": None,
            "ece_5_bins": None,
            "class_distribution": {},
            "directional_shadow": {
                "metric": "directional_shadow_non_portfolio_drawdown",
                "status": "unavailable",
                "non_portfolio": True,
                "market_proxy_not_portfolio": True,
                "horizons_may_overlap": True,
            },
        }
    briers: list[float] = []
    labels: Counter[str] = Counter()
    returns: list[tuple[datetime, float]] = []
    missing = 0
    hits = 0
    calibration_bins: list[list[float]] = [[] for _ in range(5)]
    calibration_hits: list[list[float]] = [[] for _ in range(5)]
    treatment = next((lane_id for lane_id, coef in coefficients.items() if coef > 0), None)
    for members in members_by_anchor:
        briers.append(sum(coefficients[item.lane_id] * _brier(item) for item in members))
        labels[members[0].label_move_class] += 1
        chosen = next((item for item in members if item.lane_id == treatment), members[0])
        highest = max(chosen.probabilities)
        predicted_index = 1 if chosen.probabilities[1] == highest else max(
            range(3), key=chosen.probabilities.__getitem__
        )
        hit = 1.0 if predicted_index == chosen.target_index else 0.0
        hits += int(hit)
        confidence = chosen.probabilities[predicted_index]
        bin_index = min(int(confidence * 5), 4)
        calibration_bins[bin_index].append(confidence)
        calibration_hits[bin_index].append(hit)
        if chosen.simple_return is None:
            missing += 1
            continue
        predicted = CLASSES[predicted_index]
        direction = {"DOWN": -1.0, "FLAT": 0.0, "UP": 1.0}[predicted]
        returns.append((chosen.ready_at, direction * chosen.simple_return))
    ordered = [value for _ts, value in sorted(returns, key=lambda item: item[0])]
    drawdown = None if not ordered else round(_additive_drawdown(ordered), 8)
    count = len(members_by_anchor)
    ece = sum(
        (len(confidences) / count)
        * abs(sum(confidences) / len(confidences) - sum(calibration_hits[index]) / len(calibration_hits[index]))
        for index, confidences in enumerate(calibration_bins)
        if confidences
    )
    return {
        "mean_brier_delta": round(sum(briers) / len(briers), 8),
        "accuracy": round(hits / count, 8),
        "ece_5_bins": round(ece, 8),
        "class_distribution": dict(sorted(labels.items())),
        "directional_shadow": {
            "metric": "directional_shadow_non_portfolio_drawdown",
            "status": "available" if ordered else "unavailable",
            "matched_simple_returns": len(ordered),
            "missing_simple_return": missing,
            "max_drawdown": drawdown,
            "unit": "simple_return_per_one_unit_directional_forecast",
            "non_portfolio": True,
            "market_proxy_not_portfolio": True,
            "horizons_may_overlap": True,
        },
    }


def _support_summary(cohort: WorldCohort, contrast_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    primary = [
        row
        for row in contrast_rows
        if row["horizon_id"] == cohort.manifest.primary_horizon
    ]
    unique = max((int(row["unique_anchors"]) for row in primary), default=0)
    gates = cohort.manifest.support_gates
    return {
        "pair_unit": cohort.manifest.statistical_protocol.pair_unit,
        "primary_horizon": cohort.manifest.primary_horizon,
        "unique_anchors": unique,
        "mode": gates.mode.value,
        "descriptive_minimum_unique_anchors": gates.descriptive_minimum_unique_anchors,
        "formal_minimum_unique_anchors": gates.formal_minimum_unique_anchors,
        "descriptive_ready": unique >= gates.descriptive_minimum_unique_anchors,
        "formal_ready": bool(
            gates.mode is SupportGateMode.FIXED_MINIMUM
            and gates.formal_minimum_unique_anchors is not None
            and unique >= gates.formal_minimum_unique_anchors
        ),
    }


def _sensor_coverage(cohort: WorldCohort, scored: Sequence[_ScoredLane]) -> dict[str, Any]:
    by_lane = Counter(item.lane_id for item in scored)
    coverage: dict[str, Any] = {}
    for sensor in cohort.manifest.sensor_requirements:
        coverage[sensor.sensor_id] = {
            "mode": sensor.mode.value,
            "lane_ids": list(sensor.lane_ids),
            "predictions": sum(by_lane[lane_id] for lane_id in sensor.lane_ids),
        }
    by_venue = Counter(item.venue for item in scored)
    by_horizon = Counter(item.horizon_id for item in scored)
    return {"sensors": coverage, "by_venue": dict(sorted(by_venue.items())), "by_horizon": dict(sorted(by_horizon.items()))}


def _runtime_gaps(cohort: WorldCohort, scored: Sequence[_ScoredLane]) -> dict[str, Any]:
    expected = {lane.lane_id for lane in cohort.manifest.lanes}
    present: dict[tuple[str, str, str, datetime, str], set[str]] = defaultdict(set)
    for row in scored:
        present[(row.venue, row.symbol, row.bar_interval, row.as_of_bar_ts, row.horizon_id)].add(row.lane_id)
    full = sum(1 for lanes in present.values() if expected <= lanes)
    blocked = [
        {"lane_id": lane_id, "status": state.status.value}
        for lane_id, state in cohort.lane_states.items()
        if state.status.value == "blocked"
    ]
    return {
        "admitted_slots": len(cohort.admitted_slots),
        "scored_lane_slots": len(present),
        "full_lane_slots": full,
        "blocked_lanes": blocked,
    }


def _verify_completion_evidence(
    cohort: WorldCohort,
    *,
    episodes: Mapping[str, Mapping[str, Any]],
    outcomes: Mapping[tuple[str, str], Mapping[str, Any]],
    slots: Mapping[str, WorldCohortSlot],
) -> dict[str, Any]:
    evidence = cohort.completion_evidence
    if evidence is None:
        return {"present": False, "verified": False, "mismatches": []}
    mismatches: list[str] = []
    expected_slots = set(slots)
    actual_slots = {item.slot_id for item in evidence.slots}
    if expected_slots != actual_slots:
        mismatches.append("slot_set_mismatch")
    for item in evidence.slots:
        slot = slots.get(item.slot_id)
        if slot is None:
            mismatches.append(f"unknown_slot:{item.slot_id}")
            continue
        actual_horizons = {leaf.horizon_id for leaf in item.leaves}
        if actual_horizons != set(cohort.manifest.horizons):
            mismatches.append(f"horizon_set_mismatch:{item.slot_id}")
        for leaf in item.leaves:
            current = _current_leaf(slot, leaf.horizon_id, episodes=episodes, outcomes=outcomes)
            if current is None:
                mismatches.append(f"missing_leaf:{item.slot_id}:{leaf.horizon_id}")
                continue
            digest = world_cohort_horizon_leaf_digest(
                slot_id=item.slot_id,
                horizon_id=leaf.horizon_id,
                status=str(current["status"]),
                outcome_event_id=str(current["outcome_event_id"]),
                evidence_sha256=str(current["evidence_sha256"]),
                move_class=str(current["move_class"]),
            )
            if digest != leaf.digest or str(current["status"]) != leaf.status:
                mismatches.append(f"digest_mismatch:{item.slot_id}:{leaf.horizon_id}")
    return {"present": True, "verified": not mismatches, "mismatches": mismatches}


def _current_leaf(
    slot: WorldCohortSlot,
    horizon_id: str,
    *,
    episodes: Mapping[str, Mapping[str, Any]],
    outcomes: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any] | None:
    matching_ids = [
        episode_id
        for episode_id, row in episodes.items()
        if _text(row.get("venue")) == slot.venue
        and _text(row.get("symbol")) == slot.symbol
        and _text(row.get("bar_interval")) == slot.bar_interval
        and _timestamp(row, "as_of_bar_ts") == slot.as_of_bar_ts
    ]
    if not matching_ids:
        matching_ids = [episode_id for episode_id in slot.episode_refs_by_contract.values()]
    rows: list[dict[str, Any]] = []
    for episode_id in matching_ids:
        outcome = outcomes.get((episode_id, horizon_id))
        if outcome is None:
            continue
        status = str(outcome.get("status") or "").lower()
        if status not in _TERMINAL:
            continue
        rows.append(
            {
                "status": status,
                "outcome_event_id": _text(outcome.get("outcome_event_id") or outcome.get("event_id")) or "",
                "evidence_sha256": _evidence_digest(outcome),
                "move_class": (_text(outcome.get("move_class") or outcome.get("direction")) or "").upper(),
            }
        )
    if not rows:
        return None
    rows.sort(key=lambda item: item["outcome_event_id"])
    return rows[0]


def _collection_span(cohort: WorldCohort, scored: Sequence[_ScoredLane]) -> timedelta | None:
    stamps = [slot.as_of_bar_ts for slot in cohort.admitted_slots]
    stamps.extend(item.as_of_bar_ts for item in scored)
    if not stamps:
        return None
    return max(stamps) - min(stamps)


def _report_conclusion(
    cohort: WorldCohort,
    contrast_rows: Sequence[Mapping[str, Any]],
    integrity_ok: bool,
) -> str:
    if cohort.phase is CohortPhase.INVALIDATED:
        return "invalidated"
    if not integrity_ok:
        return "inconclusive"
    primary = [
        row
        for row in contrast_rows
        if row["horizon_id"] == cohort.manifest.primary_horizon and row["role"] in {"pilot_treatment", "primary"}
    ]
    if not primary:
        primary = [row for row in contrast_rows if row["horizon_id"] == cohort.manifest.primary_horizon]
    if any(row["conclusion"] == "predictive_lift_observed" for row in primary):
        return "predictive_lift_observed"
    if any(row["conclusion"] == "degraded" for row in primary):
        return "degraded"
    return "inconclusive"


def _market_slot(
    prediction: Mapping[str, Any],
    episode: Mapping[str, Any] | None,
) -> tuple[str, str, str, datetime] | None:
    source = episode or {}
    venue = _text(source.get("venue")) or _text(prediction.get("venue"))
    symbol = _text(source.get("symbol")) or _text(prediction.get("symbol"))
    interval = _text(source.get("bar_interval")) or _text(prediction.get("bar_interval"))
    as_of = _timestamp(source, "as_of_bar_ts") or _timestamp(prediction, "as_of_bar_ts")
    observation = prediction.get("input")
    if isinstance(observation, Mapping):
        venue = venue or _text(observation.get("venue"))
        symbol = symbol or _text(observation.get("symbol"))
        interval = interval or _text(observation.get("bar_interval") or observation.get("interval"))
        as_of = as_of or _timestamp(observation, "as_of_bar_ts")
    if venue is None or symbol is None or interval is None or as_of is None:
        return None
    return (venue, symbol, interval, as_of)


def _slot_key_from_slot(slot: WorldCohortSlot) -> tuple[str, str, str, datetime]:
    return (slot.venue, slot.symbol, slot.bar_interval, slot.as_of_bar_ts)


def _anchor_key(row: _ScoredLane) -> tuple[str, str, str, datetime]:
    return (row.venue, row.symbol, row.bar_interval, row.as_of_bar_ts)


def _venue_session(row: _ScoredLane) -> str:
    return f"{row.venue}:{row.as_of_bar_ts.date().isoformat()}"


def _probabilities(value: Mapping[str, Any]) -> list[float] | None:
    raw = value.get("probabilities")
    nested = value.get("prediction")
    if raw is None and isinstance(nested, Mapping):
        raw = nested.get("probabilities")
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
        return None
    if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in probabilities):
        return None
    if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-9):
        return None
    return probabilities


def _target_index(outcome: Mapping[str, Any]) -> int | None:
    raw = outcome.get("move_class") or outcome.get("direction")
    label = (_text(raw) or "").upper()
    try:
        return CLASSES.index(label)
    except ValueError:
        return None


def _log_loss(row: _ScoredLane) -> float:
    return -math.log(max(row.probabilities[row.target_index], 1e-15))


def _brier(row: _ScoredLane) -> float:
    return sum((probability - (1.0 if index == row.target_index else 0.0)) ** 2 for index, probability in enumerate(row.probabilities))


def _simple_return(outcome: Mapping[str, Any]) -> float | None:
    for name in ("simple_return", "forward_return"):
        raw = outcome.get(name)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
            return float(raw)
        label = outcome.get("label")
        if isinstance(label, Mapping):
            nested = label.get(name)
            if isinstance(nested, (int, float)) and not isinstance(nested, bool) and math.isfinite(float(nested)):
                return float(nested)
    return None


def _evidence_digest(outcome: Mapping[str, Any]) -> str:
    digest = _text(outcome.get("evidence_sha256"))
    if digest is not None:
        return digest
    evidence = outcome.get("evidence")
    if isinstance(evidence, Mapping):
        return canonical_sha256(dict(evidence))
    return canonical_sha256({})


def _timestamp(value: Mapping[str, Any], *names: str) -> datetime | None:
    for name in names:
        raw = value.get(name)
        if raw in (None, ""):
            continue
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else None
        if isinstance(raw, str):
            try:
                return parse_utc_timestamp(raw, name)
            except (TypeError, ValueError):
                continue
    return None


def _nested_timestamp(value: Mapping[str, Any], nested_name: str, field_name: str) -> datetime | None:
    nested = value.get(nested_name)
    if isinstance(nested, Mapping):
        return _timestamp(nested, field_name)
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _percentile(ordered: Sequence[float], p: float) -> float:
    if not ordered:
        raise ValueError("percentile requires values")
    if p <= 0.0:
        return float(ordered[0])
    if p >= 1.0:
        return float(ordered[-1])
    index = (len(ordered) - 1) * p
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return float(ordered[int(low)])
    weight = index - low
    return float(ordered[int(low)]) * (1.0 - weight) + float(ordered[int(high)]) * weight


def _additive_drawdown(values: Sequence[float]) -> float:
    running = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        maximum = max(maximum, peak - running)
    return maximum


__all__ = [
    "project_world_cohort_report",
    "read_world_cohort_report",
    "world_cohort_horizon_leaf_digest",
]
