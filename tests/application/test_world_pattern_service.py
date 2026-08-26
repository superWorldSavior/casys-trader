from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from typing import Any, get_origin, get_type_hints

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_availability import (
    AvailabilityEvidence,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
    _iso,
    _receipt_hash_payload,
    _receipt_id_for,
    _receipt_identity_payload,
)
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    WorldOutcome,
    WorldPrediction,
    canonical_sha256,
)
from trader.domain.world_feature_contract import WorldFeatureContract, WorldFeatureGroup, WorldFeatureMask
from trader.domain.world_graph import WorldEntityRef
from trader.domain.world_pattern import (
    PATTERN_ASSOCIATION_METRIC,
    PatternEvaluationClosed,
    PatternEvaluationStarted,
    PatternFormationStats,
    PatternHypothesis,
    PatternHypothesisEvent,
    PatternHypothesisId,
    PatternHypothesisInvalidated,
    PatternHypothesisRegistered,
    PatternHypothesisSpec,
    PatternMatchedHop,
    PatternOccurrence,
    PatternOccurrenceEvent,
    PatternOccurrenceId,
    PatternOccurrenceInvalidated,
    PatternOccurrenceRecorded,
    PatternOutcomeLinked,
    PatternOutcomeLinkSuperseded,
    PatternStep,
    PatternTarget,
)


UTC = timezone.utc
FORMATION = datetime(2026, 9, 1, tzinfo=UTC)
EVAL_NOT_BEFORE = datetime(2026, 9, 2, tzinfo=UTC)
EVAL_STARTED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 2, 13, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 9, 4, tzinfo=UTC)
OUTCOME_AT = datetime(2026, 9, 3, tzinfo=UTC)
READY = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 9, 1, 0, 6, tzinfo=UTC)
BOOT = datetime(2026, 9, 5, tzinfo=UTC)
EPISODE_ID = f"world-episode:v1:{'b' * 64}"
FORMATION_FP = canonical_sha256({"dataset": "formation-pilot"})
EVAL_FP = canonical_sha256({"dataset": "prospective-confirm"})
EVAL_COHORT = "world_cohort:graph_pilot"
SOURCE_SHA = "c" * 64
STORE_ID = "world-pattern-jsonl.v1"
_PORTS_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_ports.py"
_SERVICE_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_service.py"


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


def _step(
    ordinal: int,
    *,
    source_kind: str,
    relation_kind: str,
    target_kind: str,
    direction: str = "forward",
    freshness_bucket: str = "0-4h",
) -> PatternStep:
    return PatternStep(
        ordinal=ordinal,
        source_kind=source_kind,
        relation_kind=relation_kind,
        direction=direction,
        target_kind=target_kind,
        freshness_bucket=freshness_bucket,
        evidence_rule_version="macro_path_rule.v1" if ordinal == 0 else "market_ontology.v1",
    )


def _default_steps() -> tuple[PatternStep, PatternStep]:
    return (
        _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
        _step(1, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country", freshness_bucket="4-24h"),
    )


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


def _instrument() -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")


def _path() -> tuple[PatternMatchedHop, PatternMatchedHop]:
    hops = []
    for step, refs in zip(
        _default_steps(),
        (("macro_source_fact_version:v1:" + "d" * 64,), ("world_observation:v1:" + "e" * 64,)),
        strict=True,
    ):
        hops.append(
            PatternMatchedHop(
                ordinal=step.ordinal,
                source_kind=step.source_kind,
                relation_kind=step.relation_kind,
                direction=step.direction,
                target_kind=step.target_kind,
                freshness_bucket=step.freshness_bucket,
                evidence_rule_version=step.evidence_rule_version,
                evidence_refs=refs,
            )
        )
    return (hops[0], hops[1])


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


def _outcome(**overrides: object) -> WorldOutcome:
    values: dict[str, object] = {
        "episode_id": EPISODE_ID,
        "horizon": {"horizon_id": "elapsed_1d.v1", "duration_seconds": 24 * 60 * 60},
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


def _subject_kind(event: object) -> str:
    if isinstance(event, PatternHypothesisEvent):
        return "pattern_hypothesis_event"
    return "pattern_occurrence_event"


def _scope_for(event: object) -> str:
    if isinstance(event, PatternHypothesisEvent):
        return f"world_pattern:{event.hypothesis_id}"
    return f"world_pattern:{event.occurrence_id}"


def _attested_receipt(*, subject: WorldAvailabilitySubjectRef, ready: datetime, scope: str) -> WorldAvailabilityReceipt:
    locator = WorldStorageLocator(kind="jsonl", store_id=STORE_ID, path="pattern/events.jsonl")
    identity = _receipt_identity_payload(
        schema_version="world_availability_receipt.v1",
        subject=subject,
        scope=scope,
        storage_locator=locator,
    )
    receipt_id = _receipt_id_for(identity)
    digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=ready))
    receipt = WorldAvailabilityReceipt.from_mapping(
        {
            **identity,
            "receipt_id": receipt_id,
            "ready_at": _iso(ready),
            "receipt_sha256": digest,
        }
    )
    return _attest_verified_store_receipt(
        receipt,
        expected_subject=receipt.subject,
        expected_scope=receipt.scope,
        expected_locator=receipt.storage_locator,
    )


def _evidence_for(event: Any, *, ready: datetime, first_seen: datetime) -> AvailabilityEvidence:
    subject = WorldAvailabilitySubjectRef(
        kind=_subject_kind(event),
        subject_id=event.event_id,
        content_sha256=canonical_sha256(event.to_dict()),
    )
    receipt = _attested_receipt(subject=subject, ready=ready, scope=_scope_for(event))
    return AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)


class _MemoryPatternStore:
    """In-memory consumer of the three pattern ports. Store-assigns first_seen."""

    def __init__(
        self,
        *,
        now: datetime = FIRST_SEEN,
        crash_before_receipt: int = 0,
        crash_event_types: frozenset[str] | None = None,
    ) -> None:
        self.now = now
        self.crash_before_receipt = crash_before_receipt
        self.crash_event_types = crash_event_types
        self.hypothesis_events: dict[str, list[PatternHypothesisEvent]] = {}
        self.occurrence_events: dict[str, list[PatternOccurrenceEvent]] = {}
        self.envelopes: dict[str, Any] = {}
        self.ready_at: dict[str, datetime] = {}
        self.first_seen_at: dict[str, datetime] = {}

    def append_event(self, event: PatternHypothesisEvent | PatternOccurrenceEvent) -> Any:
        from trader.application.world_model.pattern_ports import (
            OccurrenceEventEnvelope,
            PatternHypothesisEventEnvelope,
            PatternPayloadConflict,
        )

        if isinstance(event, PatternHypothesisEvent):
            stored = self.hypothesis_events.setdefault(event.hypothesis_id, [])
            envelope_cls = PatternHypothesisEventEnvelope
        elif isinstance(event, PatternOccurrenceEvent):
            stored = self.occurrence_events.setdefault(event.occurrence_id, [])
            envelope_cls = OccurrenceEventEnvelope
        else:
            raise TypeError(f"unsupported pattern event: {type(event).__name__}")
        existing = next((item for item in stored if item.event_id == event.event_id), None)
        if existing is not None:
            if canonical_sha256(existing.to_dict()) != canonical_sha256(event.to_dict()):
                raise PatternPayloadConflict("conflict: same event id with different payload")
            return self._seal(event, envelope_cls)
        stored.append(event)
        return self._seal(event, envelope_cls)

    def load(self, identity: PatternHypothesisId | PatternOccurrenceId) -> PatternHypothesis | PatternOccurrence:
        if isinstance(identity, PatternHypothesisId):
            events = self.hypothesis_events.get(identity.value)
            if not events:
                raise LookupError(identity.value)
            return PatternHypothesis.from_events(events)
        if isinstance(identity, PatternOccurrenceId):
            events = self.occurrence_events.get(identity.value)
            if not events:
                raise LookupError(identity.value)
            return PatternOccurrence.from_events(events)
        raise TypeError("load requires PatternHypothesisId or PatternOccurrenceId")

    def evidence_for(self, event: PatternHypothesisEvent | PatternOccurrenceEvent) -> AvailabilityEvidence | None:
        envelope = self.envelopes.get(event.event_id)
        if envelope is None:
            return None
        return envelope.evidence

    def _seal(self, event: Any, envelope_cls: type) -> Any:
        current = self.envelopes.get(event.event_id)
        if current is None:
            current = envelope_cls(event=event, evidence=None)
            self.envelopes[event.event_id] = current
        if current.evidence is not None:
            return current
        crash_applies = self.crash_event_types is None or event.event_type in self.crash_event_types
        if crash_applies and self.crash_before_receipt > 0:
            self.crash_before_receipt -= 1
            return current
        self.ready_at.setdefault(event.event_id, self.now)
        self.first_seen_at.setdefault(event.event_id, self.now)
        sealed = envelope_cls(
            event=event,
            evidence=_evidence_for(
                event,
                ready=self.ready_at[event.event_id],
                first_seen=self.first_seen_at[event.event_id],
            ),
        )
        self.envelopes[event.event_id] = sealed
        return sealed


def _service(
    *, crash_before_receipt: int = 0, crash_event_types: frozenset[str] | None = None, now: datetime = FIRST_SEEN
):
    from trader.application.world_model.pattern_service import WorldPatternService

    store = _MemoryPatternStore(
        now=now,
        crash_before_receipt=crash_before_receipt,
        crash_event_types=crash_event_types,
    )
    return WorldPatternService(hypotheses=store, occurrences=store, availability=store), store


def _register(service, spec: PatternHypothesisSpec | None = None, *, registered_at: datetime = FORMATION):
    from trader.application.world_model.pattern_service import RegisterPatternHypothesis

    return service.register(RegisterPatternHypothesis(spec=spec or _spec(), registered_at=registered_at))


def _start(
    service,
    hypothesis_id: str,
    *,
    started_at: datetime = EVAL_STARTED,
    fingerprint: str = EVAL_FP,
    evaluation_cohort_id: str = EVAL_COHORT,
):
    from trader.application.world_model.pattern_service import StartPatternEvaluation

    return service.start(
        StartPatternEvaluation(
            hypothesis_id=hypothesis_id,
            evaluation_cohort_id=evaluation_cohort_id,
            started_at=started_at,
            evaluation_dataset_fingerprint=fingerprint,
        )
    )


def _record(service, hypothesis_id: str, **overrides: object):
    from trader.application.world_model.pattern_service import RecordPatternOccurrence

    values: dict[str, object] = {
        "hypothesis_id": hypothesis_id,
        "cohort_id": "world_cohort:graph_pilot",
        "instrument": _instrument(),
        "cutoff_at": CUTOFF,
        "exact_path": _path(),
        "forecast": _prediction(),
        "artifact_refs": ("knowledge_artifact:v1:" + "a" * 64,),
        "fact_refs": ("macro_source_fact_version:v1:" + "d" * 64,),
    }
    values.update(overrides)
    return service.record(RecordPatternOccurrence(**values))  # type: ignore[arg-type]


def _link(service, occurrence_id: str, outcome: WorldOutcome | object | None = None):
    from trader.application.world_model.pattern_service import LinkPatternOutcome

    return service.link(
        LinkPatternOutcome(occurrence_id=occurrence_id, outcome=outcome if outcome is not None else _outcome())
    )


def test_ports_are_consumer_owned_typed_contracts() -> None:
    from trader.application.world_model.pattern_ports import (
        OccurrenceLedger,
        PatternAvailability,
        PatternHypothesisEventEnvelope,
        PatternHypothesisLedger,
        PatternPayloadConflict,
    )
    from trader.domain.world_availability import AvailabilityEvidence

    assert issubclass(PatternPayloadConflict, ValueError)
    append_hints = get_type_hints(PatternHypothesisLedger.append_event)
    assert list(inspect.signature(PatternHypothesisLedger.append_event).parameters) == ["self", "event"]
    assert append_hints["event"] == PatternHypothesisEvent
    assert append_hints["return"] is PatternHypothesisEventEnvelope

    load_hints = get_type_hints(PatternHypothesisLedger.load)
    assert list(inspect.signature(PatternHypothesisLedger.load).parameters) == ["self", "hypothesis_id"]
    assert load_hints["hypothesis_id"] is PatternHypothesisId
    assert load_hints["return"] is PatternHypothesis

    occ_append = get_type_hints(OccurrenceLedger.append_event)
    assert list(inspect.signature(OccurrenceLedger.append_event).parameters) == ["self", "event"]
    assert occ_append["event"] == PatternOccurrenceEvent

    occ_load = get_type_hints(OccurrenceLedger.load)
    assert list(inspect.signature(OccurrenceLedger.load).parameters) == ["self", "occurrence_id"]
    assert occ_load["occurrence_id"] is PatternOccurrenceId
    assert occ_load["return"] is PatternOccurrence

    evidence_hints = get_type_hints(PatternAvailability.evidence_for)
    assert list(inspect.signature(PatternAvailability.evidence_for).parameters) == ["self", "event"]
    assert "ready_at" not in inspect.signature(PatternAvailability.evidence_for).parameters
    assert "first_seen_at" not in inspect.signature(PatternAvailability.evidence_for).parameters
    assert "clock" not in inspect.signature(PatternAvailability.evidence_for).parameters
    assert evidence_hints["return"] == AvailabilityEvidence | None

    hypothesis_doc = PatternHypothesisLedger.append_event.__doc__ or ""
    availability_doc = PatternAvailability.evidence_for.__doc__ or ""
    ports_source = _PORTS_PATH.read_text(encoding="utf-8")
    assert "first_seen" in hypothesis_doc or "first_seen" in ports_source
    assert "restart" in hypothesis_doc.lower() or "conservative" in ports_source.lower()
    assert "clock" not in availability_doc.lower() or "not" in availability_doc.lower()


def test_ports_and_service_reject_sqlite_clock_and_layer_leaks() -> None:
    from trader.application.world_model import pattern_ports, pattern_service
    from trader.application.world_model.pattern_ports import (
        OccurrenceLedger,
        PatternAvailability,
        PatternHypothesisLedger,
    )
    from trader.application.world_model.pattern_service import WorldPatternService

    forbidden = ("trader.runtime", "trader.infrastructure", "trader.reporting", "sqlite3")
    violations: list[str] = []
    for path in (_PORTS_PATH, _SERVICE_PATH):
        assert path.exists()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in forbidden):
                    violations.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in forbidden):
                        violations.append(f"{path.name}: import {alias.name}")
        text = path.read_text(encoding="utf-8")
        assert "sqlite" not in text.lower()
        assert "world_pattern_store" not in text
        assert "datetime.now" not in text
        assert "CAUSES" not in text
        assert "set_status" not in text
    assert violations == []

    for module in (pattern_ports, pattern_service):
        source = inspect.getsource(module)
        assert "trader.infrastructure" not in source
        assert "trader.runtime" not in source
        assert "trader.reporting" not in source
        assert "trader.application.world_model.labeler" not in source

    for cls in (PatternHypothesisLedger, OccurrenceLedger, PatternAvailability):
        for method in cls.__dict__.values():
            if not callable(method) or method.__name__.startswith("_"):
                continue
            signature = inspect.signature(method)
            assert "ready_at" not in signature.parameters
            assert "clock" not in signature.parameters
            assert "first_seen_at" not in signature.parameters
            for parameter in signature.parameters.values():
                assert parameter.annotation is not dict
                assert get_origin(parameter.annotation) is not dict

    assert "ready_at" not in inspect.signature(WorldPatternService).parameters
    assert "clock" not in inspect.signature(WorldPatternService).parameters
    assert not hasattr(WorldPatternService, "set_status")


def test_typed_handlers_are_the_only_mutation_surface() -> None:
    from trader.application.world_model.pattern_service import (
        ClosePatternEvaluation,
        InvalidatePatternHypothesis,
        LinkPatternOutcome,
        RecordPatternOccurrence,
        RegisterPatternHypothesis,
        StartPatternEvaluation,
        WORLD_PATTERN_COMMANDS,
        WorldPatternService,
    )

    expected = {
        "register": RegisterPatternHypothesis,
        "start": StartPatternEvaluation,
        "record": RecordPatternOccurrence,
        "link": LinkPatternOutcome,
        "close": ClosePatternEvaluation,
        "invalidate": InvalidatePatternHypothesis,
    }
    for method_name, command_type in expected.items():
        method = getattr(WorldPatternService, method_name)
        hints = get_type_hints(method)
        assert command_type in hints.values()
    assert set(expected.values()) <= set(WORLD_PATTERN_COMMANDS)
    service, _store = _service()
    with pytest.raises(TypeError):
        service.handle({"event_type": "pattern_hypothesis_registered"})  # type: ignore[arg-type]


def test_register_is_idempotent_and_conflicting_definition_is_typed_conflict() -> None:
    from trader.application.world_model.pattern_ports import PatternPayloadConflict
    from trader.application.world_model.pattern_service import (
        PATTERN_AUTHORITY,
        PATTERN_DECISION_EFFECT,
        RegisterPatternHypothesis,
    )

    spec = _spec()
    service, store = _service(crash_before_receipt=1)
    first = service.register(RegisterPatternHypothesis(spec=spec, registered_at=FORMATION))
    assert isinstance(first.event, PatternHypothesisRegistered)
    assert first.availability_status == "availability_unproven"
    assert first.event.spec.causal_claim is False
    assert PATTERN_AUTHORITY == "shadow_only"
    assert PATTERN_DECISION_EFFECT == "none"
    retry = service.register(RegisterPatternHypothesis(spec=spec, registered_at=FORMATION))
    assert retry.event.event_id == first.event.event_id
    assert retry.availability_status == "eligible"
    again = service.register(RegisterPatternHypothesis(spec=spec, registered_at=FORMATION))
    assert again.event.event_id == first.event.event_id
    loaded = store.load(PatternHypothesisId(first.event.hypothesis_id))
    assert loaded.status == "registered"
    shifted = RegisterPatternHypothesis(spec=spec, registered_at=datetime(2026, 9, 1, 1, tzinfo=UTC))
    with pytest.raises(PatternPayloadConflict, match="conflict"):
        service.register(shifted)


def test_definition_variant_mints_a_new_hypothesis_id() -> None:
    service, store = _service()
    first = _register(service, _spec())
    permuted = _register(
        service,
        _spec(
            steps=(
                _step(0, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country", freshness_bucket="4-24h"),
                _step(1, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
            )
        ),
    )
    horizon = _register(
        service,
        _spec(
            target=PatternTarget(
                entity_kind="instrument",
                horizon_id="elapsed_4h.v1",
                move_distribution={"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
            )
        ),
    )
    model = _register(service, _spec(model_identity="online_markov_world_challenger@graph.v1"))
    ids = {
        first.event.hypothesis_id,
        permuted.event.hypothesis_id,
        horizon.event.hypothesis_id,
        model.event.hypothesis_id,
    }
    assert len(ids) == 4
    assert len(store.hypothesis_events) == 4


def test_start_rejects_in_sample_confirmation_and_is_idempotent() -> None:
    from trader.application.world_model.pattern_ports import PatternPayloadConflict
    from trader.application.world_model.pattern_service import StartPatternEvaluation

    service, store = _service()
    registered = _register(service)
    hypothesis_id = registered.event.hypothesis_id
    with pytest.raises(ValueError, match="in-sample|dataset|confirmation"):
        _start(service, hypothesis_id, fingerprint=FORMATION_FP)
    with pytest.raises(ValueError, match="evaluation_start_not_before|before"):
        _start(service, hypothesis_id, started_at=FORMATION)
    started = _start(service, hypothesis_id)
    assert isinstance(started.event, PatternEvaluationStarted)
    assert started.event.evaluation_dataset_fingerprint == EVAL_FP
    assert started.event.evaluation_dataset_fingerprint != FORMATION_FP
    retry = _start(service, hypothesis_id)
    assert retry.event.event_id == started.event.event_id
    other = canonical_sha256({"dataset": "other-confirm"})
    with pytest.raises((PatternPayloadConflict, ValueError), match="conflict|dataset"):
        service.start(
            StartPatternEvaluation(
                hypothesis_id=hypothesis_id,
                evaluation_cohort_id=EVAL_COHORT,
                started_at=EVAL_STARTED,
                evaluation_dataset_fingerprint=other,
            )
        )
    loaded = store.load(PatternHypothesisId(hypothesis_id))
    assert loaded.status == "evaluating"
    assert loaded.evaluation_dataset_fingerprint == EVAL_FP


def test_record_requires_started_evaluation_and_matching_fingerprints() -> None:
    service, store = _service()
    registered = _register(service)
    with pytest.raises(ValueError, match="before|start|evaluating"):
        _record(service, registered.event.hypothesis_id)
    started = _start(service, registered.event.hypothesis_id)
    recorded = _record(service, started.event.hypothesis_id)
    assert isinstance(recorded.event, PatternOccurrenceRecorded)
    assert recorded.availability_status == "eligible"
    occurrence = store.load(PatternOccurrenceId(recorded.event.occurrence_id))
    assert occurrence.status == "recorded"
    assert occurrence.evaluation_dataset_fingerprint == EVAL_FP
    assert occurrence.evaluation_dataset_fingerprint != occurrence.spec.formation_dataset_fingerprint
    assert occurrence.feature_contract_fingerprint == _spec().feature_contract_fingerprint
    assert "ready_at" not in occurrence.to_dict()
    retry = _record(service, started.event.hypothesis_id)
    assert retry.event.event_id == recorded.event.event_id
    from trader.application.world_model.pattern_service import ClosePatternEvaluation

    service.close(ClosePatternEvaluation(hypothesis_id=started.event.hypothesis_id, closed_at=CLOSED_AT))
    with pytest.raises(ValueError, match="evaluating|closed"):
        _record(
            service,
            started.event.hypothesis_id,
            cutoff_at=datetime(2026, 9, 6, tzinfo=UTC),
            forecast=_prediction(created_at=datetime(2026, 9, 6, tzinfo=UTC)),
        )


def test_link_requires_durable_occurrence_and_canonical_world_outcome_leaf() -> None:
    from trader.application.world_model.pattern_service import LinkPatternOutcome, validate_world_outcome_leaf

    unproven_service, unproven_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"pattern_occurrence_recorded"}),
    )
    registered = _register(unproven_service)
    _start(unproven_service, registered.event.hypothesis_id)
    recorded = _record(unproven_service, registered.event.hypothesis_id)
    assert recorded.availability_status == "availability_unproven"
    assert unproven_store.evidence_for(recorded.event) is None
    with pytest.raises(ValueError, match="availability_unproven|durable|evidence"):
        _link(unproven_service, recorded.event.occurrence_id)
    sealed = _record(unproven_service, registered.event.hypothesis_id)
    assert sealed.availability_status == "eligible"
    assert unproven_store.evidence_for(sealed.event) is not None
    outcome = _outcome()
    linked = _link(unproven_service, sealed.event.occurrence_id, outcome)
    assert isinstance(linked.event, PatternOutcomeLinked)
    leaf = linked.event.link
    assert leaf.world_outcome_event_id == outcome.event_id
    assert leaf.world_outcome_content_sha256 == outcome.payload_hash
    assert leaf.horizon_id == "elapsed_1d.v1"
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
        "ready_at",
    ):
        assert forbidden not in payload
    retry = _link(unproven_service, sealed.event.occurrence_id, outcome)
    assert retry.event.event_id == linked.event.event_id
    validated = validate_world_outcome_leaf(
        outcome, expected_horizon_id="elapsed_1d.v1", expected_episode_id=EPISODE_ID
    )
    assert validated.event_id == outcome.event_id
    with pytest.raises(TypeError, match="WorldOutcome"):
        LinkPatternOutcome(occurrence_id=sealed.event.occurrence_id, outcome=outcome.to_dict())  # type: ignore[arg-type]


def test_invalid_world_outcome_leaf_is_rejected() -> None:
    from trader.application.world_model.pattern_service import LinkPatternOutcome, validate_world_outcome_leaf

    service, _store = _service()
    registered = _register(service)
    _start(service, registered.event.hypothesis_id)
    recorded = _record(service, registered.event.hypothesis_id)
    labeler_dict = {
        "schema_version": "world_outcome.v1",
        "episode_id": EPISODE_ID,
        "horizon_id": "elapsed_1d.v1",
        "status": "observed",
        "move_class": "UP",
        "simple_return": 0.02,
        "target_at": OUTCOME_AT.isoformat(),
    }
    with pytest.raises(TypeError, match="WorldOutcome"):
        LinkPatternOutcome(occurrence_id=recorded.event.occurrence_id, outcome=labeler_dict)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="WorldOutcome"):
        validate_world_outcome_leaf(labeler_dict)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="horizon"):
        _link(
            service,
            recorded.event.occurrence_id,
            _outcome(horizon={"horizon_id": "elapsed_4h.v1", "duration_seconds": 4 * 60 * 60}),
        )
    with pytest.raises(ValueError, match="episode"):
        _link(service, recorded.event.occurrence_id, _outcome(episode_id=f"world-episode:v1:{'f' * 64}"))
    with pytest.raises(ValueError, match="horizon|leaf"):
        validate_world_outcome_leaf(_outcome(), expected_horizon_id="elapsed_4h.v1")


def test_link_correction_requires_explicit_supersession() -> None:
    service, store = _service()
    registered = _register(service)
    _start(service, registered.event.hypothesis_id)
    recorded = _record(service, registered.event.hypothesis_id)
    first = _link(service, recorded.event.occurrence_id, _outcome())
    current = first.event.link
    with pytest.raises(ValueError, match="supersed"):
        _link(
            service,
            recorded.event.occurrence_id,
            _outcome(endpoint_close=99.0, source_raw_sha256="d" * 64),
        )
    corrected = _outcome(
        endpoint_close=99.0,
        source_raw_sha256="d" * 64,
        supersedes_event_id=current.world_outcome_event_id,
    )
    superseded = _link(service, recorded.event.occurrence_id, corrected)
    assert isinstance(superseded.event, PatternOutcomeLinkSuperseded)
    occurrence = store.load(PatternOccurrenceId(recorded.event.occurrence_id))
    leaf = occurrence.active_outcome_link("elapsed_1d.v1")
    assert leaf is not None
    assert leaf.supersedes_link_id == current.link_id
    assert leaf.world_outcome_event_id == corrected.event_id


def test_close_and_invalidate_are_idempotent_and_keep_shadow_authority() -> None:
    from trader.application.world_model.pattern_service import (
        PATTERN_AUTHORITY,
        PATTERN_CAUSAL_CLAIM,
        PATTERN_DECISION_EFFECT,
        ClosePatternEvaluation,
        InvalidatePatternHypothesis,
        InvalidatePatternOccurrence,
    )

    service, store = _service()
    registered = _register(service)
    started = _start(service, registered.event.hypothesis_id)
    recorded = _record(service, started.event.hypothesis_id)
    closed = service.close(ClosePatternEvaluation(hypothesis_id=started.event.hypothesis_id, closed_at=CLOSED_AT))
    assert isinstance(closed.event, PatternEvaluationClosed)
    retry_close = service.close(ClosePatternEvaluation(hypothesis_id=started.event.hypothesis_id, closed_at=CLOSED_AT))
    assert retry_close.event.event_id == closed.event.event_id
    loaded = store.load(PatternHypothesisId(started.event.hypothesis_id))
    assert loaded.status == "evaluation_closed"
    with pytest.raises(ValueError, match="invalidat|closed|terminal"):
        service.invalidate(
            InvalidatePatternHypothesis(
                hypothesis_id=started.event.hypothesis_id,
                invalidated_at=CLOSED_AT,
                reason="too-late",
            )
        )
    assert PATTERN_AUTHORITY == "shadow_only"
    assert PATTERN_DECISION_EFFECT == "none"
    assert PATTERN_CAUSAL_CLAIM is False

    other, other_store = _service()
    other_registered = _register(other)
    invalidated = other.invalidate(
        InvalidatePatternHypothesis(
            hypothesis_id=other_registered.event.hypothesis_id,
            invalidated_at=EVAL_STARTED,
            reason="abandoned",
        )
    )
    assert isinstance(invalidated.event, PatternHypothesisInvalidated)
    retry_inv = other.invalidate(
        InvalidatePatternHypothesis(
            hypothesis_id=other_registered.event.hypothesis_id,
            invalidated_at=EVAL_STARTED,
            reason="abandoned",
        )
    )
    assert retry_inv.event.event_id == invalidated.event.event_id
    assert other_store.load(PatternHypothesisId(other_registered.event.hypothesis_id)).status == "invalidated"

    occ_invalidated = service.invalidate_occurrence(
        InvalidatePatternOccurrence(
            occurrence_id=recorded.event.occurrence_id,
            invalidated_at=CLOSED_AT,
            reason="counterexample",
        )
    )
    assert isinstance(occ_invalidated.event, PatternOccurrenceInvalidated)
    occ_retry = service.invalidate_occurrence(
        InvalidatePatternOccurrence(
            occurrence_id=recorded.event.occurrence_id,
            invalidated_at=CLOSED_AT,
            reason="counterexample",
        )
    )
    assert occ_retry.event.event_id == occ_invalidated.event.event_id
    with pytest.raises(ValueError, match="invalidat"):
        _link(service, recorded.event.occurrence_id)


def test_first_seen_is_ledger_assigned_and_restart_conservative() -> None:
    spec = _spec()
    service, store = _service(now=FIRST_SEEN)
    first = _register(service, spec)
    assert first.evidence is not None
    assert first.evidence.first_seen_at == FIRST_SEEN
    store.now = BOOT
    retry = _register(service, spec)
    assert retry.evidence is not None
    assert retry.evidence.first_seen_at == FIRST_SEEN
    assert retry.evidence.receipt.ready_at == FIRST_SEEN
    assert "clock" not in inspect.signature(type(service)).parameters
    source = _SERVICE_PATH.read_text(encoding="utf-8")
    assert "first_seen_at" not in source or "ledger" in source.lower() or "availability" in source.lower()
    assert "datetime.now" not in source


def test_identical_retry_repairs_receipt_without_moving_first_seen() -> None:
    service, store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"pattern_evaluation_started"}),
        now=FIRST_SEEN,
    )
    registered = _register(service)
    started = _start(service, registered.event.hypothesis_id)
    assert started.availability_status == "availability_unproven"
    store.now = BOOT
    retry = _start(service, registered.event.hypothesis_id)
    assert retry.event.event_id == started.event.event_id
    assert retry.availability_status == "eligible"
    assert retry.evidence is not None
    assert retry.evidence.first_seen_at == BOOT
    store.now = datetime(2026, 9, 6, tzinfo=UTC)
    again = _start(service, registered.event.hypothesis_id)
    assert again.evidence is not None
    assert again.evidence.first_seen_at == BOOT


def test_start_and_record_bind_evaluation_cohort_id() -> None:
    from trader.application.world_model.pattern_service import StartPatternEvaluation

    service, store = _service()
    registered = _register(service)
    with pytest.raises(TypeError):
        service.start(
            StartPatternEvaluation(
                hypothesis_id=registered.event.hypothesis_id,
                started_at=EVAL_STARTED,
                evaluation_dataset_fingerprint=EVAL_FP,
            )
        )
    started = _start(service, registered.event.hypothesis_id, evaluation_cohort_id=EVAL_COHORT)
    assert isinstance(started.event, PatternEvaluationStarted)
    assert started.event.evaluation_cohort_id == EVAL_COHORT
    loaded = store.load(PatternHypothesisId(started.event.hypothesis_id))
    assert loaded.evaluation_cohort_id == EVAL_COHORT
    with pytest.raises(ValueError, match="evaluation_cohort_id|cohort"):
        _record(service, started.event.hypothesis_id, cohort_id="world_cohort:other")
    recorded = _record(service, started.event.hypothesis_id, cohort_id=EVAL_COHORT)
    occurrence = store.load(PatternOccurrenceId(recorded.event.occurrence_id))
    assert occurrence.spec.cohort_id == EVAL_COHORT
