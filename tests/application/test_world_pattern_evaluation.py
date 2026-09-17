from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from tests.application.test_world_pattern_discovery import (
    _binding,
    _instrument,
    _observes,
    _record,
    _regime_driver,
    _structural,
    _venue,
)
from tests.application.test_world_pattern_service import _MemoryPatternStore
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.pattern_evaluation import (
    PatternEvaluationService,
    shadow_pattern_prediction,
)
from trader.application.world_model.pattern_evaluation_ports import (
    PatternEvaluationBatch,
    PatternEvaluationRecord,
)
from trader.application.world_model.pattern_evaluation_request import (
    PatternEvaluationRequest,
    PatternEvaluationScanRequest,
)
from trader.application.world_model.pattern_path import project_pattern_path_details
from trader.application.world_model.pattern_service import WorldPatternService
from trader.domain.world_episode import WorldPrediction, canonical_sha256
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_ID,
    graph_content_mask,
    graph_feature_contract,
)
from trader.domain.world_pattern import (
    EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
    PATTERN_ASSOCIATION_METRIC,
    PATTERN_EVALUATION_HORIZON_IDS,
    PatternFormationStats,
    PatternHypothesis,
    PatternHypothesisSpec,
    PatternTarget,
)


UTC = timezone.utc
FORMATION = datetime(2026, 9, 1, tzinfo=UTC)
EVAL_NOT_BEFORE = datetime(2026, 9, 2, tzinfo=UTC)
EVAL_STARTED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
AS_OF = datetime(2026, 9, 10, tzinfo=UTC)
POST = datetime(2026, 9, 3, tzinfo=UTC)
PRE = datetime(2026, 8, 25, tzinfo=UTC)
COHORT_ID = "world_cohort:v1:" + "c" * 64
FORMATION_FP = canonical_sha256({"dataset": "formation-pilot"})
EVAL_FP = canonical_sha256({"dataset": "prospective-confirm"})

_EVALUATION = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_evaluation.py"
_PORTS = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_evaluation_ports.py"
_REQUEST = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_evaluation_request.py"
_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_path.py"


def _stats() -> PatternFormationStats:
    return PatternFormationStats(
        support=7,
        population_support=7,
        class_counts={"DOWN": 1, "FLAT": 2, "UP": 4},
        population_class_counts={"DOWN": 1, "FLAT": 2, "UP": 4},
        smoothing_alpha=1.0,
        association_metric=PATTERN_ASSOCIATION_METRIC,
        association_score=0.0,
    )


def _evaluating_hypothesis(*, steps, **overrides: object) -> PatternHypothesis:
    contract = graph_feature_contract()
    mask = graph_content_mask()
    values: dict[str, object] = {
        "evaluation_start_not_before": EVAL_NOT_BEFORE,
        "target": PatternTarget(
            entity_kind="instrument",
            horizon_id="elapsed_1d.v1",
            move_distribution={"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50},
        ),
        "steps": steps,
        "formation_cutoff": FORMATION,
        "formation_dataset_fingerprint": FORMATION_FP,
        "feature_contract_id": GRAPH_FEATURE_CONTRACT_ID,
        "feature_contract_fingerprint": contract.fingerprint,
        "feature_mask_id": mask.mask_id,
        "feature_mask_fingerprint": mask.fingerprint,
        "model_identity": EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
        "ontology_revision": "market_ontology.v1",
        "stats": _stats(),
        "source_refs": (),
        "causal_claim": False,
    }
    values.update(overrides)
    spec = PatternHypothesisSpec(**values)  # type: ignore[arg-type]
    return PatternHypothesis.register(spec, registered_at=FORMATION).start_evaluation(
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
        evaluation_cohort_id=COHORT_ID,
    )


def _eval_record(*, as_of: datetime, knowledge=(), extra_structural=(), driver_state_bindings=None, **kwargs):
    formation = _record(
        as_of=as_of,
        structural=(_structural("TRADED_ON", _instrument(), _venue()),),
        knowledge=knowledge,
        extra_structural=extra_structural,
        driver_state_bindings=driver_state_bindings,
        available_at=as_of,
        **kwargs,
    )
    return PatternEvaluationRecord(
        episode=formation.episode,
        snapshot=formation.snapshot,
        structural_relations=formation.structural_relations,
        knowledge_relations=formation.knowledge_relations,
        recorded_at=as_of,
        available_at=as_of,
        driver_state_bindings=formation.driver_state_bindings,
    )


@dataclass(frozen=True)
class MemoryCatalog:
    hypotheses: tuple[PatternHypothesis, ...]

    def list_evaluating_hypotheses(
        self,
        *,
        evaluation_cohort_id: str,
        evaluation_dataset_fingerprint: str,
        hypothesis_ids: tuple[str, ...] | None = None,
    ) -> tuple[PatternHypothesis, ...]:
        requested = None if hypothesis_ids is None else frozenset(hypothesis_ids)
        return tuple(
            item
            for item in self.hypotheses
            if item.status == "evaluating"
            and item.evaluation_cohort_id == evaluation_cohort_id
            and item.evaluation_dataset_fingerprint == evaluation_dataset_fingerprint
            and (requested is None or item.hypothesis_id in requested)
        )


@dataclass
class MemorySource:
    records: tuple[PatternEvaluationRecord, ...]
    last_request: PatternEvaluationScanRequest | None = None

    def load_evaluation_batch(self, request: PatternEvaluationScanRequest) -> PatternEvaluationBatch:
        self.last_request = request
        return PatternEvaluationBatch(
            records=self.records,
            rejection_counts={},
            source_evidence_ids=tuple(item.episode.episode_id for item in self.records),
        )


class MemoryPredictions:
    def __init__(self) -> None:
        self.items: list[WorldPrediction] = []

    def append_prediction(self, prediction: WorldPrediction) -> bool:
        self.items.append(prediction)
        return True


def _request(**overrides: object) -> PatternEvaluationRequest:
    values: dict[str, object] = {
        "as_of": AS_OF,
        "evaluation_cohort_id": COHORT_ID,
        "evaluation_dataset_fingerprint": EVAL_FP,
    }
    values.update(overrides)
    return PatternEvaluationRequest(**values)  # type: ignore[arg-type]


def _matched_pair():
    record = _eval_record(as_of=POST)
    steps = project_pattern_path_details(record)[0].steps
    hypothesis = _evaluating_hypothesis(steps=steps)
    return hypothesis, record


def test_evaluation_modules_never_read_or_name_outcome_leaves() -> None:
    for path in (_EVALUATION, _PORTS, _REQUEST, _PATH):
        source = path.read_text(encoding="utf-8")
        assert "WorldOutcome" not in source
        assert "world_outcome_events" not in source
        assert "move_class" not in source


def test_evaluate_forwards_evaluation_cohort_identity_on_the_scan() -> None:
    hypothesis, record = _matched_pair()
    source = MemorySource((record,))
    PatternEvaluationService(MemoryCatalog((hypothesis,)), source).evaluate(_request())
    assert source.last_request is not None
    assert source.last_request.evaluation_cohort_id == COHORT_ID
    assert source.last_request.as_of == AS_OF
    assert not hasattr(source.last_request, "evaluation_dataset_fingerprint")


def test_all_records_incompatible_are_counted_and_not_silent() -> None:
    hypothesis, record = _matched_pair()
    early = _eval_record(as_of=PRE)
    result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((early,))).evaluate(_request())
    assert result.considered_records == 1
    assert result.eligible_records == 0
    assert result.matches == ()
    assert result.rejection_counts.get("snapshot_not_after_formation_cutoff") == 1
    payload = result.to_dict()
    assert payload["considered_records"] == 1
    assert payload["eligible_records"] == 0


def test_cross_revision_record_matches_and_produces_occurrence() -> None:
    hypothesis, record = _matched_pair()
    drifted = _eval_record(as_of=POST, ontology_revision="market_ontology:v1:" + "f" * 64)
    result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((drifted,))).evaluate(_request())
    assert result.considered_records == 1
    assert result.eligible_records == 1
    assert len(result.matches) == 1
    assert result.matches[0].occurrence.cutoff_at == POST
    assert "ontology_revision_mismatch" not in result.rejection_counts


def test_zero_occurrence_evaluating_hypothesis_is_visible() -> None:
    record = _eval_record(as_of=PRE)
    steps = project_pattern_path_details(record)[0].steps
    hypothesis = _evaluating_hypothesis(steps=steps)
    result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource(())).evaluate(_request())
    assert result.hypotheses == (hypothesis,)
    assert result.matches == ()
    assert result.eligible_records == 0


def test_post_cutoff_fence_rejects_in_sample_and_pre_start_records() -> None:
    hypothesis, post = _matched_pair()
    pre = _eval_record(as_of=PRE)
    result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((pre, post))).evaluate(_request())
    assert len(result.matches) == 1
    assert result.matches[0].occurrence.cutoff_at == POST
    assert result.matches[0].occurrence.spec.expected_horizon_ids == PATTERN_EVALUATION_HORIZON_IDS
    assert result.rejection_counts.get("snapshot_not_after_formation_cutoff") == 1


def test_exact_driver_state_is_required_and_replay_is_deterministic() -> None:
    overlay = _observes(_venue(), digest="11" * 32, effective_from=POST - timedelta(hours=1))
    rising = _eval_record(
        as_of=POST,
        knowledge=(overlay,),
        driver_state_bindings=(_binding(overlay, _regime_driver(rates_regime="rising")),),
    )
    falling = _eval_record(
        as_of=POST,
        knowledge=(overlay,),
        driver_state_bindings=(_binding(overlay, _regime_driver(rates_regime="falling")),),
    )
    rising_steps = next(
        path.steps for path in project_pattern_path_details(rising) if path.steps[-1].relation_kind == "OBSERVES"
    )
    hypothesis = _evaluating_hypothesis(steps=rising_steps)
    rising_result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((rising,))).evaluate(
        _request()
    )
    falling_result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((falling,))).evaluate(
        _request()
    )
    assert len(rising_result.matches) == 1
    assert falling_result.matches == ()
    replay = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((rising,))).evaluate(_request())
    first = rising_result.matches[0]
    second = replay.matches[0]
    assert first.occurrence.occurrence_id == second.occurrence.occurrence_id
    assert first.prediction.prediction_id == second.prediction.prediction_id
    assert first.prediction.status == "shadow_only"
    assert first.prediction.authority == "shadow_only"
    assert first.prediction.recommendation == "NO_GO"
    assert first.prediction.decision_effect == "none"
    assert not hasattr(rising, "outcome")


def test_persist_writes_prediction_then_occurrence_without_labels() -> None:
    from trader.application.world_model.pattern_service import RegisterPatternHypothesis, StartPatternEvaluation
    from trader.domain.world_pattern import PatternOccurrenceId

    hypothesis, record = _matched_pair()
    store = _MemoryPatternStore()
    patterns = WorldPatternService(hypotheses=store, occurrences=store, availability=store)
    patterns.register(RegisterPatternHypothesis(spec=hypothesis.spec, registered_at=FORMATION))
    patterns.start(
        StartPatternEvaluation(
            hypothesis_id=hypothesis.hypothesis_id,
            evaluation_cohort_id=COHORT_ID,
            started_at=EVAL_STARTED,
            evaluation_dataset_fingerprint=EVAL_FP,
        )
    )
    result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((record,))).evaluate(_request())
    writer = MemoryPredictions()
    persisted = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((record,))).persist(
        result, patterns=patterns, predictions=writer
    )
    assert writer.items
    assert writer.items[0].prediction_id == result.matches[0].prediction.prediction_id
    assert persisted.persisted_occurrence_ids == (result.matches[0].occurrence.occurrence_id,)
    loaded = store.load(PatternOccurrenceId(result.matches[0].occurrence.occurrence_id))
    assert loaded.spec.expected_horizon_ids == PATTERN_EVALUATION_HORIZON_IDS
    for horizon_id in PATTERN_EVALUATION_HORIZON_IDS:
        assert loaded.active_outcome_link(horizon_id) is None


def test_scan_request_is_exclusive_on_the_lower_bound() -> None:
    with pytest.raises(ValueError, match="strictly later"):
        PatternEvaluationScanRequest(as_of=AS_OF, not_before=AS_OF, evaluation_cohort_id=COHORT_ID)
    request = PatternEvaluationScanRequest(
        as_of=AS_OF, not_before=EVAL_NOT_BEFORE, evaluation_cohort_id=COHORT_ID
    )
    assert request.not_before < request.as_of
    assert request.evaluation_cohort_id == COHORT_ID
    assert "evaluation_dataset_fingerprint" not in request.to_dict()


def test_shadow_prediction_is_built_from_formulated_distribution() -> None:
    hypothesis, record = _matched_pair()
    path = project_pattern_path_details(record)[0]
    prediction = shadow_pattern_prediction(hypothesis, record, path_signature=path.signature)
    assert prediction.model_id == EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY
    assert prediction.probabilities == {"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50}
    assert prediction.comparison_batch_id == COHORT_ID
    assert prediction.comparison_cohort_fingerprint == EVAL_FP
    assert prediction.created_at == record.snapshot.cutoff_at
