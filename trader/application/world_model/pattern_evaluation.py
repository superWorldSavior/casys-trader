"""Prospective matching of evaluating graph-pattern hypotheses.

Loads only evaluating hypotheses, scans unlabeled graph companions admitted
by that evaluation cohort's slots,
matches exact PatternStep identity including DriverState, and constructs
shadow WorldPrediction plus PatternOccurrence records before any label is
read. Persistence is a separate persist() step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trader.application.world_model.pattern_evaluation_ports import (
    EvaluatingHypothesisCatalog,
    PatternEvaluationBatch,
    PatternEvaluationRecord,
    PatternEvaluationSource,
    PatternPredictionWriter,
)
from trader.application.world_model.pattern_evaluation_request import (
    PatternEvaluationMatch,
    PatternEvaluationPersistResult,
    PatternEvaluationRequest,
    PatternEvaluationResult,
    PatternEvaluationScanRequest,
)
from trader.application.world_model.pattern_path import paths_matching_steps, pattern_path_signature
from trader.application.world_model.pattern_service import (
    PATTERN_AUTHORITY,
    PATTERN_DECISION_EFFECT,
    PATTERN_RECOMMENDATION,
    RecordPatternOccurrence,
    WorldPatternService,
)
from trader.domain.world_episode import PREDICTION_CLASSES, WorldPrediction, canonical_sha256
from trader.domain.world_graph import WorldEntityRef
from trader.domain.world_pattern import (
    EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
    PATTERN_EVALUATION_HORIZON_IDS,
    PatternHypothesis,
    PatternMatchedHop,
    PatternOccurrence,
)


PATTERN_EVALUATION_MODEL_VERSION = "v1"


def _clock_not_after(value: datetime | None, cutoff: datetime) -> bool:
    return value is not None and value <= cutoff


def _defensive_reject(
    record: PatternEvaluationRecord,
    hypothesis: PatternHypothesis,
    as_of: datetime,
) -> str | None:
    spec = hypothesis.spec
    episode = record.episode
    observation = episode.observation
    snapshot = record.snapshot
    if hypothesis.status != "evaluating":
        return "hypothesis_not_evaluating"
    if episode.training_eligible is not True:
        return "episode_not_training_eligible"
    if snapshot.cutoff_at <= spec.formation_cutoff:
        return "snapshot_not_after_formation_cutoff"
    if snapshot.cutoff_at <= spec.evaluation_start_not_before:
        return "snapshot_before_evaluation_start"
    if snapshot.cutoff_at > as_of:
        return "snapshot_after_as_of"
    if observation.as_of_bar_ts <= spec.formation_cutoff:
        return "as_of_not_after_formation_cutoff"
    if observation.as_of_bar_ts <= spec.evaluation_start_not_before:
        return "as_of_before_evaluation_start"
    if observation.as_of_bar_ts > as_of:
        return "as_of_after_scan"
    if not _clock_not_after(observation.available_at, as_of):
        return "episode_available_after_as_of"
    if not _clock_not_after(record.available_at, as_of):
        return "record_available_after_as_of"
    if record.recorded_at > as_of:
        return "record_recorded_after_as_of"
    if snapshot.ontology_revision != spec.ontology_revision:
        return "ontology_revision_mismatch"
    if observation.feature_contract_version != spec.feature_contract_id:
        return "feature_contract_mismatch"
    if not isinstance(snapshot.root_entity, WorldEntityRef) or snapshot.root_entity.kind != spec.target.entity_kind:
        return "instrument_kind_mismatch"
    return None


def _feature_hash(*, hypothesis_id: str, snapshot_id: str, snapshot_hash: str, path_signature: str) -> str:
    return canonical_sha256(
        {
            "hypothesis_id": hypothesis_id,
            "snapshot_id": snapshot_id,
            "snapshot_hash": snapshot_hash,
            "path_signature": path_signature,
        }
    )


def shadow_pattern_prediction(
    hypothesis: PatternHypothesis,
    record: PatternEvaluationRecord,
    *,
    path_signature: str,
) -> WorldPrediction:
    """Deterministic shadow forecast from the formulated distribution. No label."""

    spec = hypothesis.spec
    probabilities = {label: float(spec.target.move_distribution[label]) for label in PREDICTION_CLASSES}
    return WorldPrediction(
        episode_id=record.episode.episode_id,
        horizon_id=spec.target.horizon_id,
        model_id=EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
        model_version=PATTERN_EVALUATION_MODEL_VERSION,
        feature_hash=_feature_hash(
            hypothesis_id=hypothesis.hypothesis_id,
            snapshot_id=record.snapshot.snapshot_id or "",
            snapshot_hash=record.snapshot.content_sha256 or "",
            path_signature=path_signature,
        ),
        created_at=record.snapshot.cutoff_at,
        probabilities=probabilities,
        status="shadow_only",
        tier="exact",
        support=spec.stats.support,
        exact_support=spec.stats.support,
        training_cutoff=spec.formation_cutoff,
        model_fingerprint=spec.content_sha256,
        comparison_batch_id=hypothesis.evaluation_cohort_id,
        comparison_cohort_fingerprint=hypothesis.evaluation_dataset_fingerprint,
        recommendation=PATTERN_RECOMMENDATION,
        authority=PATTERN_AUTHORITY,
        decision_effect=PATTERN_DECISION_EFFECT,
    )


def _occurrence_for_match(
    hypothesis: PatternHypothesis,
    record: PatternEvaluationRecord,
    prediction: WorldPrediction,
    hops: tuple[PatternMatchedHop, ...],
) -> PatternOccurrence:
    if not isinstance(record.snapshot.root_entity, WorldEntityRef):
        raise TypeError("snapshot root_entity must be a WorldEntityRef")
    return PatternOccurrence.record(
        hypothesis,
        cohort_id=hypothesis.evaluation_cohort_id or "",
        instrument=record.snapshot.root_entity,
        cutoff_at=record.snapshot.cutoff_at,
        exact_path=hops,
        forecast=prediction,
        expected_horizon_ids=PATTERN_EVALUATION_HORIZON_IDS,
    )


@dataclass(frozen=True)
class PatternEvaluationService:
    catalog: EvaluatingHypothesisCatalog
    source: PatternEvaluationSource

    def evaluate(self, request: PatternEvaluationRequest) -> PatternEvaluationResult:
        if not isinstance(request, PatternEvaluationRequest):
            raise TypeError("PatternEvaluationRequest is required")
        hypotheses = self.catalog.list_evaluating_hypotheses(
            evaluation_cohort_id=request.evaluation_cohort_id,
            evaluation_dataset_fingerprint=request.evaluation_dataset_fingerprint,
            hypothesis_ids=request.hypothesis_ids,
        )
        rejection_counts: dict[str, int] = {}
        if not hypotheses:
            return PatternEvaluationResult(
                matches=(),
                hypotheses=(),
                eligible_records=0,
                rejection_counts=rejection_counts,
                source_evidence_ids=(),
                request=request,
            )
        for hypothesis in hypotheses:
            if hypothesis.status != "evaluating":
                raise ValueError("catalog must return only evaluating hypotheses")
            if hypothesis.evaluation_cohort_id != request.evaluation_cohort_id:
                raise ValueError("evaluation cohort_id does not match the started evaluation")
            if hypothesis.evaluation_dataset_fingerprint != request.evaluation_dataset_fingerprint:
                raise ValueError("evaluation dataset fingerprint does not match the started evaluation")
            if hypothesis.evaluation_dataset_fingerprint == hypothesis.spec.formation_dataset_fingerprint:
                raise ValueError("in-sample confirmation is forbidden: evaluation dataset must differ from formation")
            if spec_target_missing(hypothesis):
                raise ValueError("expected_horizon_ids must include the hypothesis target horizon")

        not_before = min(
            max(item.spec.formation_cutoff, item.spec.evaluation_start_not_before) for item in hypotheses
        )
        if not (not_before < request.as_of):
            return PatternEvaluationResult(
                matches=(),
                hypotheses=hypotheses,
                eligible_records=0,
                rejection_counts={"scan_window_empty": 1},
                source_evidence_ids=(),
                request=request,
            )
        batch = self.source.load_evaluation_batch(
            PatternEvaluationScanRequest(
                as_of=request.as_of,
                not_before=not_before,
                evaluation_cohort_id=request.evaluation_cohort_id,
            )
        )
        if not isinstance(batch, PatternEvaluationBatch):
            raise TypeError("source must return a PatternEvaluationBatch")
        rejection_counts.update(dict(batch.rejection_counts))
        matches: list[PatternEvaluationMatch] = []
        eligible = 0
        for record in batch.records:
            if not isinstance(record, PatternEvaluationRecord):
                raise TypeError("batch records must be PatternEvaluationRecord")
            if hasattr(record, "outcome"):
                raise ValueError("evaluation records must not carry an outcome")
            considered = False
            for hypothesis in hypotheses:
                reason = _defensive_reject(record, hypothesis, request.as_of)
                if reason is not None:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    continue
                considered = True
                matched_paths = paths_matching_steps(record, hypothesis.spec.steps)
                for path in matched_paths:
                    prediction = shadow_pattern_prediction(
                        hypothesis, record, path_signature=path.signature
                    )
                    occurrence = _occurrence_for_match(hypothesis, record, prediction, path.hops)
                    matches.append(
                        PatternEvaluationMatch(
                            hypothesis=hypothesis,
                            occurrence=occurrence,
                            prediction=prediction,
                            semantic_signature=pattern_path_signature(path.steps),
                        )
                    )
            if considered:
                eligible += 1
        matches.sort(
            key=lambda item: (
                item.hypothesis.hypothesis_id,
                item.occurrence.cutoff_at,
                item.occurrence.occurrence_id,
            )
        )
        return PatternEvaluationResult(
            matches=tuple(matches),
            hypotheses=hypotheses,
            eligible_records=eligible,
            rejection_counts=dict(sorted(rejection_counts.items())),
            source_evidence_ids=batch.source_evidence_ids,
            request=request,
        )

    def persist(
        self,
        result: PatternEvaluationResult,
        *,
        patterns: WorldPatternService,
        predictions: PatternPredictionWriter,
    ) -> PatternEvaluationPersistResult:
        if not isinstance(result, PatternEvaluationResult):
            raise TypeError("PatternEvaluationResult is required")
        prediction_ids: list[str] = []
        occurrence_ids: list[str] = []
        for match in result.matches:
            predictions.append_prediction(match.prediction)
            prediction_ids.append(match.prediction.prediction_id)
            envelope = patterns.record(
                RecordPatternOccurrence(
                    hypothesis_id=match.hypothesis.hypothesis_id,
                    cohort_id=match.occurrence.spec.cohort_id,
                    instrument=match.occurrence.instrument,
                    cutoff_at=match.occurrence.cutoff_at,
                    exact_path=match.occurrence.exact_path,
                    forecast=match.prediction,
                    expected_horizon_ids=PATTERN_EVALUATION_HORIZON_IDS,
                )
            )
            occurrence_ids.append(envelope.event.occurrence_id)
        return PatternEvaluationPersistResult(
            result=result,
            persisted_prediction_ids=tuple(prediction_ids),
            persisted_occurrence_ids=tuple(occurrence_ids),
        )


def spec_target_missing(hypothesis: PatternHypothesis) -> bool:
    return hypothesis.spec.target.horizon_id not in PATTERN_EVALUATION_HORIZON_IDS


__all__ = (
    "PATTERN_EVALUATION_MODEL_VERSION",
    "PatternEvaluationService",
    "shadow_pattern_prediction",
)
