from __future__ import annotations

import ast
import inspect
import json
import sys
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    WorldOutcome,
    WorldPrediction,
    canonical_sha256,
)
from trader.domain.world_feature_contract import WorldFeatureContract, WorldFeatureGroup, WorldFeatureMask
from trader.domain.world_driver import DriverRegimeBundle, DriverState
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    PatternHypothesisRef,
    WorldEntityRef,
)
from trader.domain.world_macro import MACRO_PRODUCER_VERSION, MACRO_TRANSFORM_VERSION
from trader.domain.world_pattern import (
    PATTERN_ASSOCIATION_METRIC,
    PATTERN_DISCOVERY_COMPLETED_EVENT_TYPE,
    PATTERN_DISCOVERY_COMPLETED_SCHEMA,
    PATTERN_EVALUATION_HORIZON_IDS,
    PATTERN_HYPOTHESIS_EVENT_TYPES,
    PATTERN_HYPOTHESIS_SCHEMA,
    PATTERN_HYPOTHESIS_STATUSES,
    PATTERN_OCCURRENCE_EVENT_TYPES,
    PATTERN_OCCURRENCE_SCHEMA,
    PATTERN_OUTCOME_LINK_SCHEMA,
    PatternDiscoveryCompleted,
    PatternEvaluationClosed,
    PatternEvaluationStarted,
    PatternForecast,
    PatternHypothesis,
    PatternHypothesisEventId,
    PatternHypothesisId,
    PatternHypothesisInvalidated,
    PatternHypothesisRegistered,
    PatternFormationStats,
    PatternHypothesisSpec,
    PatternMatchedHop,
    PatternOccurrence,
    PatternOccurrenceEventId,
    PatternOccurrenceId,
    PatternOccurrenceInvalidated,
    PatternOccurrenceRecorded,
    PatternOutcomeLink,
    PatternOutcomeLinked,
    PatternOutcomeLinkId,
    PatternOutcomeLinkSuperseded,
    PatternStep,
    PatternTarget,
    parse_pattern_hypothesis_event,
    parse_pattern_occurrence_event,
    reconcile_pattern_hypothesis,
    reconcile_pattern_occurrence,
)


UTC = timezone.utc
FORMATION = datetime(2026, 9, 1, tzinfo=UTC)
EVAL_NOT_BEFORE = datetime(2026, 9, 2, tzinfo=UTC)
EVAL_STARTED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 2, 13, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 9, 4, tzinfo=UTC)
OUTCOME_AT = datetime(2026, 9, 3, tzinfo=UTC)
EPISODE_ID = f"world-episode:v1:{'b' * 64}"
FORMATION_FP = canonical_sha256({"dataset": "formation-pilot"})
EVAL_FP = canonical_sha256({"dataset": "prospective-confirm"})
EVAL_COHORT = "world_cohort:graph_pilot"
SOURCE_SHA = "c" * 64
MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_pattern.py"


def _stats(**overrides: object) -> PatternFormationStats:
    values: dict[str, object] = {
        "support": 7,
        "population_support": 7,
        "class_counts": {"DOWN": 1, "FLAT": 2, "UP": 4},
        "population_class_counts": {"DOWN": 1, "FLAT": 2, "UP": 4},
        "smoothing_alpha": 1.0,
        "association_metric": PATTERN_ASSOCIATION_METRIC,
        "association_score": 0.0,
    }
    values.update(overrides)
    return PatternFormationStats(**values)  # type: ignore[arg-type]


def _contract() -> WorldFeatureContract:
    return WorldFeatureContract(
        contract_id="world_feature.graph.v1",
        accepted_episode_contract=MARKET_FEATURE_CONTRACT_ID,
        projection_version="graph_projection.v3",
        encoder_identity="world_feature_encoder.graph.v1",
        groups=(
            WorldFeatureGroup(
                group_id="market",
                categorical_features=frozenset({"venue"}),
                numeric_features=frozenset({"return"}),
            ),
        ),
        ontology_revision="market_ontology.v1",
        vocabulary_version="graph_vocab.v3",
        path_rule_version="macro_path_rule.v1",
    )


def _mask(contract: WorldFeatureContract | None = None) -> WorldFeatureMask:
    resolved = contract if contract is not None else _contract()
    return WorldFeatureMask.bind(resolved, mask_id="graph_content.v1", selected_groups=("market",))


def _regime_driver() -> DriverState:
    return DriverState(
        source_family="macro_observation",
        signal_class="regime_bundle",
        regimes=DriverRegimeBundle(
            macro_regime="unknown",
            rates_regime="rising",
            usd_regime="unknown",
            coverage_status="partial",
        ),
        artifact_kind="macro_world_observation",
        until_bound="bounded",
        producer_version=MACRO_PRODUCER_VERSION,
        transform_version=MACRO_TRANSFORM_VERSION,
        missingness="none",
    )


_UNSET = object()


def _step(
    ordinal: int,
    *,
    source_kind: str,
    relation_kind: str,
    target_kind: str,
    direction: str = "forward",
    freshness_bucket: str = "0-4h",
    driver_state: DriverState | None | object = _UNSET,
    family_ref: str | None = None,
) -> PatternStep:
    if driver_state is _UNSET:
        resolved: DriverState | None = (
            DriverState.unspecified(artifact_kind="news_macro")
            if relation_kind == "ABOUT"
            else _regime_driver()
            if relation_kind == "OBSERVES"
            else None
        )
    else:
        resolved = driver_state  # type: ignore[assignment]
    return PatternStep(
        ordinal=ordinal,
        source_kind=source_kind,
        relation_kind=relation_kind,
        direction=direction,
        target_kind=target_kind,
        freshness_bucket=freshness_bucket,
        evidence_rule_version="macro_path_rule.v1" if ordinal == 0 else "market_ontology.v1",
        driver_state=resolved,
        family_ref=family_ref,
    )


def _default_steps() -> tuple[PatternStep, PatternStep]:
    return (
        _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
        _step(1, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country", freshness_bucket="4-24h"),
    )


def _spec(**overrides: object) -> PatternHypothesisSpec:
    contract = _contract()
    mask = _mask(contract)
    values: dict[str, object] = {
        "evaluation_start_not_before": EVAL_NOT_BEFORE,
        "target": PatternTarget(
            entity_kind="instrument",
            horizon_id="elapsed_1d.v1",
            move_distribution={"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50},
        ),
        "steps": _default_steps(),
        "formation_cutoff": FORMATION,
        "formation_dataset_fingerprint": FORMATION_FP,
        "feature_contract_id": contract.contract_id,
        "feature_contract_fingerprint": contract.fingerprint,
        "feature_mask_id": mask.mask_id,
        "feature_mask_fingerprint": mask.fingerprint,
        "model_identity": "online_gru_world_challenger@graph.v1",
        "ontology_revision": "market_ontology.v1",
        "stats": _stats(),
        "source_refs": (),
        "causal_claim": False,
    }
    values.update(overrides)
    return PatternHypothesisSpec(**values)  # type: ignore[arg-type]


def _hypothesis(**overrides: object) -> PatternHypothesis:
    spec = overrides.pop("spec", _spec())
    registered_at = overrides.pop("registered_at", FORMATION)
    assert not overrides
    return PatternHypothesis.register(spec, registered_at=registered_at)  # type: ignore[arg-type]


def _evaluating(**overrides: object) -> PatternHypothesis:
    started_at = overrides.pop("started_at", EVAL_STARTED)
    evaluation_dataset_fingerprint = overrides.pop("evaluation_dataset_fingerprint", EVAL_FP)
    evaluation_cohort_id = overrides.pop("evaluation_cohort_id", EVAL_COHORT)
    return _hypothesis(**overrides).start_evaluation(
        started_at=started_at,  # type: ignore[arg-type]
        evaluation_dataset_fingerprint=evaluation_dataset_fingerprint,  # type: ignore[arg-type]
        evaluation_cohort_id=evaluation_cohort_id,  # type: ignore[arg-type]
    )


def _instrument() -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")


def _hop(step: PatternStep, *, evidence_refs: tuple[str, ...] = ()) -> PatternMatchedHop:
    return PatternMatchedHop(
        ordinal=step.ordinal,
        source_kind=step.source_kind,
        relation_kind=step.relation_kind,
        direction=step.direction,
        target_kind=step.target_kind,
        freshness_bucket=step.freshness_bucket,
        evidence_rule_version=step.evidence_rule_version,
        driver_state=step.driver_state,
        evidence_refs=evidence_refs,
    )


def _path() -> tuple[PatternMatchedHop, PatternMatchedHop]:
    first, second = _default_steps()
    return (
        _hop(first, evidence_refs=("macro_source_fact_version:v1:" + "d" * 64,)),
        _hop(second, evidence_refs=("world_observation:v1:" + "e" * 64,)),
    )


def _prediction(**overrides: object) -> WorldPrediction:
    values: dict[str, object] = {
        "episode_id": EPISODE_ID,
        "horizon_id": "elapsed_1d.v1",
        "model_id": "online_gru_world_challenger@graph.v1",
        "model_version": "graph.v1",
        "feature_hash": "feature-view",
        "created_at": CUTOFF,
        "probabilities": {"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50},
        "status": "shadow_only",
    }
    values.update(overrides)
    return WorldPrediction(**values)  # type: ignore[arg-type]


def _occurrence(hypothesis: PatternHypothesis | None = None, **overrides: object) -> PatternOccurrence:
    resolved = hypothesis if hypothesis is not None else _evaluating()
    values: dict[str, object] = {
        "cohort_id": "world_cohort:graph_pilot",
        "instrument": _instrument(),
        "cutoff_at": CUTOFF,
        "exact_path": _path(),
        "forecast": _prediction(),
        "artifact_refs": ("knowledge_artifact:v1:" + "a" * 64,),
        "fact_refs": ("macro_source_fact_version:v1:" + "d" * 64,),
    }
    values.update(overrides)
    return PatternOccurrence.record(resolved, **values)  # type: ignore[arg-type]


def _outcome(**overrides: object) -> WorldOutcome:
    values: dict[str, object] = {
        "episode_id": EPISODE_ID,
        "horizon": {
            "horizon_id": "elapsed_1d.v1",
            "duration_seconds": 24 * 60 * 60,
        },
        "status": "observed",
        "target_at": OUTCOME_AT,
        "available_at": datetime(2026, 9, 3, 0, 5, tzinfo=UTC),
        "computed_at": datetime(2026, 9, 3, 0, 5, tzinfo=UTC),
        "anchor_close": 100.0,
        "endpoint_close": 102.0,
        "endpoint_bar_ts": OUTCOME_AT,
        "source": "analysis_bars",
        "source_raw_sha256": SOURCE_SHA,
    }
    values.update(overrides)
    return WorldOutcome(**values)  # type: ignore[arg-type]


def test_ordered_chain_hash_changes_when_steps_are_permuted() -> None:
    first = _spec()
    permuted = _spec(
        steps=(
            _step(0, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country", freshness_bucket="4-24h"),
            _step(1, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
        )
    )
    assert first.hypothesis_id != permuted.hypothesis_id
    assert first.content_sha256 != permuted.content_sha256
    assert first.hypothesis_id.startswith("pattern_hypothesis:v1:")
    replayed = PatternHypothesisSpec.from_mapping(first.to_dict())
    assert replayed == first
    assert replayed.hypothesis_id == first.hypothesis_id
    assert first.to_dict()["schema_version"] == PATTERN_HYPOTHESIS_SCHEMA
    assert first.causal_claim is False
    PatternHypothesisRef(hypothesis_id=first.hypothesis_id)


def test_definition_change_under_the_same_id_is_a_conflict() -> None:
    spec = _spec()
    payload = spec.to_dict()
    payload["steps"] = [
        step.to_dict()
        for step in _spec(
            steps=_default_steps()[:1]
            + (_step(1, source_kind="venue", relation_kind="TRADED_ON", target_kind="instrument", direction="reverse"),)
        ).steps
    ]
    with pytest.raises(ValueError, match="hypothesis_id"):
        PatternHypothesisSpec.from_mapping(payload)
    duplicate = PatternHypothesis.register(spec, registered_at=FORMATION)
    assert reconcile_pattern_hypothesis(duplicate, _hypothesis()) == duplicate
    shifted = PatternHypothesis.register(spec, registered_at=datetime(2026, 9, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="conflict"):
        reconcile_pattern_hypothesis(duplicate, shifted)


def test_hypothesis_lifecycle_allows_only_the_closed_transitions() -> None:
    registered = _hypothesis()
    assert registered.status == "registered"
    assert PATTERN_HYPOTHESIS_STATUSES == frozenset({"registered", "evaluating", "evaluation_closed", "invalidated"})
    evaluating = registered.start_evaluation(
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
        evaluation_cohort_id=EVAL_COHORT,
    )
    assert evaluating.status == "evaluating"
    assert evaluating.evaluation_dataset_fingerprint == EVAL_FP
    closed = evaluating.close_evaluation(closed_at=CLOSED_AT)
    assert closed.status == "evaluation_closed"
    from_registered = registered.invalidate(invalidated_at=EVAL_STARTED, reason="abandoned")
    assert from_registered.status == "invalidated"
    from_evaluating = evaluating.invalidate(invalidated_at=CLOSED_AT, reason="not_supported")
    assert from_evaluating.status == "invalidated"
    assert {type(event) for event in closed.events} <= {
        PatternHypothesisRegistered,
        PatternEvaluationStarted,
        PatternEvaluationClosed,
    }
    assert not hasattr(PatternHypothesis, "record_occurrence")
    assert {item.name for item in fields(PatternHypothesis)} == {"events"}
    with pytest.raises(ValueError, match="start|registered"):
        closed.start_evaluation(
            started_at=CLOSED_AT,
            evaluation_dataset_fingerprint=EVAL_FP,
            evaluation_cohort_id=EVAL_COHORT,
        )
    with pytest.raises(ValueError, match="close|evaluating"):
        registered.close_evaluation(closed_at=CLOSED_AT)
    with pytest.raises(ValueError, match="invalidat"):
        closed.invalidate(invalidated_at=CLOSED_AT, reason="too-late")
    with pytest.raises(ValueError, match="after|terminal|closed|invalidat"):
        PatternHypothesis.from_events(
            (
                *closed.events,
                PatternHypothesisInvalidated(
                    hypothesis_id=closed.hypothesis_id,
                    invalidated_at=CLOSED_AT,
                    reason="late",
                ),
            )
        )
    with pytest.raises(ValueError, match="unknown|event_type"):
        parse_pattern_hypothesis_event({"event_type": "pattern_occurrence_recorded", "schema_version": "x"})
    assert PATTERN_HYPOTHESIS_EVENT_TYPES == frozenset(
        {
            "pattern_hypothesis_registered",
            "pattern_evaluation_started",
            "pattern_evaluation_closed",
            "pattern_hypothesis_invalidated",
        }
    )


def test_formation_cutoff_must_precede_evaluation_and_in_sample_confirmation_is_rejected() -> None:
    with pytest.raises(ValueError, match="formation|evaluation"):
        _spec(evaluation_start_not_before=FORMATION)
    with pytest.raises(ValueError, match="formation|evaluation"):
        _spec(formation_cutoff=EVAL_NOT_BEFORE, evaluation_start_not_before=FORMATION)
    registered = _hypothesis()
    with pytest.raises(ValueError, match="in-sample|dataset|confirmation"):
        registered.start_evaluation(
            started_at=EVAL_STARTED,
            evaluation_dataset_fingerprint=FORMATION_FP,
            evaluation_cohort_id=EVAL_COHORT,
        )
    with pytest.raises(ValueError, match="evaluation_start_not_before|before"):
        registered.start_evaluation(
            started_at=FORMATION,
            evaluation_dataset_fingerprint=EVAL_FP,
            evaluation_cohort_id=EVAL_COHORT,
        )
    started = registered.start_evaluation(
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
        evaluation_cohort_id=EVAL_COHORT,
    )
    assert (
        started.start_evaluation(
            started_at=EVAL_STARTED,
            evaluation_dataset_fingerprint=EVAL_FP,
            evaluation_cohort_id=EVAL_COHORT,
        )
        == started
    )
    with pytest.raises(ValueError, match="conflict|dataset"):
        started.start_evaluation(
            started_at=EVAL_STARTED,
            evaluation_dataset_fingerprint=canonical_sha256({"dataset": "other"}),
            evaluation_cohort_id=EVAL_COHORT,
        )


def test_occurrence_cannot_be_recorded_before_evaluation_start_or_on_formation_cutoff() -> None:
    with pytest.raises(ValueError, match="before|start|evaluating"):
        _occurrence(hypothesis=_hypothesis())
    evaluating = _evaluating()
    with pytest.raises(ValueError, match="before|evaluation"):
        _occurrence(evaluating, cutoff_at=FORMATION)
    with pytest.raises(ValueError, match="before|evaluation"):
        _occurrence(evaluating, cutoff_at=datetime(2026, 9, 1, 12, tzinfo=UTC))
    closed = evaluating.close_evaluation(closed_at=CLOSED_AT)
    with pytest.raises(ValueError, match="evaluating|closed"):
        _occurrence(closed)
    invalidated = evaluating.invalidate(invalidated_at=CLOSED_AT, reason="invalidated")
    with pytest.raises(ValueError, match="evaluating|invalidat"):
        _occurrence(invalidated)


def test_occurrence_is_a_separate_bounded_aggregate_matching_hypothesis_fingerprints() -> None:
    hypothesis = _evaluating()
    occurrence = _occurrence(hypothesis)
    assert occurrence.hypothesis_id == hypothesis.hypothesis_id
    assert occurrence.status == "recorded"
    assert occurrence.feature_contract_id == hypothesis.spec.feature_contract_id
    assert occurrence.feature_contract_fingerprint == hypothesis.spec.feature_contract_fingerprint
    assert occurrence.feature_mask_id == hypothesis.spec.feature_mask_id
    assert occurrence.feature_mask_fingerprint == hypothesis.spec.feature_mask_fingerprint
    assert occurrence.evaluation_dataset_fingerprint == EVAL_FP
    assert occurrence.evaluation_dataset_fingerprint != hypothesis.spec.formation_dataset_fingerprint
    assert occurrence.cutoff_at == CUTOFF
    assert occurrence.instrument == _instrument()
    assert occurrence.to_dict()["schema_version"] == PATTERN_OCCURRENCE_SCHEMA
    assert "ready_at" not in occurrence.to_dict()
    assert "ready_at" not in inspect.signature(PatternOccurrence.record).parameters
    assert {item.name for item in fields(PatternOccurrence)} == {"events"}
    assert isinstance(occurrence.events[0], PatternOccurrenceRecorded)
    assert {type(event) for event in hypothesis.events}.isdisjoint(
        {PatternOccurrenceRecorded, PatternOutcomeLinked, PatternOccurrenceInvalidated}
    )
    rebuilt = PatternOccurrence.from_events([event.to_dict() for event in occurrence.events])
    assert rebuilt == occurrence
    assert PatternHypothesisRef(hypothesis_id=occurrence.hypothesis_id).hypothesis_id == hypothesis.hypothesis_id
    with pytest.raises(ValueError, match="fingerprint|contract|mask"):
        _occurrence(
            hypothesis,
            feature_contract_fingerprint="0" * 64,
        )
    with pytest.raises(ValueError, match="instrument|namespaced|mic"):
        _occurrence(hypothesis, instrument=WorldEntityRef(kind="venue", entity_id="mic:XTAI"))
    with pytest.raises(ValueError, match="dataset|in-sample|confirmation"):
        PatternOccurrence.record(
            hypothesis,
            cohort_id="world_cohort:graph_pilot",
            instrument=_instrument(),
            cutoff_at=CUTOFF,
            exact_path=_path(),
            forecast=_prediction(),
            evaluation_dataset_fingerprint=FORMATION_FP,
        )


def test_outcome_cannot_be_linked_before_occurrence_and_does_not_copy_labels() -> None:
    with pytest.raises(ValueError, match="recorded|first"):
        PatternOccurrence.from_events(
            (
                PatternOutcomeLinked(
                    link=PatternOutcomeLink(
                        occurrence_id=f"pattern_occurrence:v1:{'a' * 64}",
                        horizon_id="elapsed_1d.v1",
                        world_outcome_event_id=_outcome().event_id,
                        world_outcome_content_sha256=_outcome().payload_hash,
                    )
                ),
            )
        )
    occurrence = _occurrence()
    outcome = _outcome()
    linked = occurrence.link_outcome(outcome)
    leaf = linked.active_outcome_link("elapsed_1d.v1")
    assert leaf is not None
    assert leaf.schema_version == PATTERN_OUTCOME_LINK_SCHEMA
    assert leaf.occurrence_id == occurrence.occurrence_id
    assert leaf.horizon_id == "elapsed_1d.v1"
    assert leaf.world_outcome_event_id == outcome.event_id
    assert leaf.world_outcome_content_sha256 == outcome.payload_hash
    assert leaf.supersedes_link_id is None
    payload = leaf.to_dict()
    for forbidden in (
        "move_class",
        "direction",
        "simple_return",
        "anchor_close",
        "endpoint_close",
        "target_at",
        "available_at",
        "computed_at",
        "endpoint_bar_ts",
        "training_eligible",
        "status",
        "label",
        "ready_at",
    ):
        assert forbidden not in payload
    with pytest.raises(ValueError, match="move_class|label|forbidden"):
        PatternOutcomeLink.from_mapping({**payload, "move_class": "UP"})
    with pytest.raises(TypeError, match="WorldOutcome"):
        occurrence.link_outcome(outcome.to_dict())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="horizon"):
        occurrence.link_outcome(_outcome(horizon={"horizon_id": "elapsed_4h.v1", "duration_seconds": 4 * 60 * 60}))
    assert linked.link_outcome(outcome) == linked


def test_outcome_correction_requires_explicit_supersession_and_keeps_one_leaf() -> None:
    occurrence = _occurrence().link_outcome(_outcome())
    current = occurrence.active_outcome_link("elapsed_1d.v1")
    assert current is not None
    with pytest.raises(ValueError, match="supersed"):
        occurrence.link_outcome(
            _outcome(endpoint_close=99.0, source_raw_sha256="d" * 64, supersedes_event_id=_outcome().event_id)
        )
    corrected = _outcome(
        endpoint_close=99.0,
        source_raw_sha256="d" * 64,
        supersedes_event_id=current.world_outcome_event_id,
    )
    superseded = occurrence.supersede_outcome_link(corrected)
    leaf = superseded.active_outcome_link("elapsed_1d.v1")
    assert leaf is not None
    assert leaf.link_id != current.link_id
    assert leaf.supersedes_link_id == current.link_id
    assert leaf.world_outcome_event_id == corrected.event_id
    assert leaf.world_outcome_content_sha256 == corrected.payload_hash
    assert isinstance(superseded.events[-1], PatternOutcomeLinkSuperseded)
    assert [link.link_id for link in superseded.outcome_links] == [current.link_id, leaf.link_id]
    with pytest.raises(ValueError, match="supersed"):
        superseded.supersede_outcome_link(
            _outcome(
                endpoint_close=101.0,
                source_raw_sha256="e" * 64,
                supersedes_event_id=_outcome().event_id,
            )
        )
    closed_hypothesis = _evaluating().close_evaluation(closed_at=CLOSED_AT)
    after_close = _occurrence(_evaluating()).link_outcome(_outcome())
    corrected_after_close = after_close.supersede_outcome_link(corrected)
    assert corrected_after_close.active_outcome_link("elapsed_1d.v1") is not None
    assert closed_hypothesis.status == "evaluation_closed"
    invalidated = after_close.invalidate(invalidated_at=CLOSED_AT, reason="counterexample")
    assert invalidated.status == "invalidated"
    with pytest.raises(ValueError, match="invalidat"):
        invalidated.link_outcome(_outcome())
    with pytest.raises(ValueError, match="invalidat"):
        invalidated.supersede_outcome_link(corrected)


def test_events_round_trip_and_aggregates_stay_stdlib_domain() -> None:
    hypothesis = _evaluating().close_evaluation(closed_at=CLOSED_AT)
    parsed_h = [parse_pattern_hypothesis_event(event.to_dict()) for event in hypothesis.events]
    assert [type(event) for event in parsed_h] == [
        PatternHypothesisRegistered,
        PatternEvaluationStarted,
        PatternEvaluationClosed,
    ]
    assert PatternHypothesis.from_events(parsed_h) == hypothesis
    assert isinstance(PatternHypothesisId(hypothesis.hypothesis_id).value, str)
    assert isinstance(PatternHypothesisEventId(hypothesis.events[0].event_id).value, str)
    occurrence = _occurrence().link_outcome(_outcome())
    parsed_o = [parse_pattern_occurrence_event(json.loads(json.dumps(event.to_dict()))) for event in occurrence.events]
    assert [type(event) for event in parsed_o] == [PatternOccurrenceRecorded, PatternOutcomeLinked]
    rebuilt = PatternOccurrence.from_events(parsed_o)
    assert rebuilt == occurrence
    assert isinstance(PatternOccurrenceId(occurrence.occurrence_id).value, str)
    assert isinstance(PatternOccurrenceEventId(occurrence.events[0].event_id).value, str)
    assert isinstance(PatternOutcomeLinkId(occurrence.active_outcome_link("elapsed_1d.v1").link_id).value, str)
    assert PATTERN_OCCURRENCE_EVENT_TYPES == frozenset(
        {
            "pattern_occurrence_recorded",
            "pattern_outcome_linked",
            "pattern_outcome_link_superseded",
            "pattern_occurrence_invalidated",
        }
    )
    with pytest.raises(ValueError, match="unknown|event_type"):
        parse_pattern_occurrence_event({"event_type": "pattern_hypothesis_registered"})
    assert reconcile_pattern_occurrence(occurrence, rebuilt) == occurrence
    mutated = dict(occurrence.events[0].to_dict())
    mutated["occurrence"]["cutoff_at"] = "2026-09-02T18:00:00+00:00"
    with pytest.raises(ValueError, match="occurrence_id"):
        parse_pattern_occurrence_event(mutated)
    path = MODULE_PATH
    source = path.read_text(encoding="utf-8")
    assert "networkx" not in source.lower()
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert "trader.application" not in source
    assert "trader.reporting" not in source
    assert _domain_import_violations([path], REPO_ROOT) == []
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if node.module.startswith("trader.") and not node.module.startswith("trader.domain"):
                violations.append(node.module)
            elif root != "trader" and root not in sys.stdlib_module_names:
                violations.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root != "trader" and root not in sys.stdlib_module_names:
                    violations.append(alias.name)
    assert violations == []
    with pytest.raises(FrozenInstanceError):
        hypothesis.events = ()  # type: ignore[misc]
    with pytest.raises((TypeError, AttributeError)):
        occurrence.exact_path[0].evidence_refs.append("x")  # type: ignore[attr-defined]
    for kind in tuple(FORBIDDEN_RELATION_KINDS):
        with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
            _step(0, source_kind="instrument", relation_kind=kind, target_kind="venue")
    legal_hops = (
        ("region", "PART_OF_WORLD", "world", "forward"),
        ("venue", "LOCATED_IN", "country", "forward"),
        ("country", "LOCATED_IN", "venue", "reverse"),
        ("instrument", "TRADED_ON", "venue", "forward"),
        ("venue", "TRADED_ON", "instrument", "reverse"),
        ("instrument", "ISSUED_BY", "company", "forward"),
        ("instrument", "MEMBER_OF_FAMILY", "family", "forward"),
        ("knowledge_artifact", "ABOUT", "country", "forward"),
        ("country", "ABOUT", "knowledge_artifact", "reverse"),
        ("world_observation", "OBSERVES", "country", "forward"),
        ("country", "OBSERVES", "world_observation", "reverse"),
        ("knowledge_artifact", "DERIVED_FROM", "macro_source_fact_version", "forward"),
        ("world_graph_snapshot", "USES", "knowledge_artifact", "forward"),
        ("knowledge_artifact", "SUPERSEDES", "knowledge_artifact", "forward"),
    )
    for source_kind, relation_kind, target_kind, direction in legal_hops:
        hop = _step(
            0,
            source_kind=source_kind,
            relation_kind=relation_kind,
            target_kind=target_kind,
            direction=direction,
            family_ref="v1:test" if relation_kind == "MEMBER_OF_FAMILY" else None,
        )
        assert hop.relation_kind == relation_kind
        assert hop.direction == direction
    with pytest.raises(ValueError, match="pair|source/target"):
        _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="company")
    with pytest.raises(ValueError, match="pair|source/target"):
        _step(0, source_kind="instrument", relation_kind="LOCATED_IN", target_kind="venue")
    with pytest.raises(ValueError, match="pair|source/target"):
        _step(0, source_kind="instrument", relation_kind="OBSERVES", target_kind="country")
    with pytest.raises(ValueError, match="pair|source/target"):
        _step(0, source_kind="world_observation", relation_kind="ABOUT", target_kind="country")
    with pytest.raises(ValueError, match="source_kind|path"):
        _step(0, source_kind="sensor", relation_kind="OBSERVES", target_kind="country")
    with pytest.raises(ValueError, match="direction|ISSUED_BY|graph_traversal"):
        _step(0, source_kind="company", relation_kind="ISSUED_BY", target_kind="instrument", direction="reverse")
    with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
        _step(0, source_kind="macro_indicator", relation_kind="HypothesizedInfluence", target_kind="country")
    legal_payload = _default_steps()[0].to_dict()
    for legacy_key, legacy_value in (
        ("predicate", "TRADED_ON"),
        ("lag_window", "0h..24h"),
        ("subject_kind", "instrument"),
        ("object_kind", "venue"),
    ):
        with pytest.raises(ValueError, match="ID-free|predicate|lag_window|subject_kind|object_kind"):
            PatternStep.from_mapping({**legal_payload, legacy_key: legacy_value})
    replayed_step = PatternStep.from_mapping(_default_steps()[0].to_dict())
    assert replayed_step == _default_steps()[0]
    assert list(replayed_step.to_dict()) == [
        "ordinal",
        "source_kind",
        "relation_kind",
        "direction",
        "target_kind",
        "freshness_bucket",
        "evidence_rule_version",
        "driver_state",
        "family_ref",
    ]
    assert replayed_step.driver_state is None
    forecast = PatternForecast.from_world_prediction(
        _prediction(),
        model_identity="online_gru_world_challenger@graph.v1",
    )
    assert forecast.prediction_id == _prediction().prediction_id
    assert forecast.episode_id == EPISODE_ID
    with pytest.raises(ValueError, match="lineage|model"):
        _occurrence(forecast=_prediction(model_id="other-model"))


def test_instrument_venue_country_world_observation_is_an_exact_path_match() -> None:
    steps = (
        _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
        _step(1, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country"),
        _step(
            2,
            source_kind="country",
            relation_kind="OBSERVES",
            target_kind="world_observation",
            direction="reverse",
            freshness_bucket="1-7d",
        ),
    )
    hypothesis = _evaluating(spec=_spec(steps=steps))
    path = tuple(_hop(step) for step in steps)
    occurrence = _occurrence(hypothesis, exact_path=path)
    assert [hop.target_kind for hop in occurrence.exact_path] == ["venue", "country", "world_observation"]
    assert occurrence.exact_path[2].direction == "reverse"
    assert occurrence.exact_path[2].freshness_bucket == "1-7d"
    rebuilt = PatternMatchedHop.from_mapping(occurrence.exact_path[2].to_dict())
    assert rebuilt.identity_tuple() == steps[2].identity_tuple()
    assert rebuilt.evidence_refs == ()
    assert rebuilt.driver_state == steps[2].driver_state
    assert rebuilt.driver_state is not None
    assert "evidence_refs" not in rebuilt.driver_state.to_dict()


def test_legacy_hop_keys_are_rejected_on_pattern_hypothesis_v1() -> None:
    legal = _default_steps()[0].to_dict()
    for legacy_key in ("predicate", "lag_window", "subject_kind", "object_kind"):
        payload = dict(legal)
        payload[legacy_key] = legal.get("relation_kind" if legacy_key == "predicate" else "source_kind", "instrument")
        with pytest.raises(ValueError, match="ID-free|predicate|lag_window|subject_kind|object_kind"):
            PatternStep.from_mapping(payload)
        with pytest.raises(ValueError, match="ID-free|predicate|lag_window|subject_kind|object_kind"):
            PatternMatchedHop.from_mapping({**payload, "evidence_refs": ()})
    assert not hasattr(_default_steps()[0], "subject_kind")
    assert not hasattr(_default_steps()[0], "predicate")
    assert not hasattr(_default_steps()[0], "object_kind")


def test_causes_is_rejected_and_direction_or_freshness_mismatch_is_not_exact() -> None:
    with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
        PatternStep(
            ordinal=0,
            source_kind="instrument",
            relation_kind="CAUSES",
            direction="forward",
            target_kind="venue",
            freshness_bucket="0-4h",
            evidence_rule_version="macro_path_rule.v1",
        )
    with pytest.raises(ValueError, match="CAUSES|forbidden|causal"):
        PatternMatchedHop.from_mapping(
            {
                "ordinal": 0,
                "source_kind": "instrument",
                "relation_kind": "CAUSED_BY",
                "direction": "forward",
                "target_kind": "venue",
                "freshness_bucket": "0-4h",
                "evidence_rule_version": "macro_path_rule.v1",
            }
        )
    hypothesis = _evaluating()
    first, second = _default_steps()
    reversed_direction = PatternMatchedHop(
        ordinal=first.ordinal,
        source_kind=first.target_kind,
        relation_kind=first.relation_kind,
        direction="reverse",
        target_kind=first.source_kind,
        freshness_bucket=first.freshness_bucket,
        evidence_rule_version=first.evidence_rule_version,
    )
    with pytest.raises(ValueError, match="exact_path|match"):
        _occurrence(hypothesis, exact_path=(reversed_direction, _hop(second)))
    stale = PatternMatchedHop(
        ordinal=second.ordinal,
        source_kind=second.source_kind,
        relation_kind=second.relation_kind,
        direction=second.direction,
        target_kind=second.target_kind,
        freshness_bucket="older",
        evidence_rule_version=second.evidence_rule_version,
    )
    with pytest.raises(ValueError, match="exact_path|match"):
        _occurrence(hypothesis, exact_path=(_hop(first), stale))


def test_formation_stats_invariants_and_spec_roundtrip() -> None:
    from trader.domain.world_pattern import laplace_smoothed_distribution, total_variation_distance

    class_counts = {"DOWN": 3, "FLAT": 1, "UP": 0}
    population = {"DOWN": 5, "FLAT": 5, "UP": 10}
    pattern = laplace_smoothed_distribution(class_counts, alpha=1.0)
    pop = laplace_smoothed_distribution(population, alpha=1.0)
    score = total_variation_distance(pattern, pop)
    assert pattern["DOWN"] == pytest.approx(4 / 7)
    assert pattern["FLAT"] == pytest.approx(2 / 7)
    assert pattern["UP"] == pytest.approx(1 / 7)
    assert pop["DOWN"] == pytest.approx(6 / 23)
    assert score == pytest.approx(0.5 * sum(abs(pattern[label] - pop[label]) for label in ("DOWN", "FLAT", "UP")))
    stats = PatternFormationStats(
        support=4,
        population_support=20,
        class_counts=class_counts,
        population_class_counts=population,
        smoothing_alpha=1.0,
        association_metric=PATTERN_ASSOCIATION_METRIC,
        association_score=score,
    )
    replayed = PatternFormationStats.from_mapping(stats.to_dict())
    assert replayed == stats
    with pytest.raises(ValueError, match="population_support"):
        PatternFormationStats(
            support=5,
            population_support=4,
            class_counts={"DOWN": 1, "FLAT": 2, "UP": 2},
            population_class_counts={"DOWN": 1, "FLAT": 1, "UP": 2},
            smoothing_alpha=1.0,
            association_metric=PATTERN_ASSOCIATION_METRIC,
            association_score=0.0,
        )
    with pytest.raises(ValueError, match="association_metric|total_variation"):
        PatternFormationStats(
            support=4,
            population_support=20,
            class_counts=class_counts,
            population_class_counts=population,
            smoothing_alpha=1.0,
            association_metric="kl.v1",
            association_score=score,
        )
    with pytest.raises(ValueError, match="association_score"):
        PatternFormationStats(
            support=4,
            population_support=20,
            class_counts=class_counts,
            population_class_counts=population,
            smoothing_alpha=1.0,
            association_metric=PATTERN_ASSOCIATION_METRIC,
            association_score=0.99,
        )
    spec = _spec(
        target=PatternTarget(entity_kind="instrument", horizon_id="elapsed_1d.v1", move_distribution=pattern),
        stats=stats,
    )
    assert spec.stats.association_metric == PATTERN_ASSOCIATION_METRIC
    assert PatternHypothesisSpec.from_mapping(spec.to_dict()) == spec
    with pytest.raises(ValueError, match="move_distribution"):
        _spec(stats=stats)
    with pytest.raises((TypeError, ValueError)):
        PatternHypothesisSpec.from_mapping({k: v for k, v in spec.to_dict().items() if k != "stats"})


def test_evaluation_cohort_is_required_identity_and_binds_occurrences() -> None:
    registered = _hypothesis()
    with pytest.raises((TypeError, ValueError)):
        registered.start_evaluation(started_at=EVAL_STARTED, evaluation_dataset_fingerprint=EVAL_FP)
    started = registered.start_evaluation(
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
        evaluation_cohort_id=EVAL_COHORT,
    )
    assert started.evaluation_cohort_id == EVAL_COHORT
    started_payload = next(event for event in started.events if isinstance(event, PatternEvaluationStarted)).to_dict()
    assert started_payload["evaluation_cohort_id"] == EVAL_COHORT
    other = registered.start_evaluation(
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
        evaluation_cohort_id="world_cohort:other",
    )
    assert other.evaluation_cohort_id == "world_cohort:other"
    assert other.events[-1].event_id != started.events[-1].event_id
    with pytest.raises(ValueError, match="evaluation_cohort_id|cohort"):
        _occurrence(started, cohort_id="world_cohort:other")
    occurrence = _occurrence(started, cohort_id=EVAL_COHORT)
    assert occurrence.spec.cohort_id == EVAL_COHORT
    with pytest.raises((TypeError, ValueError)):
        PatternEvaluationStarted.from_mapping(
            {
                "hypothesis_id": started.hypothesis_id,
                "started_at": EVAL_STARTED.isoformat(),
                "evaluation_dataset_fingerprint": EVAL_FP,
            }
        )
    with pytest.raises(ValueError, match="conflict|dataset"):
        started.start_evaluation(
            started_at=EVAL_STARTED,
            evaluation_dataset_fingerprint=EVAL_FP,
            evaluation_cohort_id="world_cohort:other",
        )


def test_overlay_hops_require_driver_state_and_structural_hops_forbid_it() -> None:
    with pytest.raises(ValueError, match="OBSERVES/ABOUT|DriverState"):
        PatternStep(
            ordinal=0,
            source_kind="world_observation",
            relation_kind="OBSERVES",
            direction="forward",
            target_kind="country",
            freshness_bucket="0-4h",
            evidence_rule_version="macro_path_rule.v1",
        )
    with pytest.raises(ValueError, match="OBSERVES/ABOUT|DriverState"):
        PatternStep(
            ordinal=0,
            source_kind="knowledge_artifact",
            relation_kind="ABOUT",
            direction="forward",
            target_kind="country",
            freshness_bucket="0-4h",
            evidence_rule_version="macro_path_rule.v1",
            driver_state=None,
        )
    with pytest.raises(ValueError, match="driver_state=None|structural"):
        _step(
            0,
            source_kind="instrument",
            relation_kind="TRADED_ON",
            target_kind="venue",
            driver_state=_regime_driver(),
        )
    observes = _step(0, source_kind="world_observation", relation_kind="OBSERVES", target_kind="country")
    about = _step(0, source_kind="knowledge_artifact", relation_kind="ABOUT", target_kind="country")
    assert observes.driver_state is not None
    assert about.driver_state is not None
    derived = _step(
        0,
        source_kind="knowledge_artifact",
        relation_kind="DERIVED_FROM",
        target_kind="macro_source_fact_version",
    )
    assert derived.driver_state is None


def test_old_overlay_mappings_without_driver_state_are_rejected() -> None:
    observes = _step(0, source_kind="world_observation", relation_kind="OBSERVES", target_kind="country")
    payload = observes.to_dict()
    payload.pop("driver_state")
    with pytest.raises(ValueError, match="driver_state"):
        PatternStep.from_mapping(payload)
    with pytest.raises(ValueError, match="driver_state"):
        PatternMatchedHop.from_mapping({**payload, "evidence_refs": ()})
    with pytest.raises(ValueError, match="OBSERVES/ABOUT|DriverState"):
        PatternStep.from_mapping({**observes.to_dict(), "driver_state": None})
    structural = _default_steps()[0].to_dict()
    structural.pop("driver_state")
    replayed = PatternStep.from_mapping(structural)
    assert replayed.driver_state is None
    assert replayed.identity_tuple()[-1] is None


def test_driver_state_participates_in_hop_and_hypothesis_identity_not_evidence() -> None:
    rising = _regime_driver()
    falling = DriverState(
        source_family="macro_observation",
        signal_class="regime_bundle",
        regimes=DriverRegimeBundle(
            macro_regime="unknown",
            rates_regime="falling",
            usd_regime="unknown",
            coverage_status="partial",
        ),
        artifact_kind="macro_world_observation",
        until_bound="bounded",
        producer_version=MACRO_PRODUCER_VERSION,
        transform_version=MACRO_TRANSFORM_VERSION,
        missingness="none",
    )
    structural = (
        _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
        _step(1, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country"),
    )
    rising_step = _step(
        2,
        source_kind="country",
        relation_kind="OBSERVES",
        target_kind="world_observation",
        direction="reverse",
        driver_state=rising,
    )
    falling_step = _step(
        2,
        source_kind="country",
        relation_kind="OBSERVES",
        target_kind="world_observation",
        direction="reverse",
        driver_state=falling,
    )
    first = _spec(steps=(*structural, rising_step))
    second = _spec(steps=(*structural, falling_step))
    assert first.hypothesis_id != second.hypothesis_id
    assert first.to_dict()["schema_version"] == PATTERN_HYPOTHESIS_SCHEMA
    left = _hop(rising_step, evidence_refs=("world_observation:v1:" + "e" * 64,))
    right = _hop(rising_step, evidence_refs=("world_observation:v1:" + "f" * 64,))
    assert left.identity_tuple() == rising_step.identity_tuple()
    assert left.identity_tuple() == right.identity_tuple()
    assert left.identity_tuple() != falling_step.identity_tuple()
    assert left.evidence_refs != right.evidence_refs
    occurrence = _occurrence(
        _evaluating(spec=first),
        exact_path=(*(_hop(step) for step in structural), left),
    )
    assert occurrence.to_dict()["schema_version"] == PATTERN_OCCURRENCE_SCHEMA
    other = _occurrence(
        _evaluating(spec=first),
        exact_path=(*(_hop(step) for step in structural), right),
    )
    assert other.occurrence_id != occurrence.occurrence_id
    assert other.exact_path[-1].identity_tuple() == occurrence.exact_path[-1].identity_tuple()


def test_prospective_occurrence_keeps_4h_1d_3d_pending_until_linked() -> None:
    assert PATTERN_EVALUATION_HORIZON_IDS == ("elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1")
    occurrence = _occurrence(expected_horizon_ids=PATTERN_EVALUATION_HORIZON_IDS)
    assert occurrence.spec.expected_horizon_ids == PATTERN_EVALUATION_HORIZON_IDS
    assert occurrence.spec.forecast.horizon_id == "elapsed_1d.v1"
    for horizon_id in PATTERN_EVALUATION_HORIZON_IDS:
        assert occurrence.active_outcome_link(horizon_id) is None
    linked = occurrence.link_outcome(_outcome())
    assert linked.active_outcome_link("elapsed_1d.v1") is not None
    assert linked.active_outcome_link("elapsed_4h.v1") is None
    assert linked.active_outcome_link("elapsed_3d.v1") is None


DISCOVERY_COHORT = f"world_cohort:v1:{canonical_sha256({'cohort': 'discovery'})}"
DISCOVERY_STARTED = f"world_cohort_event:v1:{canonical_sha256({'started': 'discovery'})}"
DISCOVERY_MANIFEST = canonical_sha256({"manifest": "pattern-discovery"})
DISCOVERY_HYP_A = f"pattern_hypothesis:v1:{'a' * 64}"
DISCOVERY_HYP_B = f"pattern_hypothesis:v1:{'b' * 64}"


def _discovery_completed(**overrides: object) -> PatternDiscoveryCompleted:
    values: dict[str, object] = {
        "evaluation_cohort_id": DISCOVERY_COHORT,
        "manifest_sha256": DISCOVERY_MANIFEST,
        "formation_dataset_fingerprint": FORMATION_FP,
        "evaluation_dataset_fingerprint": EVAL_FP,
        "started_event_id": DISCOVERY_STARTED,
        "formation_cutoff": FORMATION,
        "evaluation_start_not_before": EVAL_NOT_BEFORE,
        "selected_hypothesis_ids": (),
        "selected_count": 0,
    }
    values.update(overrides)
    return PatternDiscoveryCompleted(**values)  # type: ignore[arg-type]


def test_pattern_discovery_completed_zero_selection_roundtrip_and_determinism() -> None:
    event = _discovery_completed()
    assert event.schema_version == PATTERN_DISCOVERY_COMPLETED_SCHEMA
    assert event.event_type == PATTERN_DISCOVERY_COMPLETED_EVENT_TYPE
    assert event.selected_hypothesis_ids == ()
    assert event.selected_count == 0
    assert event.shadow_only is True
    assert event.decision_effect == "none"
    assert event.learning_authority == "shadow_only"
    assert event.event_id.startswith("pattern_discovery_completed:v1:")
    assert event.evaluation_cohort_id == DISCOVERY_COHORT
    assert event.started_event_id == DISCOVERY_STARTED
    replayed = PatternDiscoveryCompleted.from_mapping(event.to_dict())
    assert replayed == event
    assert replayed.event_id == event.event_id
    assert PatternDiscoveryCompleted.from_mapping(json.loads(json.dumps(event.to_dict()))) == event
    same = _discovery_completed()
    assert same.event_id == event.event_id
    selected = _discovery_completed(selected_hypothesis_ids=(DISCOVERY_HYP_A, DISCOVERY_HYP_B), selected_count=2)
    assert selected.selected_count == 2
    assert selected.selected_hypothesis_ids == (DISCOVERY_HYP_A, DISCOVERY_HYP_B)
    assert selected.event_id != event.event_id
    assert selected.event_id.startswith("pattern_discovery_completed:v1:")
    assert PatternDiscoveryCompleted.from_mapping(selected.to_dict()) == selected


def test_pattern_discovery_completed_is_frozen() -> None:
    event = _discovery_completed()
    with pytest.raises(FrozenInstanceError):
        event.selected_count = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        event.shadow_only = False  # type: ignore[misc]
    with pytest.raises((TypeError, AttributeError)):
        event.selected_hypothesis_ids.append(DISCOVERY_HYP_A)  # type: ignore[attr-defined]


def test_pattern_discovery_completed_rejects_invalid_order_hash_id_duplicates_count_and_claims() -> None:
    with pytest.raises(ValueError, match="strictly later|formation_cutoff"):
        _discovery_completed(evaluation_start_not_before=FORMATION)
    with pytest.raises(ValueError, match="strictly later|formation_cutoff"):
        _discovery_completed(formation_cutoff=EVAL_NOT_BEFORE, evaluation_start_not_before=FORMATION)
    with pytest.raises(ValueError, match="sha256"):
        _discovery_completed(manifest_sha256=DISCOVERY_MANIFEST.upper())
    with pytest.raises(ValueError, match="sha256"):
        _discovery_completed(formation_dataset_fingerprint="F" * 64)
    with pytest.raises(ValueError, match="sha256"):
        _discovery_completed(evaluation_dataset_fingerprint="0" * 63)
    with pytest.raises(ValueError, match="evaluation_cohort_id"):
        _discovery_completed(evaluation_cohort_id="world_cohort:graph_pilot")
    with pytest.raises(ValueError, match="started_event_id"):
        _discovery_completed(started_event_id=f"pattern_hypothesis_event:v1:{'c' * 64}")
    with pytest.raises(ValueError, match="hypothesis_id"):
        _discovery_completed(selected_hypothesis_ids=("not-a-hypothesis-id",), selected_count=1)
    with pytest.raises(ValueError, match="duplicates"):
        _discovery_completed(selected_hypothesis_ids=(DISCOVERY_HYP_A, DISCOVERY_HYP_A), selected_count=2)
    with pytest.raises(ValueError, match="selected_count"):
        _discovery_completed(selected_hypothesis_ids=(DISCOVERY_HYP_A,), selected_count=0)
    with pytest.raises(ValueError, match="selected_count"):
        _discovery_completed(selected_count=1)
    with pytest.raises(ValueError, match="shadow_only"):
        _discovery_completed(shadow_only=False)
    with pytest.raises(ValueError, match="decision_effect"):
        _discovery_completed(decision_effect="trade")
    with pytest.raises(ValueError, match="learning_authority"):
        _discovery_completed(learning_authority="live")
    valid = _discovery_completed()
    with pytest.raises(ValueError, match="event_id"):
        PatternDiscoveryCompleted.from_mapping(
            {**valid.to_dict(), "event_id": f"pattern_discovery_completed:v1:{'0' * 64}"}
        )


def test_family_hops_require_normalized_family_ref() -> None:
    step = _step(
        0,
        source_kind="instrument",
        relation_kind="MEMBER_OF_FAMILY",
        target_kind="family",
        family_ref="V1:Software",
    )
    assert step.family_ref == "v1:software"
    assert step.identity_tuple()[-1] == "v1:software"
    replayed = PatternStep.from_mapping(step.to_dict())
    assert replayed == step
    assert replayed.to_dict()["family_ref"] == "v1:software"
    with pytest.raises(ValueError, match="require family_ref"):
        _step(0, source_kind="instrument", relation_kind="MEMBER_OF_FAMILY", target_kind="family")
    with pytest.raises(ValueError, match="bare taxonomy id"):
        _step(
            0,
            source_kind="instrument",
            relation_kind="MEMBER_OF_FAMILY",
            target_kind="family",
            family_ref="not a family!",
        )


def test_family_ref_is_forbidden_outside_family_hops() -> None:
    with pytest.raises(ValueError, match="only MEMBER_OF_FAMILY"):
        _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue", family_ref="v1:tech")
    legacy = _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue")
    assert legacy.family_ref is None
    assert PatternStep.from_mapping({k: v for k, v in legacy.to_dict().items() if k != "family_ref"}) == legacy


def test_distinct_families_are_distinct_identities() -> None:
    tech = _step(
        0,
        source_kind="instrument",
        relation_kind="MEMBER_OF_FAMILY",
        target_kind="family",
        family_ref="v1:software",
    )
    energy = _step(
        0,
        source_kind="instrument",
        relation_kind="MEMBER_OF_FAMILY",
        target_kind="family",
        family_ref="v1:oil_gas",
    )
    assert tech.identity_tuple() != energy.identity_tuple()
