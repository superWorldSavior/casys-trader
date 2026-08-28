"""Reconstructible pattern assessments: matched market/graph sets, controls, multiplicity.

The report is a read model. It never creates, migrates, or writes ``world_model.db``.
SQL stays in ``world_model_query``. Authority stays ``shadow_only`` with
``decision_effect=none`` and ``recommendation=NO_GO``. Conclusions never claim
causality or PnL.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import permutations
from pathlib import Path
from typing import Any
import math
import random

import yaml

from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    COHORT_TRADER_CONTRIBUTION,
)
from trader.domain.world_episode import WorldOutcome, parse_utc_timestamp
from trader.domain.world_feature_contract import (
    CONTEXT_FEATURE_CONTRACT_ID,
    GRAPH_FEATURE_CONTRACT_ID,
    TOPOLOGY_STATUS_ONLY_MASK_ID,
)
from trader.domain.world_pattern import (
    PATTERN_EVALUATION_HORIZON_IDS,
    PatternDiscoveryCompleted,
    PatternHypothesis,
    PatternOccurrence,
)
from trader.infrastructure.state_db.world_model_query import read_world_pattern_ledger
from trader.reporting.read_models.world_prediction_integrity import contains_forbidden_trader_feature


WORLD_PATTERN_REPORT_SCHEMA = "world_pattern_report.v1"
WORLD_PATTERN_STATUS_SCHEMA = "world_pattern_status.v1"
PATTERN_ASSESSMENT_SCHEMA = "pattern_assessment.v1"
ASSESSMENT_CONCLUSIONS = frozenset(
    {
        "predictive_association_observed",
        "inconclusive",
        "not_supported",
        "invalidated",
    }
)
CLASSES = ("DOWN", "FLAT", "UP")
_MIN_UNIQUE_SUPPORT = 2
_PERMUTATION_SEED = 20260823
_MAX_EXACT_PERMUTATIONS = 120
_GRAPH_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "world_graph.yaml"
_CLAIM_FIELDS = {
    "authority": COHORT_AUTHORITY,
    "decision_effect": COHORT_DECISION_EFFECT,
    "recommendation": COHORT_RECOMMENDATION,
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": COHORT_TRADER_CONTRIBUTION,
    "winner": False,
}


@dataclass(frozen=True)
class _ScoredPair:
    hypothesis_id: str
    occurrence_id: str
    venue: str
    symbol: str
    cutoff_at: datetime
    horizon_id: str
    graph_probabilities: tuple[float, float, float]
    context_probabilities: tuple[float, float, float]
    topology_probabilities: tuple[float, float, float] | None
    target_index: int
    predicted_class: str
    label_class: str
    context_prediction_id: str
    graph_prediction_id: str
    graph_feature_contract_id: str
    graph_feature_mask_id: str
    path_signature: tuple[tuple[object, ...], ...]
    truncated_signature: tuple[tuple[object, ...], ...]
    world_outcome_event_id: str
    world_outcome_content_sha256: str


def read_world_pattern_report(state_dir: str | Path, cohort_id: str) -> dict[str, Any]:
    """Project a reconstructible pattern report without creating the ledger."""

    db_path = Path(state_dir) / "world_model.db"
    ledger = read_world_pattern_ledger(db_path, cohort_id)
    return project_world_pattern_report(ledger, db_path=db_path)


def read_world_pattern_status(state_dir: str | Path, cohort_id: str | None = None) -> dict[str, Any]:
    """Hypothesis-first status, including evaluating hypotheses with zero occurrences."""

    db_path = Path(state_dir) / "world_model.db"
    ledger = read_world_pattern_ledger(db_path, cohort_id)
    return project_world_pattern_status(ledger, db_path=db_path)


def project_world_pattern_status(
    ledger: Mapping[str, Any],
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Rebuild ``world_pattern_status.v1`` without requiring an occurrence or label."""

    path = None if db_path is None else str(db_path)
    cohort_id = ledger.get("cohort_id")
    payload = {
        "schema_version": WORLD_PATTERN_STATUS_SCHEMA,
        "db_path": path,
        "cohort_id": cohort_id,
        "status": str(ledger.get("status") or "unavailable"),
        "exists": bool(ledger.get("exists")),
        "hypotheses": [],
        "lifecycle": _empty_lifecycle(),
        **_CLAIM_FIELDS,
    }
    if "missing_tables" in ledger:
        payload["missing_tables"] = list(ledger["missing_tables"])
    if "error" in ledger:
        payload["error"] = ledger["error"]
    if payload["status"] != "loaded":
        return payload
    excluded: defaultdict[str, int] = defaultdict(int)
    hypotheses = _reconstruct_hypotheses(ledger.get("hypothesis_events") or (), excluded=excluded)
    occurrences = _reconstruct_occurrences(ledger.get("occurrence_events") or (), excluded=excluded)
    by_hypothesis: dict[str, list[PatternOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        if cohort_id and occurrence.spec.cohort_id != cohort_id:
            continue
        by_hypothesis[occurrence.hypothesis_id].append(occurrence)
    rows: list[dict[str, Any]] = []
    for hypothesis_id in sorted(hypotheses):
        hypothesis = hypotheses[hypothesis_id]
        items = by_hypothesis.get(hypothesis_id, [])
        expected = (
            list(PATTERN_EVALUATION_HORIZON_IDS)
            if hypothesis.status == "evaluating"
            else [hypothesis.spec.target.horizon_id]
        )
        if items:
            expected = list(items[0].spec.expected_horizon_ids)
        linked: dict[str, int] = {horizon_id: 0 for horizon_id in expected}
        pending: dict[str, int] = {horizon_id: 0 for horizon_id in expected}
        for occurrence in items:
            for horizon_id in occurrence.spec.expected_horizon_ids:
                if occurrence.active_outcome_link(horizon_id) is None:
                    pending[horizon_id] = pending.get(horizon_id, 0) + 1
                else:
                    linked[horizon_id] = linked.get(horizon_id, 0) + 1
        if not items and hypothesis.status == "evaluating":
            for horizon_id in expected:
                pending[horizon_id] = 0
        rows.append(
            {
                "hypothesis_id": hypothesis_id,
                "status": hypothesis.status,
                "evaluation_cohort_id": hypothesis.evaluation_cohort_id,
                "evaluation_dataset_fingerprint": hypothesis.evaluation_dataset_fingerprint,
                "target_horizon_id": hypothesis.spec.target.horizon_id,
                "expected_horizon_ids": expected,
                "occurrence_count": len(items),
                "linked_horizons": linked,
                "pending_horizons": pending,
            }
        )
    payload.update(
        {
            "hypotheses": rows,
            "hypothesis_count": len(rows),
            "occurrence_count": sum(item["occurrence_count"] for item in rows),
            "lifecycle": {
                "replay_authority": "world_pattern_lifecycle_events",
                "markers": _lifecycle_markers(
                    ledger.get("lifecycle_events") or (),
                    cohort_id=cohort_id if isinstance(cohort_id, str) else None,
                    excluded=excluded,
                ),
                "causal_claim": False,
            },
            "exclusions": dict(sorted(excluded.items())),
        }
    )
    return payload


def project_world_pattern_report(
    ledger: Mapping[str, Any],
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Rebuild ``world_pattern_report.v1`` from a read-only ledger snapshot."""

    path = None if db_path is None else str(db_path)
    cohort_id = str(ledger.get("cohort_id") or "")
    protocol = _graph_protocol()
    report = _bounded_base(path=path, cohort_id=cohort_id, protocol=protocol)
    status = str(ledger.get("status") or "unavailable")
    report["exists"] = bool(ledger.get("exists"))
    report["status"] = status
    if "missing_tables" in ledger:
        report["missing_tables"] = list(ledger["missing_tables"])
    if "error" in ledger:
        report["error"] = ledger["error"]
    if status != "loaded":
        return report

    integrity_failures: list[str] = []
    excluded: defaultdict[str, int] = defaultdict(int)
    hypotheses = _reconstruct_hypotheses(ledger.get("hypothesis_events") or (), excluded=excluded)
    occurrences = _reconstruct_occurrences(ledger.get("occurrence_events") or (), excluded=excluded)
    outcome_by_event = _index_outcomes(ledger.get("outcomes") or ())
    episode_by_id = _index_episodes(ledger.get("episodes") or ())
    predictions = [item for item in ledger.get("predictions") or () if isinstance(item, Mapping)]
    for prediction in predictions:
        if contains_forbidden_trader_feature(prediction):
            integrity_failures.append("trader_feature_key")
    context_by_slot = _index_control_predictions(
        predictions,
        episodes=episode_by_id,
        mask=None,
        contract=CONTEXT_FEATURE_CONTRACT_ID,
    )
    topology_by_slot = _index_control_predictions(
        predictions,
        episodes=episode_by_id,
        mask=TOPOLOGY_STATUS_ONLY_MASK_ID,
        contract=GRAPH_FEATURE_CONTRACT_ID,
    )

    scored: list[_ScoredPair] = []
    matched_sets: list[dict[str, Any]] = []
    canonical_by_hypothesis: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        if occurrence.spec.cohort_id != cohort_id:
            continue
        if occurrence.status == "invalidated":
            excluded["occurrence_invalidated"] += 1
            continue
        pair = _score_occurrence(
            occurrence,
            hypotheses=hypotheses,
            outcome_by_event=outcome_by_event,
            context_by_slot=context_by_slot,
            topology_by_slot=topology_by_slot,
            excluded=excluded,
            canonical_by_hypothesis=canonical_by_hypothesis,
        )
        if pair is None:
            continue
        scored.append(pair)
        matched_sets.append(
            {
                "hypothesis_id": pair.hypothesis_id,
                "occurrence_id": pair.occurrence_id,
                "venue": pair.venue,
                "symbol": pair.symbol,
                "as_of_bar_ts": pair.cutoff_at.isoformat(),
                "horizon_id": pair.horizon_id,
                "context_prediction_id": pair.context_prediction_id,
                "graph_prediction_id": pair.graph_prediction_id,
                "context_feature_contract_id": CONTEXT_FEATURE_CONTRACT_ID,
                "graph_feature_contract_id": pair.graph_feature_contract_id,
                "graph_feature_mask_id": pair.graph_feature_mask_id,
                "lane_ids": ["context", "graph"],
            }
        )

    matched_sets.sort(key=lambda item: (item["hypothesis_id"], item["symbol"], item["as_of_bar_ts"]))
    integrity_ok = not integrity_failures
    assessments, family = _assess_hypotheses(
        hypotheses,
        scored=scored,
        canonical_by_hypothesis=canonical_by_hypothesis,
        predictions=predictions,
        episodes=episode_by_id,
        protocol=protocol,
        integrity_ok=integrity_ok,
        excluded=excluded,
    )
    negative_controls = _report_negative_controls(assessments, protocol=protocol)
    multiplicity = {
        "method": protocol["method"],
        "family_wise_alpha": protocol["family_wise_alpha"],
        "frozen_before": protocol["frozen_before"],
        "config_sha256": protocol["config_sha256"],
        "family_size": len(family),
        "adjusted": family,
    }
    if not integrity_ok:
        for assessment in assessments:
            if assessment["conclusion"] != "invalidated":
                assessment["conclusion"] = "inconclusive"
    report.update(
        {
            "matched_sets": matched_sets,
            "assessments": assessments,
            "exclusions": dict(sorted(excluded.items())),
            "negative_controls": negative_controls,
            "multiplicity": multiplicity,
            "gates": {"integrity": {"passed": integrity_ok, "failures": integrity_failures}},
            "conclusion": _report_conclusion(assessments, integrity_ok),
        }
    )
    return report


def _empty_lifecycle() -> dict[str, Any]:
    return {
        "replay_authority": "world_pattern_lifecycle_events",
        "markers": [],
        "causal_claim": False,
    }


def _lifecycle_markers(
    events: Sequence[object],
    *,
    cohort_id: str | None,
    excluded: defaultdict[str, int],
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            excluded["invalid_lifecycle_event"] += 1
            continue
        try:
            marker = PatternDiscoveryCompleted.from_mapping(event)
        except (TypeError, ValueError):
            excluded["invalid_lifecycle_event"] += 1
            continue
        if cohort_id and marker.evaluation_cohort_id != cohort_id:
            continue
        markers.append(
            {
                "event_id": marker.event_id,
                "event_type": marker.event_type,
                "evaluation_cohort_id": marker.evaluation_cohort_id,
                "started_event_id": marker.started_event_id,
                "manifest_sha256": marker.manifest_sha256,
                "formation_cutoff": marker.formation_cutoff.isoformat(),
                "evaluation_start_not_before": marker.evaluation_start_not_before.isoformat(),
                "formation_dataset_fingerprint": marker.formation_dataset_fingerprint,
                "evaluation_dataset_fingerprint": marker.evaluation_dataset_fingerprint,
                "selected_hypothesis_ids": list(marker.selected_hypothesis_ids),
                "selected_count": marker.selected_count,
                "shadow_only": True,
                "decision_effect": "none",
                "learning_authority": "shadow_only",
                "replay_authority": "world_pattern_lifecycle_events",
                "causal_claim": False,
            }
        )
    markers.sort(key=lambda item: (str(item["evaluation_cohort_id"]), str(item["event_id"])))
    return markers


def _bounded_base(*, path: str | None, cohort_id: str, protocol: Mapping[str, Any]) -> dict[str, Any]:
    controls = {
        control_id: {
            "control_id": control_id,
            "status": "unavailable",
            "causal_claim": False,
        }
        for control_id in protocol["negative_controls"]
    }
    return {
        "schema_version": WORLD_PATTERN_REPORT_SCHEMA,
        "db_path": path,
        "cohort_id": cohort_id,
        "status": "unavailable",
        "exists": False,
        "matched_sets": [],
        "assessments": [],
        "exclusions": {},
        "negative_controls": controls,
        "multiplicity": {
            "method": protocol["method"],
            "family_wise_alpha": protocol["family_wise_alpha"],
            "frozen_before": protocol["frozen_before"],
            "config_sha256": protocol["config_sha256"],
            "family_size": 0,
            "adjusted": [],
        },
        "conclusion": "inconclusive",
        **_CLAIM_FIELDS,
    }


def _graph_protocol() -> dict[str, Any]:
    config = yaml.safe_load(_GRAPH_CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, Mapping):
        raise ValueError("world_graph.yaml must be a mapping")
    multiplicity = config.get("multiplicity")
    if not isinstance(multiplicity, Mapping):
        raise ValueError("world_graph.yaml multiplicity is required")
    method = str(multiplicity.get("method") or "")
    if method != "holm_bonferroni":
        raise ValueError("GRAPH-11 multiplicity method is holm_bonferroni")
    controls = tuple(str(item) for item in (config.get("negative_controls") or ()))
    return {
        "negative_controls": controls,
        "method": method,
        "family_wise_alpha": float(multiplicity["family_wise_alpha"]),
        "frozen_before": str(multiplicity.get("frozen_before") or "GRAPH-7"),
        "config_sha256": str(config.get("content_sha256") or ""),
    }


def _reconstruct_hypotheses(
    events: Sequence[object],
    *,
    excluded: defaultdict[str, int],
) -> dict[str, PatternHypothesis]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        if not isinstance(event, Mapping):
            excluded["invalid_hypothesis_event"] += 1
            continue
        hypothesis_id = _text(event.get("hypothesis_id"))
        spec = event.get("spec")
        if hypothesis_id is None and isinstance(spec, Mapping):
            hypothesis_id = _text(spec.get("hypothesis_id"))
        if hypothesis_id is None:
            excluded["invalid_hypothesis_event"] += 1
            continue
        grouped[hypothesis_id].append(event)
    reconstructed: dict[str, PatternHypothesis] = {}
    for hypothesis_id, rows in grouped.items():
        ordered = _order_events(rows)
        try:
            reconstructed[hypothesis_id] = PatternHypothesis.from_events(ordered)
        except (TypeError, ValueError):
            excluded["invalid_hypothesis_event"] += 1
    return reconstructed


def _reconstruct_occurrences(
    events: Sequence[object],
    *,
    excluded: defaultdict[str, int],
) -> list[PatternOccurrence]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        if not isinstance(event, Mapping):
            excluded["invalid_occurrence_event"] += 1
            continue
        occurrence_id = _text(event.get("occurrence_id"))
        nested = event.get("occurrence")
        if occurrence_id is None and isinstance(nested, Mapping):
            occurrence_id = _text(nested.get("occurrence_id"))
        if occurrence_id is None:
            excluded["invalid_occurrence_event"] += 1
            continue
        grouped[occurrence_id].append(event)
    reconstructed: list[PatternOccurrence] = []
    for rows in grouped.values():
        try:
            reconstructed.append(PatternOccurrence.from_events(_order_events(rows)))
        except (TypeError, ValueError):
            excluded["invalid_occurrence_event"] += 1
    reconstructed.sort(key=lambda item: (item.hypothesis_id, item.occurrence_id))
    return reconstructed


def _order_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rows = list(events)
    if any(item.get("sequence") not in (None, "") for item in rows):
        rows.sort(key=lambda item: (int(item.get("sequence") or 0), str(item.get("event_id") or "")))
    return rows


def _index_outcomes(rows: Sequence[object]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        event_id = _text(row.get("outcome_event_id") or row.get("event_id"))
        if event_id is not None:
            indexed[event_id] = row
    return indexed


def _index_episodes(rows: Sequence[object]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        episode_id = _text(row.get("episode_id"))
        if episode_id is not None:
            indexed[episode_id] = row
    return indexed


def _index_control_predictions(
    predictions: Sequence[Mapping[str, Any]],
    *,
    episodes: Mapping[str, Mapping[str, Any]],
    mask: str | None,
    contract: str,
) -> dict[tuple[str, str, datetime, str], list[Mapping[str, Any]]]:
    indexed: dict[tuple[str, str, datetime, str], list[Mapping[str, Any]]] = defaultdict(list)
    for prediction in predictions:
        episode = episodes.get(_text(prediction.get("episode_id")) or "")
        actual_contract = _prediction_contract(prediction, episode)
        actual_mask = _text(prediction.get("feature_mask_id"))
        if actual_contract != contract:
            continue
        if mask is not None and actual_mask != mask:
            continue
        slot = _prediction_slot(prediction, episode)
        if slot is None:
            continue
        indexed[slot].append(prediction)
    return indexed


def _score_occurrence(
    occurrence: PatternOccurrence,
    *,
    hypotheses: Mapping[str, PatternHypothesis],
    outcome_by_event: Mapping[str, Mapping[str, Any]],
    context_by_slot: Mapping[tuple[str, str, datetime, str], Sequence[Mapping[str, Any]]],
    topology_by_slot: Mapping[tuple[str, str, datetime, str], Sequence[Mapping[str, Any]]],
    excluded: defaultdict[str, int],
    canonical_by_hypothesis: defaultdict[str, list[dict[str, Any]]],
) -> _ScoredPair | None:
    hypothesis = hypotheses.get(occurrence.hypothesis_id)
    join_exclusion = _hypothesis_join_exclusion(occurrence, hypothesis)
    if join_exclusion is not None:
        excluded[join_exclusion] += 1
        return None
    horizon_id = occurrence.spec.forecast.horizon_id
    instrument = occurrence.instrument.entity_id
    parsed = _instrument_slot(instrument)
    if parsed is None:
        excluded["invalid_instrument_identity"] += 1
        return None
    venue, symbol = parsed
    slot = (venue, symbol, occurrence.cutoff_at, horizon_id)
    link = occurrence.active_outcome_link(horizon_id)
    if link is None:
        excluded["outcome_link_missing"] += 1
        return None
    row = outcome_by_event.get(link.world_outcome_event_id)
    if row is None:
        excluded["canonical_outcome_missing"] += 1
        canonical_by_hypothesis[occurrence.hypothesis_id].append(
            {
                "verified": False,
                "world_outcome_event_id": link.world_outcome_event_id,
                "world_outcome_content_sha256": link.world_outcome_content_sha256,
                "horizon_id": link.horizon_id,
            }
        )
        return None
    try:
        outcome = WorldOutcome.from_dict(row)
    except (TypeError, ValueError):
        excluded["canonical_outcome_unreadable"] += 1
        return None
    verified = (
        outcome.event_id == link.world_outcome_event_id
        and outcome.payload_hash == link.world_outcome_content_sha256
        and outcome.horizon.horizon_id == link.horizon_id
    )
    canonical_by_hypothesis[occurrence.hypothesis_id].append(
        {
            "verified": verified,
            "world_outcome_event_id": outcome.event_id,
            "world_outcome_content_sha256": outcome.payload_hash,
            "horizon_id": outcome.horizon.horizon_id,
        }
    )
    if not verified:
        excluded["canonical_outcome_mismatch"] += 1
        return None
    if outcome.status != "observed" or outcome.direction is None:
        excluded["outcome_not_observed"] += 1
        return None
    target_index = CLASSES.index(outcome.direction)
    context_rows = list(context_by_slot.get(slot, ()))
    if not context_rows:
        excluded["absent_context_member"] += 1
        return None
    if len(context_rows) > 1:
        excluded["ambiguous_context_member"] += len(context_rows)
        return None
    context_probabilities = _probabilities(context_rows[0])
    graph_probabilities = _forecast_probabilities(occurrence.spec.forecast.probabilities)
    if context_probabilities is None or graph_probabilities is None:
        excluded["invalid_score_payload"] += 1
        return None
    topology_rows = list(topology_by_slot.get(slot, ()))
    topology_probabilities = None
    if len(topology_rows) == 1:
        topology_probabilities = _probabilities(topology_rows[0])
    elif len(topology_rows) > 1:
        excluded["ambiguous_topology_member"] += len(topology_rows)
    hops = occurrence.exact_path
    path_signature = tuple(hop.identity_tuple() for hop in hops)
    truncated_signature = path_signature[:-1] if len(path_signature) > 1 else path_signature
    return _ScoredPair(
        hypothesis_id=occurrence.hypothesis_id,
        occurrence_id=occurrence.occurrence_id,
        venue=venue,
        symbol=symbol,
        cutoff_at=occurrence.cutoff_at,
        horizon_id=horizon_id,
        graph_probabilities=graph_probabilities,
        context_probabilities=context_probabilities,
        topology_probabilities=topology_probabilities,
        target_index=target_index,
        predicted_class=occurrence.spec.forecast.predicted_class,
        label_class=outcome.direction,
        context_prediction_id=_text(context_rows[0].get("prediction_id")) or "",
        graph_prediction_id=occurrence.spec.forecast.prediction_id,
        graph_feature_contract_id=occurrence.feature_contract_id,
        graph_feature_mask_id=occurrence.feature_mask_id,
        path_signature=path_signature,
        truncated_signature=truncated_signature,
        world_outcome_event_id=outcome.event_id,
        world_outcome_content_sha256=outcome.payload_hash,
    )


def _hypothesis_join_exclusion(
    occurrence: PatternOccurrence,
    hypothesis: PatternHypothesis | None,
) -> str | None:
    if hypothesis is None:
        return "hypothesis_missing"
    spec = hypothesis.spec
    if occurrence.spec.hypothesis_content_sha256 != spec.content_sha256:
        return "hypothesis_fingerprint_mismatch"
    if (
        occurrence.feature_contract_id != spec.feature_contract_id
        or occurrence.feature_contract_fingerprint != spec.feature_contract_fingerprint
    ):
        return "hypothesis_contract_mismatch"
    if (
        occurrence.feature_mask_id != spec.feature_mask_id
        or occurrence.feature_mask_fingerprint != spec.feature_mask_fingerprint
    ):
        return "hypothesis_mask_mismatch"
    if occurrence.spec.forecast.model_identity != spec.model_identity:
        return "hypothesis_model_mismatch"
    if (
        occurrence.spec.forecast.horizon_id != spec.target.horizon_id
        or spec.target.horizon_id not in occurrence.spec.expected_horizon_ids
    ):
        return "hypothesis_horizon_mismatch"
    return None


def _assess_hypotheses(
    hypotheses: Mapping[str, PatternHypothesis],
    *,
    scored: Sequence[_ScoredPair],
    canonical_by_hypothesis: Mapping[str, Sequence[Mapping[str, Any]]],
    predictions: Sequence[Mapping[str, Any]],
    episodes: Mapping[str, Mapping[str, Any]],
    protocol: Mapping[str, Any],
    integrity_ok: bool,
    excluded: Mapping[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_hypothesis: dict[str, list[_ScoredPair]] = defaultdict(list)
    for pair in scored:
        by_hypothesis[pair.hypothesis_id].append(pair)
    hypothesis_ids = sorted(set(hypotheses) | set(by_hypothesis))
    drafts: list[dict[str, Any]] = []
    family_p: list[tuple[str, float]] = []
    for hypothesis_id in hypothesis_ids:
        hypothesis = hypotheses.get(hypothesis_id)
        pairs = by_hypothesis.get(hypothesis_id, [])
        status = hypothesis.status if hypothesis is not None else "unknown"
        metrics = _pair_metrics(pairs)
        permutation = _entity_permutation(pairs)
        time_shift = _block_time_shift(pairs)
        topology = _topology_control(pairs)
        truncated = _truncated_chain_control(hypothesis, pairs)
        without_event = _population_without_event(pairs, predictions, episodes=episodes)
        raw_p = 1.0 if len({_anchor_key(item) for item in pairs}) < _MIN_UNIQUE_SUPPORT else permutation["p_value"]
        if status != "invalidated":
            family_p.append((hypothesis_id, raw_p))
        drafts.append(
            {
                "hypothesis_id": hypothesis_id,
                "status": status,
                "hypothesis_content_sha256": None if hypothesis is None else hypothesis.spec.content_sha256,
                "pairs": pairs,
                "metrics": metrics,
                "permutation": permutation,
                "time_shift": time_shift,
                "topology": topology,
                "truncated": truncated,
                "without_event": without_event,
                "raw_p": raw_p,
                "canonical_outcomes": list(canonical_by_hypothesis.get(hypothesis_id, ())),
            }
        )
    adjusted = _holm_bonferroni(family_p, alpha=float(protocol["family_wise_alpha"]))
    adjusted_by_id = {item["hypothesis_id"]: item for item in adjusted}
    assessments: list[dict[str, Any]] = []
    for draft in drafts:
        holm = adjusted_by_id.get(
            draft["hypothesis_id"],
            {
                "raw_p": draft["raw_p"],
                "holm_rank": None,
                "holm_threshold": None,
                "holm_rejected": False,
            },
        )
        pairs = draft["pairs"]
        metrics = draft["metrics"]
        unique_support = len({_anchor_key(item) for item in pairs})
        conclusion = _assessment_conclusion(
            status=draft["status"],
            unique_support=unique_support,
            mean_delta=metrics["mean_delta"],
            holm_rejected=bool(holm.get("holm_rejected")),
            topology=draft["topology"],
            time_shift=draft["time_shift"],
            integrity_ok=integrity_ok,
        )
        assessments.append(
            {
                "schema_version": PATTERN_ASSESSMENT_SCHEMA,
                "hypothesis_id": draft["hypothesis_id"],
                "hypothesis_content_sha256": draft["hypothesis_content_sha256"],
                "status": draft["status"],
                "unique_support": unique_support,
                "coverage": unique_support,
                "log_loss": metrics["log_loss"],
                "brier": metrics["brier"],
                "calibration": {"ece_5_bins": metrics["ece_5_bins"]},
                "lift_vs_context": {
                    "mean_delta": metrics["mean_delta"],
                    "median_delta": metrics["median_delta"],
                    "pair_unit": "unique_market_anchor",
                    "block_key": "venue_session",
                },
                "favorable_contexts": _favorable_contexts(pairs),
                "counter_examples": _counter_examples(pairs),
                "permutation_results": {
                    "entity_permutation_within_venue_session": draft["permutation"],
                    "block_respecting_time_shift": draft["time_shift"],
                },
                "negative_controls": {
                    "topology_status_only": draft["topology"],
                    "entity_permutation_within_venue_session": draft["permutation"],
                    "block_respecting_time_shift": draft["time_shift"],
                    "truncated_chain": draft["truncated"],
                    "comparable_population_without_event": draft["without_event"],
                },
                "exclusion_reasons": dict(sorted(excluded.items())),
                "canonical_outcomes": draft["canonical_outcomes"],
                "multiplicity": {
                    "raw_p": holm.get("raw_p", draft["raw_p"]),
                    "holm_rank": holm.get("holm_rank"),
                    "holm_threshold": holm.get("holm_threshold"),
                    "holm_rejected": bool(holm.get("holm_rejected")),
                },
                "conclusion": conclusion,
                "causal_claim": False,
                "pnl_claim": False,
                "winner": False,
            }
        )
    assessments.sort(key=lambda item: (-int(item["unique_support"]), str(item["hypothesis_id"])))
    return assessments, adjusted


def _assessment_conclusion(
    *,
    status: str,
    unique_support: int,
    mean_delta: float | None,
    holm_rejected: bool,
    topology: Mapping[str, Any],
    time_shift: Mapping[str, Any],
    integrity_ok: bool,
) -> str:
    if status == "invalidated":
        return "invalidated"
    if not integrity_ok or unique_support < _MIN_UNIQUE_SUPPORT or mean_delta is None:
        return "inconclusive"
    if mean_delta >= 0.0:
        return "not_supported"
    topology_explains = topology.get("status") == "evaluated" and topology.get("explains_association") is True
    shift_survives = (
        time_shift.get("status") == "evaluated"
        and time_shift.get("shifted_mean_delta") is not None
        and time_shift["shifted_mean_delta"] <= mean_delta
    )
    if topology_explains or shift_survives:
        return "not_supported"
    if holm_rejected:
        return "predictive_association_observed"
    return "inconclusive"


def _pair_metrics(pairs: Sequence[_ScoredPair]) -> dict[str, Any]:
    if not pairs:
        return {
            "mean_delta": None,
            "median_delta": None,
            "log_loss": {"graph": None, "context": None, "delta": None},
            "brier": {"graph": None, "context": None, "delta": None},
            "ece_5_bins": None,
        }
    graph_losses = [_log_loss(item.graph_probabilities, item.target_index) for item in pairs]
    context_losses = [_log_loss(item.context_probabilities, item.target_index) for item in pairs]
    graph_brier = [_brier(item.graph_probabilities, item.target_index) for item in pairs]
    context_brier = [_brier(item.context_probabilities, item.target_index) for item in pairs]
    deltas = [left - right for left, right in zip(graph_losses, context_losses, strict=True)]
    mean_graph = sum(graph_losses) / len(graph_losses)
    mean_context = sum(context_losses) / len(context_losses)
    mean_delta = sum(deltas) / len(deltas)
    return {
        "mean_delta": round(mean_delta, 8),
        "median_delta": round(_median(deltas), 8),
        "log_loss": {
            "graph": round(mean_graph, 8),
            "context": round(mean_context, 8),
            "delta": round(mean_delta, 8),
        },
        "brier": {
            "graph": round(sum(graph_brier) / len(graph_brier), 8),
            "context": round(sum(context_brier) / len(context_brier), 8),
            "delta": round(
                sum(left - right for left, right in zip(graph_brier, context_brier, strict=True)) / len(graph_brier),
                8,
            ),
        },
        "ece_5_bins": round(_ece(pairs), 8),
    }


def _entity_permutation(pairs: Sequence[_ScoredPair]) -> dict[str, Any]:
    payload = {
        "control_id": "entity_permutation_within_venue_session",
        "status": "unavailable",
        "block_key": "venue_session",
        "p_value": None,
        "observed_mean_delta": None,
        "causal_claim": False,
    }
    if not pairs:
        return payload
    observed = _mean_delta(pairs)
    grouped: dict[str, list[_ScoredPair]] = defaultdict(list)
    for pair in pairs:
        grouped[_venue_session(pair)].append(pair)
    blocks = [grouped[key] for key in sorted(grouped)]
    exact = 1
    for block in blocks:
        exact *= math.factorial(len(block))
        if exact > _MAX_EXACT_PERMUTATIONS:
            break
    if exact <= _MAX_EXACT_PERMUTATIONS:
        stats = _exact_block_permutation_deltas(blocks)
    else:
        stats = _sampled_block_permutation_deltas(blocks, resamples=_MAX_EXACT_PERMUTATIONS)
    extreme = sum(1 for value in stats if value <= observed + 1e-12)
    p_value = 1.0 if not stats else extreme / len(stats)
    payload.update(
        {
            "status": "evaluated",
            "observed_mean_delta": round(observed, 8),
            "p_value": round(p_value, 8),
            "permutations": len(stats),
        }
    )
    return payload


def _exact_block_permutation_deltas(blocks: Sequence[Sequence[_ScoredPair]]) -> list[float]:
    block_perms = [list(permutations(block)) for block in blocks]
    stats: list[float] = []

    def _walk(index: int, assigned: list[_ScoredPair]) -> None:
        if index == len(block_perms):
            stats.append(_mean_delta_from_assignment(blocks, assigned))
            return
        offset = sum(len(block) for block in blocks[:index])
        for perm in block_perms[index]:
            next_assigned = assigned[:offset] + list(perm)
            _walk(index + 1, next_assigned)

    _walk(0, [])
    return stats


def _sampled_block_permutation_deltas(blocks: Sequence[Sequence[_ScoredPair]], *, resamples: int) -> list[float]:
    rng = random.Random(_PERMUTATION_SEED)
    stats: list[float] = []
    for _ in range(resamples):
        assigned: list[_ScoredPair] = []
        for block in blocks:
            shuffled = list(block)
            rng.shuffle(shuffled)
            assigned.extend(shuffled)
        stats.append(_mean_delta_from_assignment(blocks, assigned))
    return stats


def _mean_delta_from_assignment(blocks: Sequence[Sequence[_ScoredPair]], assigned: Sequence[_ScoredPair]) -> float:
    original = [item for block in blocks for item in block]
    deltas = [
        _log_loss(shuffled.graph_probabilities, original.target_index)
        - _log_loss(original.context_probabilities, original.target_index)
        for original, shuffled in zip(original, assigned, strict=True)
    ]
    return sum(deltas) / len(deltas)


def _block_time_shift(pairs: Sequence[_ScoredPair]) -> dict[str, Any]:
    payload = {
        "control_id": "block_respecting_time_shift",
        "status": "unavailable",
        "shifted_mean_delta": None,
        "observed_mean_delta": None,
        "causal_claim": False,
    }
    if len(pairs) < 2:
        return payload
    grouped: dict[str, list[_ScoredPair]] = defaultdict(list)
    for pair in pairs:
        grouped[_venue_session(pair)].append(pair)
    deltas: list[float] = []
    for block in grouped.values():
        ordered = sorted(block, key=lambda item: (item.cutoff_at, item.symbol, item.occurrence_id))
        if len(ordered) < 2:
            continue
        shifted_targets = [ordered[(index - 1) % len(ordered)].target_index for index in range(len(ordered))]
        for pair, target_index in zip(ordered, shifted_targets, strict=True):
            deltas.append(
                _log_loss(pair.graph_probabilities, target_index) - _log_loss(pair.context_probabilities, target_index)
            )
    if not deltas:
        return payload
    payload.update(
        {
            "status": "evaluated",
            "observed_mean_delta": round(_mean_delta(pairs), 8),
            "shifted_mean_delta": round(sum(deltas) / len(deltas), 8),
        }
    )
    return payload


def _topology_control(pairs: Sequence[_ScoredPair]) -> dict[str, Any]:
    payload = {
        "control_id": "topology_status_only",
        "status": "unavailable",
        "mask_id": TOPOLOGY_STATUS_ONLY_MASK_ID,
        "mean_delta": None,
        "explains_association": False,
        "causal_claim": False,
    }
    usable = [item for item in pairs if item.topology_probabilities is not None]
    if not usable:
        return payload
    topology_delta = sum(
        _log_loss(item.topology_probabilities, item.target_index)
        - _log_loss(item.context_probabilities, item.target_index)
        for item in usable
    ) / len(usable)
    graph_delta = _mean_delta(usable)
    payload.update(
        {
            "status": "evaluated",
            "mean_delta": round(topology_delta, 8),
            "graph_mean_delta": round(graph_delta, 8),
            "explains_association": bool(topology_delta <= graph_delta + 1e-12),
        }
    )
    return payload


def _truncated_chain_control(
    hypothesis: PatternHypothesis | None,
    pairs: Sequence[_ScoredPair],
) -> dict[str, Any]:
    signature = ()
    if pairs:
        signature = pairs[0].truncated_signature
    elif hypothesis is not None and len(hypothesis.spec.steps) > 1:
        steps = hypothesis.spec.steps[:-1]
        signature = tuple(step.identity_tuple() for step in steps)
    return {
        "control_id": "truncated_chain",
        "status": "evaluated" if signature else "unavailable",
        "truncated_signature": [list(item) for item in signature],
        "isolated_last_factor": False,
        "causal_claim": False,
    }


def _population_without_event(
    pairs: Sequence[_ScoredPair],
    predictions: Sequence[Mapping[str, Any]],
    *,
    episodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    matched = {_anchor_key(item) for item in pairs}
    sessions = {_venue_session(item) for item in pairs}
    without = 0
    for prediction in predictions:
        episode = episodes.get(_text(prediction.get("episode_id")) or "")
        contract = _prediction_contract(prediction, episode)
        if contract != CONTEXT_FEATURE_CONTRACT_ID:
            continue
        slot = _prediction_slot(prediction, episode)
        if slot is None:
            continue
        venue, symbol, as_of, horizon_id = slot
        session = f"{venue}:{as_of.date().isoformat()}"
        if session not in sessions:
            continue
        if (venue, symbol, as_of, horizon_id) in matched:
            continue
        without += 1
    return {
        "control_id": "comparable_population_without_event",
        "status": "evaluated" if sessions else "unavailable",
        "without_event_count": without,
        "causal_claim": False,
    }


def _report_negative_controls(
    assessments: Sequence[Mapping[str, Any]],
    *,
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    source = next((item for item in assessments if item.get("unique_support")), None)
    controls = {}
    for control_id in protocol["negative_controls"]:
        if source is not None:
            row = dict(source.get("negative_controls", {}).get(control_id) or {"control_id": control_id})
        else:
            row = {"control_id": control_id, "status": "unavailable", "causal_claim": False}
        row["control_id"] = control_id
        row["causal_claim"] = False
        controls[control_id] = row
    return controls


def _holm_bonferroni(items: Sequence[tuple[str, float]], *, alpha: float) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda item: (float(item[1]), item[0]))
    count = len(ordered)
    adjusted: list[dict[str, Any]] = []
    rejecting = True
    for index, (hypothesis_id, p_value) in enumerate(ordered):
        threshold = None if count == 0 else alpha / (count - index)
        rejected = bool(rejecting and threshold is not None and p_value <= threshold)
        if not rejected:
            rejecting = False
        adjusted.append(
            {
                "hypothesis_id": hypothesis_id,
                "raw_p": round(float(p_value), 8),
                "holm_rank": index + 1,
                "holm_threshold": threshold,
                "holm_rejected": rejected,
            }
        )
    return adjusted


def _report_conclusion(assessments: Sequence[Mapping[str, Any]], integrity_ok: bool) -> str:
    if not integrity_ok:
        return "inconclusive"
    conclusions = [str(item.get("conclusion")) for item in assessments]
    if any(item == "predictive_association_observed" for item in conclusions):
        return "predictive_association_observed"
    if conclusions and all(item == "invalidated" for item in conclusions):
        return "invalidated"
    if conclusions and all(item in {"not_supported", "invalidated"} for item in conclusions):
        return "not_supported"
    if any(item == "not_supported" for item in conclusions) and not any(item == "inconclusive" for item in conclusions):
        return "not_supported"
    return "inconclusive"


def _favorable_contexts(pairs: Sequence[_ScoredPair]) -> list[dict[str, Any]]:
    grouped: dict[str, list[_ScoredPair]] = defaultdict(list)
    for pair in pairs:
        grouped[_venue_session(pair)].append(pair)
    rows = []
    for session, items in sorted(grouped.items()):
        delta = _mean_delta(items)
        if delta < 0.0:
            rows.append({"venue_session": session, "mean_delta": round(delta, 8), "n": len(items)})
    return rows


def _counter_examples(pairs: Sequence[_ScoredPair]) -> list[dict[str, Any]]:
    rows = []
    for pair in pairs:
        if pair.predicted_class == pair.label_class:
            continue
        rows.append(
            {
                "occurrence_id": pair.occurrence_id,
                "predicted_class": pair.predicted_class,
                "label_class": pair.label_class,
                "path_signature": [list(item) for item in pair.path_signature],
            }
        )
    rows.sort(key=lambda item: item["occurrence_id"])
    return rows


def _mean_delta(pairs: Sequence[_ScoredPair]) -> float:
    deltas = [
        _log_loss(item.graph_probabilities, item.target_index)
        - _log_loss(item.context_probabilities, item.target_index)
        for item in pairs
    ]
    return sum(deltas) / len(deltas)


def _ece(pairs: Sequence[_ScoredPair]) -> float:
    bins: list[list[float]] = [[] for _ in range(5)]
    hits: list[list[float]] = [[] for _ in range(5)]
    for pair in pairs:
        predicted_index = CLASSES.index(pair.predicted_class)
        confidence = pair.graph_probabilities[predicted_index]
        bin_index = min(int(confidence * 5), 4)
        bins[bin_index].append(confidence)
        hits[bin_index].append(1.0 if predicted_index == pair.target_index else 0.0)
    count = len(pairs)
    return sum(
        (len(confidences) / count) * abs(sum(confidences) / len(confidences) - sum(hits[index]) / len(hits[index]))
        for index, confidences in enumerate(bins)
        if confidences
    )


def _prediction_slot(
    prediction: Mapping[str, Any],
    episode: Mapping[str, Any] | None = None,
) -> tuple[str, str, datetime, str] | None:
    source = episode or {}
    episode_observation = source.get("observation")
    if not isinstance(episode_observation, Mapping):
        episode_observation = source
    venue = _text(source.get("venue")) or _text(episode_observation.get("venue")) or _text(prediction.get("venue"))
    symbol = _text(source.get("symbol")) or _text(episode_observation.get("symbol")) or _text(prediction.get("symbol"))
    as_of = (
        _timestamp(source, "as_of_bar_ts")
        or _timestamp(episode_observation, "as_of_bar_ts")
        or _timestamp(prediction, "as_of_bar_ts")
    )
    horizon_id = _text(prediction.get("horizon_id") or prediction.get("horizon_code"))
    legacy_observation = prediction.get("input")
    if isinstance(legacy_observation, Mapping):
        venue = venue or _text(legacy_observation.get("venue"))
        symbol = symbol or _text(legacy_observation.get("symbol"))
        as_of = as_of or _timestamp(legacy_observation, "as_of_bar_ts")
    if venue is None or symbol is None or as_of is None or horizon_id is None:
        return None
    return (venue, symbol, as_of, horizon_id)


def _prediction_contract(
    prediction: Mapping[str, Any],
    episode: Mapping[str, Any] | None,
) -> str | None:
    contract = _text(prediction.get("feature_contract_id") or prediction.get("feature_contract_version"))
    if contract is not None or episode is None:
        return contract
    contract = _text(episode.get("feature_contract_id") or episode.get("feature_contract_version"))
    if contract is not None:
        return contract
    observation = episode.get("observation")
    if not isinstance(observation, Mapping):
        return None
    return _text(observation.get("feature_contract_id") or observation.get("feature_contract_version"))


def _instrument_slot(entity_id: str) -> tuple[str, str] | None:
    if not entity_id.startswith("mic:") or ":symbol:" not in entity_id:
        return None
    venue, symbol = entity_id[len("mic:") :].split(":symbol:", 1)
    if not venue or not symbol:
        return None
    return venue, symbol


def _anchor_key(pair: _ScoredPair) -> tuple[str, str, datetime]:
    return (pair.venue, pair.symbol, pair.cutoff_at)


def _venue_session(pair: _ScoredPair) -> str:
    return f"{pair.venue}:{pair.cutoff_at.date().isoformat()}"


def _forecast_probabilities(value: Mapping[str, Any]) -> tuple[float, float, float] | None:
    try:
        probabilities = tuple(float(value[label]) for label in CLASSES)
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in probabilities):
        return None
    if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-9):
        return None
    return probabilities  # type: ignore[return-value]


def _probabilities(value: Mapping[str, Any]) -> tuple[float, float, float] | None:
    raw = value.get("probabilities")
    nested = value.get("prediction")
    if raw is None and isinstance(nested, Mapping):
        raw = nested.get("probabilities")
    if isinstance(raw, Mapping):
        return _forecast_probabilities(raw)
    if isinstance(raw, (list, tuple)) and len(raw) == len(CLASSES):
        try:
            probabilities = tuple(float(item) for item in raw)
        except (TypeError, ValueError):
            return None
        if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in probabilities):
            return None
        if not math.isclose(sum(probabilities), 1.0, abs_tol=1e-9):
            return None
        return probabilities  # type: ignore[return-value]
    return None


def _log_loss(probabilities: Sequence[float], target_index: int) -> float:
    return -math.log(max(probabilities[target_index], 1e-15))


def _brier(probabilities: Sequence[float], target_index: int) -> float:
    return sum(
        (probability - (1.0 if index == target_index else 0.0)) ** 2 for index, probability in enumerate(probabilities)
    )


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


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


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


__all__ = [
    "ASSESSMENT_CONCLUSIONS",
    "PATTERN_ASSESSMENT_SCHEMA",
    "WORLD_PATTERN_REPORT_SCHEMA",
    "WORLD_PATTERN_STATUS_SCHEMA",
    "project_world_pattern_report",
    "project_world_pattern_status",
    "read_world_pattern_report",
    "read_world_pattern_status",
]
