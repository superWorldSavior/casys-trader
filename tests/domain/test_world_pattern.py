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
    MARKET_FEATURE_CONTRACT_VERSION,
    WorldOutcome,
    WorldPrediction,
    canonical_sha256,
)
from trader.domain.world_feature_contract import WorldFeatureContract, WorldFeatureGroup, WorldFeatureMask
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    KNOWLEDGE_RELATION_KINDS,
    STRUCTURAL_RELATION_KINDS,
    PatternHypothesisRef,
    WorldEntityRef,
)
from trader.domain.world_pattern import (
    PATTERN_HYPOTHESIS_EVENT_TYPES,
    PATTERN_HYPOTHESIS_SCHEMA,
    PATTERN_HYPOTHESIS_STATUSES,
    PATTERN_OCCURRENCE_EVENT_TYPES,
    PATTERN_OCCURRENCE_SCHEMA,
    PATTERN_OUTCOME_LINK_SCHEMA,
    PatternEvaluationClosed,
    PatternEvaluationStarted,
    PatternForecast,
    PatternHypothesis,
    PatternHypothesisEventId,
    PatternHypothesisId,
    PatternHypothesisInvalidated,
    PatternHypothesisRegistered,
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
SOURCE_SHA = "c" * 64
MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_pattern.py"


def _contract() -> WorldFeatureContract:
    return WorldFeatureContract(
        contract_id="market_ohlcv_graph.v3",
        accepted_episode_contract=MARKET_FEATURE_CONTRACT_VERSION,
        projection_version="graph_projection.v3",
        encoder_identity="world_feature_encoder.v3",
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


def _step(ordinal: int, *, subject_kind: str, predicate: str, object_kind: str) -> PatternStep:
    return PatternStep(
        ordinal=ordinal,
        subject_kind=subject_kind,
        predicate=predicate,
        object_kind=object_kind,
        lag_window="0h..24h",
        evidence_rule_version="macro_path_rule.v1" if ordinal == 0 else "market_ontology.v1",
    )


def _default_steps() -> tuple[PatternStep, PatternStep]:
    return (
        _step(0, subject_kind="macro_indicator", predicate="state_changed", object_kind="country"),
        _step(1, subject_kind="country", predicate="contains_venue", object_kind="venue"),
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
        "model_identity": "online_gru_world_challenger@graph.v3",
        "ontology_revision": "market_ontology.v1",
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
    return _hypothesis(**overrides).start_evaluation(
        started_at=started_at,  # type: ignore[arg-type]
        evaluation_dataset_fingerprint=evaluation_dataset_fingerprint,  # type: ignore[arg-type]
    )


def _instrument() -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")


def _path() -> tuple[PatternMatchedHop, PatternMatchedHop]:
    return (
        PatternMatchedHop(
            ordinal=0,
            subject_kind="macro_indicator",
            predicate="state_changed",
            object_kind="country",
            direction="forward",
            evidence_refs=("macro_source_fact_version:v1:" + "d" * 64,),
        ),
        PatternMatchedHop(
            ordinal=1,
            subject_kind="country",
            predicate="contains_venue",
            object_kind="venue",
            direction="reverse",
            evidence_refs=("world_observation:v1:" + "e" * 64,),
        ),
    )


def _prediction(**overrides: object) -> WorldPrediction:
    values: dict[str, object] = {
        "episode_id": EPISODE_ID,
        "horizon_id": "elapsed_1d.v1",
        "model_id": "online_gru_world_challenger@graph.v3",
        "model_version": "graph.v3",
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
        "cohort_id": "world_cohort:graph_v3_pilot",
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
            _step(0, subject_kind="country", predicate="contains_venue", object_kind="venue"),
            _step(1, subject_kind="macro_indicator", predicate="state_changed", object_kind="country"),
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
    payload["steps"] = [step.to_dict() for step in _spec(steps=_default_steps()[:1] + (
        _step(1, subject_kind="venue", predicate="contains_venue", object_kind="instrument"),
    )).steps]
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
    assert PATTERN_HYPOTHESIS_STATUSES == frozenset(
        {"registered", "evaluating", "evaluation_closed", "invalidated"}
    )
    evaluating = registered.start_evaluation(started_at=EVAL_STARTED, evaluation_dataset_fingerprint=EVAL_FP)
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
        closed.start_evaluation(started_at=CLOSED_AT, evaluation_dataset_fingerprint=EVAL_FP)
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
        registered.start_evaluation(started_at=EVAL_STARTED, evaluation_dataset_fingerprint=FORMATION_FP)
    with pytest.raises(ValueError, match="evaluation_start_not_before|before"):
        registered.start_evaluation(started_at=FORMATION, evaluation_dataset_fingerprint=EVAL_FP)
    started = registered.start_evaluation(started_at=EVAL_STARTED, evaluation_dataset_fingerprint=EVAL_FP)
    assert started.start_evaluation(started_at=EVAL_STARTED, evaluation_dataset_fingerprint=EVAL_FP) == started
    with pytest.raises(ValueError, match="conflict|dataset"):
        started.start_evaluation(
            started_at=EVAL_STARTED,
            evaluation_dataset_fingerprint=canonical_sha256({"dataset": "other"}),
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
            cohort_id="world_cohort:graph_v3_pilot",
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
    factual = STRUCTURAL_RELATION_KINDS | KNOWLEDGE_RELATION_KINDS | FORBIDDEN_RELATION_KINDS
    factual -= {"HYPOTHESIZED_INFLUENCE", "HYPOTHESIZEDINFLUENCE"}
    for kind in tuple(factual):
        with pytest.raises(ValueError, match="predicate|factual|forbidden|CAUSES"):
            _step(0, subject_kind="macro_indicator", predicate=kind, object_kind="country")
    hypothesized = _step(
        0,
        subject_kind="macro_indicator",
        predicate="HypothesizedInfluence",
        object_kind="country",
    )
    assert hypothesized.predicate == "hypothesized_influence"
    forecast = PatternForecast.from_world_prediction(
        _prediction(),
        model_identity="online_gru_world_challenger@graph.v3",
    )
    assert forecast.prediction_id == _prediction().prediction_id
    assert forecast.episode_id == EPISODE_ID
    with pytest.raises(ValueError, match="lineage|model"):
        _occurrence(forecast=_prediction(model_id="other-model"))
