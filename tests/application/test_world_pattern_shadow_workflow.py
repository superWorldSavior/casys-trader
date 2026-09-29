from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.application.test_world_cohort_service import START_READY, START_SEEN, _arm_command, _start_command
from tests.application.test_world_pattern_discovery import _record
from tests.application.test_world_pattern_service import (
    _MemoryPatternStore,
    _link,
    _outcome,
    _prediction,
    _record as _record_occurrence,
    _spec,
)
from tests.application.test_world_pilot_activation import BOOT, LATER_BOOT, _activate, _service as _pilot_service
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.pattern_discovery import (
    PatternDiscoveryCandidate,
    PatternDiscoveryResult,
    PatternDiscoveryService,
)
from trader.application.world_model.pattern_discovery_ports import PatternFormationBatch
from trader.application.world_model.pattern_evaluation_request import (
    PatternEvaluationPersistResult,
    PatternEvaluationRequest,
    PatternEvaluationResult,
)
from trader.application.world_model.pattern_outcome_link import (
    PatternOutcomeLinkRequest,
    PatternOutcomeLinkResult,
    PatternQueryUnavailable,
)
from trader.application.world_model.pattern_service import WorldPatternService
from trader.application.world_model.pattern_shadow_workflow import (
    PatternShadowWorkflow,
    next_canonical_bar_boundary,
    pattern_evaluation_dataset_fingerprint,
)
from trader.domain.world_cohort import (
    CloseWorldCohort,
    CohortPhase,
    RegisterWorldCohort,
    WorldCohort,
    WorldCohortEvent,
    WorldCohortEventEnvelope,
    WorldCohortId,
)
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_feature_contract import graph_content_mask, graph_feature_contract
from trader.domain.world_pattern import (
    EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
    PatternDiscoveryCompleted,
    PatternEvaluationClosed,
    PatternHypothesisId,
    PatternOccurrenceId,
)


UTC = timezone.utc
FORMATION_FP = canonical_sha256({"dataset": "pattern-shadow-formation"})
MODULE_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_shadow_workflow.py"


class _Lifecycle:
    def __init__(self) -> None:
        self.marker: PatternDiscoveryCompleted | None = None
        self.append_calls = 0
        self.append_failure: Exception | None = None

    def get_discovery_completed(
        self,
        evaluation_cohort_id: str,
        started_event_id: str,
    ) -> PatternDiscoveryCompleted | None:
        marker = self.marker
        if marker is None:
            return None
        if marker.evaluation_cohort_id != evaluation_cohort_id or marker.started_event_id != started_event_id:
            return None
        return marker

    def append_discovery_completed(self, event: PatternDiscoveryCompleted) -> bool:
        self.append_calls += 1
        if self.append_failure is not None:
            raise self.append_failure
        if self.marker is None:
            self.marker = event
            return True
        assert self.marker == event
        return False


class _Discovery:
    def __init__(
        self,
        calls: list[str],
        *,
        candidates: int = 1,
        failure: Exception | None = None,
        considered_records: int | None = None,
        eligible_records: int | None = None,
    ) -> None:
        self.calls = calls
        self.candidates = candidates
        self.failure = failure
        self.considered_records = considered_records
        self.eligible_records = eligible_records
        self.requests = []

    def discover(self, request):
        self.calls.append("discover")
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        contract = graph_feature_contract()
        mask = graph_content_mask()
        candidate = PatternDiscoveryCandidate(
            spec=_spec(
                formation_cutoff=request.formation_cutoff,
                evaluation_start_not_before=request.evaluation_start_not_before,
                formation_dataset_fingerprint=FORMATION_FP,
                feature_contract_id=contract.contract_id,
                feature_contract_fingerprint=contract.fingerprint,
                feature_mask_id=mask.mask_id,
                feature_mask_fingerprint=mask.fingerprint,
                model_identity=EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
                ontology_revision=request.ontology_revision or "market_ontology.v1",
            ),
            semantic_signature="instrument:TRADED_ON:forward:venue",
        )
        return PatternDiscoveryResult(
            candidates=() if self.candidates == 0 else (candidate,),
            formation_dataset_fingerprint=FORMATION_FP,
            eligible_records=20 if self.eligible_records is None else self.eligible_records,
            rejection_counts={},
            source_evidence_ids=(),
            considered_records=20 if self.considered_records is None else self.considered_records,
        )


class _Evaluation:
    def __init__(
        self,
        calls: list[str],
        *,
        evaluate_failure: Exception | None = None,
        persist_failure: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.evaluate_failure = evaluate_failure
        self.persist_failure = persist_failure
        self.requests: list[PatternEvaluationRequest] = []

    def evaluate(self, request: PatternEvaluationRequest) -> PatternEvaluationResult:
        self.calls.append("evaluate")
        self.requests.append(request)
        if self.evaluate_failure is not None:
            raise self.evaluate_failure
        return PatternEvaluationResult(
            matches=(),
            hypotheses=(),
            eligible_records=0,
            rejection_counts={},
            source_evidence_ids=(),
            request=request,
            considered_records=0,
        )

    def persist(self, result, *, patterns, predictions) -> PatternEvaluationPersistResult:
        self.calls.append("persist")
        if self.persist_failure is not None:
            raise self.persist_failure
        return PatternEvaluationPersistResult(
            result=result,
            persisted_prediction_ids=("prediction:v1:test",),
            persisted_occurrence_ids=("pattern_occurrence:v1:test",),
        )


class _Outcomes:
    def __init__(self, calls: list[str], *, failure: Exception | None = None) -> None:
        self.calls = calls
        self.failure = failure
        self.requests: list[PatternOutcomeLinkRequest] = []

    def link(self, request: PatternOutcomeLinkRequest, *, patterns) -> PatternOutcomeLinkResult:
        self.calls.append("outcome_link")
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return PatternOutcomeLinkResult(
            statuses=(),
            linked_count=0,
            available_count=0,
            pending_count=0,
            request=request,
        )


class _Predictions:
    def append_prediction(self, _prediction) -> bool:
        return True


@dataclass
class _Query:
    cohorts: tuple[WorldCohort, ...]
    delegate: object | None = None

    def list_collecting_cohorts(self) -> tuple[WorldCohort, ...]:
        return self.cohorts

    def list_slots(self, cohort_id: WorldCohortId):
        assert self.delegate is not None
        return self.delegate.list_slots(cohort_id)

    def load(self, cohort_id: WorldCohortId) -> WorldCohort:
        assert self.delegate is not None
        return self.delegate.load(cohort_id)

    def envelope_for(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope:
        assert self.delegate is not None
        return self.delegate.envelope_for(event)


def _collecting_graph() -> tuple[object, WorldCohort]:
    service, store = _pilot_service()
    report = _activate(cohort_service=service, now=BOOT)
    graph_id = next(item["cohort_id"] for item in report.cohorts if item["key"] == "graph")
    registered = store.load(WorldCohortId(graph_id))
    service.arm(_arm_command(registered.manifest, sensors=("graph", "company", "macro")))
    service.start(_start_command(registered.manifest))
    return store, store.load(WorldCohortId(graph_id))


class _CutoffAwareFormationSource:
    """Admit only records whose source clocks are at or before the frozen cutoff."""

    def __init__(self) -> None:
        self.records: list[object] = []

    def load_formation_batch(self, request) -> PatternFormationBatch:
        cutoff = request.formation_cutoff
        admitted = []
        for record in self.records:
            observation = record.episode.observation
            if observation.as_of_bar_ts > cutoff:
                continue
            if observation.available_at is not None and observation.available_at > cutoff:
                continue
            if record.snapshot.cutoff_at > cutoff:
                continue
            if record.recorded_at > cutoff:
                continue
            if record.available_at is not None and record.available_at > cutoff:
                continue
            admitted.append(record)
        return PatternFormationBatch(
            records=tuple(admitted),
            rejection_counts={},
            source_evidence_ids=tuple(dict.fromkeys(item.episode.episode_id for item in admitted)),
        )


def _workflow(
    *,
    query=None,
    candidates: int = 1,
    discovery_failure: Exception | None = None,
    evaluate_failure: Exception | None = None,
    persist_failure: Exception | None = None,
    outcome_failure: Exception | None = None,
    considered_records: int | None = None,
    eligible_records: int | None = None,
    evaluation: object | None = None,
    discovery: object | None = None,
):
    store, graph = _collecting_graph()
    calls: list[str] = []
    pattern_store = _MemoryPatternStore()
    patterns = WorldPatternService(
        hypotheses=pattern_store,
        occurrences=pattern_store,
        availability=pattern_store,
    )
    lifecycle = _Lifecycle()
    discovery = discovery or _Discovery(
        calls,
        candidates=candidates,
        failure=discovery_failure,
        considered_records=considered_records,
        eligible_records=eligible_records,
    )
    evaluation = evaluation or _Evaluation(
        calls,
        evaluate_failure=evaluate_failure,
        persist_failure=persist_failure,
    )
    outcomes = _Outcomes(calls, failure=outcome_failure)
    workflow = PatternShadowWorkflow(
        cohorts=query or _Query((graph,), store),
        lifecycle=lifecycle,
        discovery=discovery,
        patterns=patterns,
        evaluation=evaluation,
        predictions=_Predictions(),
        outcomes=outcomes,
    )
    return workflow, graph, lifecycle, discovery, evaluation, outcomes, patterns, pattern_store, calls


def _stage(result, name: str):
    return next(item for item in result.stages if item.stage == name)


class _ControlCountQuery:
    """Cohort query stub proving real prediction production (or its absence)."""

    def __init__(self, slots: tuple[object, ...], count: int) -> None:
        self._slots = slots
        self._count = count

    def list_slots(self, _cohort_id):
        return self._slots

    def count_study_predictions(self, _study_cohort_id, *, feature_contract_fingerprint=None):
        assert feature_contract_fingerprint
        return self._count


def test_control_production_skips_before_evaluation_starts() -> None:
    workflow, graph, *_ = _workflow()
    stage = workflow._check_control_production(graph, evaluation_started=False)
    assert stage.stage == "control_production"
    assert stage.status == "completed"
    assert stage.reason == "evaluation_not_started"


def test_control_production_without_query_capability_is_explicit_unchecked() -> None:
    workflow, graph, *_ = _workflow()
    stage = workflow._check_control_production(graph, evaluation_started=True)
    assert stage.stage == "control_production"
    assert stage.status == "completed"
    assert stage.reason == "control_production_unchecked"


def test_control_production_fails_loud_when_admitted_slots_have_no_control_prediction() -> None:
    workflow, graph, *_ = _workflow(query=_ControlCountQuery((object(),), 0))
    stage = workflow._check_control_production(graph, evaluation_started=True)
    assert stage.stage == "control_production"
    assert stage.status == "failed"
    assert (stage.error or {}).get("code") == "control_predictions_missing"


def test_control_production_passes_when_control_predictions_exist() -> None:
    workflow, graph, *_ = _workflow(query=_ControlCountQuery((object(),), 2))
    stage = workflow._check_control_production(graph, evaluation_started=True)
    assert stage.stage == "control_production"
    assert stage.status == "completed"
    assert stage.reason is None


def test_first_run_freezes_window_registers_then_evaluates_and_links_last() -> None:
    workflow, graph, lifecycle, discovery, evaluation, outcomes, _patterns, pattern_store, calls = _workflow()

    result = workflow.run(BOOT)

    assert result.status == "completed"
    assert calls == ["discover", "evaluate", "persist", "outcome_link"]
    assert lifecycle.marker is not None
    assert lifecycle.append_calls == 1
    assert result.selected_hypothesis_ids == lifecycle.marker.selected_hypothesis_ids
    assert result.discovered_count == 1
    assert result.persisted_prediction_count == 1
    assert result.persisted_occurrence_count == 1
    assert result.shadow_only is True
    assert result.decision_effect == "none"
    assert result.learning_authority == "shadow_only"
    hypothesis = pattern_store.load(PatternHypothesisId(result.selected_hypothesis_ids[0]))
    assert hypothesis.status == "evaluating"
    assert evaluation.requests[0].hypothesis_ids == result.selected_hypothesis_ids
    assert outcomes.requests[0].hypothesis_ids == result.selected_hypothesis_ids
    assert discovery.requests[0].horizons == graph.manifest.horizons
    assert discovery.requests[0].ontology_revision == graph.manifest.ontology_revision


def test_replay_never_rediscovers_or_restarts_and_keeps_fingerprint_stable() -> None:
    workflow, _graph, lifecycle, discovery, evaluation, outcomes, _patterns, pattern_store, calls = _workflow()
    first = workflow.run(BOOT)
    event_count = sum(len(items) for items in pattern_store.hypothesis_events.values())

    replay = workflow.run(BOOT)

    assert replay.status == "completed"
    assert replay.replayed is True
    assert replay.evaluation_dataset_fingerprint == first.evaluation_dataset_fingerprint
    assert replay.selected_hypothesis_ids == first.selected_hypothesis_ids
    assert lifecycle.append_calls == 1
    assert len(discovery.requests) == 1
    assert len(evaluation.requests) == 2
    assert len(outcomes.requests) == 2
    assert sum(len(items) for items in pattern_store.hypothesis_events.values()) == event_count
    assert calls == [
        "discover",
        "evaluate",
        "persist",
        "outcome_link",
        "evaluate",
        "persist",
        "outcome_link",
    ]


def test_zero_candidates_is_a_durable_replayable_completion_and_still_links() -> None:
    workflow, _graph, lifecycle, discovery, evaluation, outcomes, *_rest = _workflow(candidates=0)
    first = workflow.run(BOOT)
    replay = workflow.run(BOOT)

    assert first.status == replay.status == "completed"
    assert lifecycle.marker is not None
    assert lifecycle.marker.selected_count == 0
    assert lifecycle.marker.selected_hypothesis_ids == ()
    assert replay.replayed is True
    assert len(discovery.requests) == 1
    assert evaluation.requests == []
    assert [request.hypothesis_ids for request in outcomes.requests] == [(), ()]


@pytest.mark.parametrize(
    ("cohorts", "reason"),
    [
        ((), "no_graph_cohort"),
        ("duplicate", "ambiguous_graph_cohort"),
    ],
)
def test_missing_or_ambiguous_graph_cohort_skips_before_discovery(cohorts, reason: str) -> None:
    store, graph = _collecting_graph()
    resolved = (graph, graph) if cohorts == "duplicate" else ()
    workflow, *_rest, calls = _workflow(query=_Query(resolved, store))

    result = workflow.run(BOOT)

    assert result.status == "skipped"
    assert _stage(result, "cohort_select").reason == reason
    assert calls == []


def test_graph_cohort_without_context_control_skips_before_discovery() -> None:
    from tests.read_models.test_world_graph_report import _graph_manifest

    service, store = _pilot_service()
    full = _graph_manifest()
    manifest = _graph_manifest(
        market_feature_contract="world_feature.market.v2",
        context_feature_contract="world_feature.graph.v1",
        sensor_requirements=tuple(
            item for item in full.sensor_requirements if item.sensor_id == "graph"
        ),
        lanes=tuple(lane for lane in full.lanes if lane.lane_id.endswith(".graph")),
    )
    service.register(RegisterWorldCohort(manifest=manifest))
    service.arm(_arm_command(manifest, sensors=("graph",)))
    service.start(_start_command(manifest))
    legacy = store.load(WorldCohortId(manifest.cohort_id))
    workflow, *_rest, calls = _workflow(query=_Query((legacy,), store))

    result = workflow.run(BOOT)

    assert result.status == "partial"
    assert _stage(result, "cohort_select").reason == "graph_cohort_missing_context_control"
    assert calls == []


def test_missing_start_availability_proof_skips_before_discovery() -> None:
    store, graph = _collecting_graph()
    started = graph.started_event
    assert started is not None
    current = store.envelope_for(started)
    store.envelopes[started.event_id] = WorldCohortEventEnvelope.bind(
        started,
        sequence=current.sequence,
        evidence=None,
    )
    workflow, *_rest, calls = _workflow(query=_Query((graph,), store))

    result = workflow.run(BOOT)

    assert result.status == "skipped"
    assert _stage(result, "cohort_select").reason == "started_availability_unproven"
    assert calls == []


def test_next_boundary_and_dataset_fingerprint_are_strict_and_deterministic() -> None:
    store, graph = _collecting_graph()
    started = graph.started_event
    assert started is not None
    cutoff = store.envelope_for(started).require_proven().effective_ready_at
    boundary = next_canonical_bar_boundary(cutoff, "15m")

    assert cutoff == datetime(2026, 8, 24, 0, 6, tzinfo=UTC)
    assert boundary == datetime(2026, 8, 24, 0, 15, tzinfo=UTC)
    assert next_canonical_bar_boundary(boundary, "15m") == datetime(2026, 8, 24, 0, 30, tzinfo=UTC)
    first = pattern_evaluation_dataset_fingerprint(
        graph,
        formation_cutoff=cutoff,
        evaluation_start_not_before=boundary,
    )
    second = pattern_evaluation_dataset_fingerprint(
        graph,
        formation_cutoff=cutoff,
        evaluation_start_not_before=boundary,
    )
    assert first == second
    assert len(first) == 64
    with pytest.raises(ValueError, match="unsupported"):
        next_canonical_bar_boundary(cutoff, "1w")


def test_before_evaluation_window_skips_matching_but_links_last() -> None:
    workflow, *_prefix, evaluation, outcomes, _patterns, _store, calls = _workflow()
    result = workflow.run(datetime(2026, 8, 24, 0, 10, tzinfo=UTC))

    assert result.status == "completed"
    assert _stage(result, "evaluation").reason == "evaluation_window_not_started"
    assert evaluation.requests == []
    assert len(outcomes.requests) == 1
    assert calls == ["discover", "outcome_link"]


def test_as_of_before_proven_formation_cutoff_has_no_pattern_side_effect() -> None:
    workflow, _graph, lifecycle, discovery, _evaluation, _outcomes, _patterns, _store, calls = _workflow()

    result = workflow.run(START_SEEN - timedelta(microseconds=1))

    assert result.status == "skipped"
    assert _stage(result, "cohort_select").reason == "as_of_before_formation_cutoff"
    assert lifecycle.marker is None
    assert discovery.requests == []
    assert calls == []


def test_failed_discovery_marker_append_never_authorizes_evaluation() -> None:
    workflow, _graph, lifecycle, _discovery, evaluation, _outcomes, _patterns, _store, calls = _workflow()
    lifecycle.append_failure = OSError("lifecycle ledger unavailable")

    result = workflow.run(BOOT)

    assert result.status == "partial"
    assert _stage(result, "discovery").status == "failed"
    assert _stage(result, "evaluation").reason == "discovery_not_completed"
    assert result.selected_hypothesis_ids == ()
    assert evaluation.requests == []
    assert calls == ["discover", "outcome_link"]


@pytest.mark.parametrize(
    ("kwargs", "failed_stage", "expected_calls"),
    [
        (
            {"discovery_failure": OSError("formation unavailable")},
            "discovery",
            ["discover", "outcome_link"],
        ),
        (
            {"evaluate_failure": OSError("evaluation unavailable")},
            "evaluation",
            ["discover", "evaluate", "outcome_link"],
        ),
        (
            {"persist_failure": OSError("prediction store unavailable")},
            "evaluation",
            ["discover", "evaluate", "persist", "outcome_link"],
        ),
        (
            {"outcome_failure": OSError("outcome store unavailable")},
            "outcome_link",
            ["discover", "evaluate", "persist", "outcome_link"],
        ),
    ],
)
def test_operational_failures_are_partial_and_outcomes_are_only_touched_last(
    kwargs,
    failed_stage: str,
    expected_calls: list[str],
) -> None:
    workflow, *_rest, calls = _workflow(**kwargs)

    result = workflow.run(BOOT)

    assert result.status == "partial"
    assert _stage(result, failed_stage).status == "failed"
    assert _stage(result, failed_stage).error is not None
    assert calls == expected_calls
    assert calls[-1] == "outcome_link"


def test_reopen_with_later_clock_replays_frozen_marker_without_rediscovery(tmp_path: Path) -> None:
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import ArmWorldCohort, RegisterWorldCohort, StartWorldCohort
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore
    from tests.read_models.test_world_graph_report import _graph_manifest, _required_sensors

    path = tmp_path / "world_model.db"
    manifest = _graph_manifest()
    calls: list[str] = []
    pattern_store = _MemoryPatternStore()
    patterns = WorldPatternService(
        hypotheses=pattern_store,
        occurrences=pattern_store,
        availability=pattern_store,
    )
    discovery = _Discovery(calls)
    evaluation = _Evaluation(calls)
    outcomes = _Outcomes(calls)

    world = WorldModelStore(path, clock=lambda: START_READY)
    try:
        service = WorldCohortService(repository=world, query=world)
        service.register(RegisterWorldCohort(manifest=manifest))
        service.arm(
            ArmWorldCohort(
                cohort_id=manifest.cohort_id,
                manifest_sha256=manifest.manifest_sha256,
                runtime_identity=manifest.runtime_identity,
                satisfied_sensor_ids=_required_sensors(manifest),
            )
        )
        service.start(
            StartWorldCohort(
                cohort_id=manifest.cohort_id,
                manifest_sha256=manifest.manifest_sha256,
                runtime_identity=manifest.runtime_identity,
            )
        )
        graph = world.load(WorldCohortId(manifest.cohort_id))
        started = graph.started_event
        assert started is not None
        first_evidence = world.envelope_for(started).require_proven()
        assert first_evidence.first_seen_at == START_READY
        assert first_evidence.receipt.ready_at == START_READY
        lifecycle = WorldPatternStore(world._db, clock=lambda: START_READY)
        first = PatternShadowWorkflow(
            cohorts=world,
            lifecycle=lifecycle,
            discovery=discovery,
            patterns=patterns,
            evaluation=evaluation,
            predictions=_Predictions(),
            outcomes=outcomes,
        ).run(BOOT)
    finally:
        world.close()

    assert first.status == "completed"
    assert first.replayed is False
    assert first.formation_cutoff == START_READY
    assert first.shadow_only is True
    assert first.decision_effect == "none"
    assert calls == ["discover", "evaluate", "persist", "outcome_link"]
    frozen_cutoff = first.formation_cutoff
    frozen_start = first.evaluation_start_not_before
    frozen_fingerprint = first.evaluation_dataset_fingerprint
    frozen_ids = first.selected_hypothesis_ids
    event_count = sum(len(items) for items in pattern_store.hypothesis_events.values())

    restarted = WorldModelStore(path, clock=lambda: LATER_BOOT)
    try:
        restarted_graph = restarted.load(WorldCohortId(manifest.cohort_id))
        restarted_started = restarted_graph.started_event
        assert restarted_started is not None
        later_evidence = restarted.envelope_for(restarted_started).require_proven()
        assert later_evidence.receipt.ready_at == START_READY
        assert later_evidence.first_seen_at == LATER_BOOT
        assert later_evidence.effective_ready_at == LATER_BOOT
        assert later_evidence.first_seen_at > frozen_cutoff
        lifecycle = WorldPatternStore(restarted._db, clock=lambda: LATER_BOOT)
        replay = PatternShadowWorkflow(
            cohorts=restarted,
            lifecycle=lifecycle,
            discovery=discovery,
            patterns=patterns,
            evaluation=evaluation,
            predictions=_Predictions(),
            outcomes=outcomes,
        ).run(BOOT)
    finally:
        restarted.close()

    assert replay.status == "completed"
    assert replay.replayed is True
    assert replay.formation_cutoff == frozen_cutoff
    assert replay.evaluation_start_not_before == frozen_start
    assert replay.evaluation_dataset_fingerprint == frozen_fingerprint
    assert replay.selected_hypothesis_ids == frozen_ids
    assert replay.formation_cutoff != later_evidence.effective_ready_at
    assert replay.shadow_only is True
    assert replay.decision_effect == "none"
    assert replay.learning_authority == "shadow_only"
    assert len(discovery.requests) == 1
    assert len(evaluation.requests) == 2
    assert len(outcomes.requests) == 2
    assert evaluation.requests[1].evaluation_dataset_fingerprint == frozen_fingerprint
    assert evaluation.requests[1].hypothesis_ids == frozen_ids
    assert outcomes.requests[1].hypothesis_ids == frozen_ids
    assert sum(len(items) for items in pattern_store.hypothesis_events.values()) == event_count
    assert calls == [
        "discover",
        "evaluate",
        "persist",
        "outcome_link",
        "evaluate",
        "persist",
        "outcome_link",
    ]


def test_outcome_query_unavailable_marks_outcome_link_failed_and_partial() -> None:
    workflow, *_rest, calls = _workflow(outcome_failure=PatternQueryUnavailable("load_active_observed_leaves failed"))
    result = workflow.run(BOOT)
    assert result.status == "partial"
    assert _stage(result, "outcome_link").status == "failed"
    assert "PatternQueryUnavailable" in str(_stage(result, "outcome_link").error)
    assert calls[-1] == "outcome_link"


def test_reopen_sqlite_stores_with_later_clock_continues_evaluation_and_linking(tmp_path: Path) -> None:
    from tests.read_models.test_world_graph_report import _graph_manifest, _required_sensors
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import ArmWorldCohort, RegisterWorldCohort, StartWorldCohort
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    path = tmp_path / "world_model.db"
    manifest = _graph_manifest()
    calls: list[str] = []
    discovery = _Discovery(calls)
    evaluation = _Evaluation(calls)
    outcomes = _Outcomes(calls)

    world = WorldModelStore(path, clock=lambda: START_READY)
    try:
        service = WorldCohortService(repository=world, query=world)
        service.register(RegisterWorldCohort(manifest=manifest))
        service.arm(
            ArmWorldCohort(
                cohort_id=manifest.cohort_id,
                manifest_sha256=manifest.manifest_sha256,
                runtime_identity=manifest.runtime_identity,
                satisfied_sensor_ids=_required_sensors(manifest),
            )
        )
        service.start(
            StartWorldCohort(
                cohort_id=manifest.cohort_id,
                manifest_sha256=manifest.manifest_sha256,
                runtime_identity=manifest.runtime_identity,
            )
        )
        graph = world.load(WorldCohortId(manifest.cohort_id))
        started = graph.started_event
        assert started is not None
        started_event_id = started.event_id
        lifecycle = WorldPatternStore(world._db, clock=lambda: START_READY)
        patterns = WorldPatternService(hypotheses=lifecycle, occurrences=lifecycle, availability=lifecycle)
        first = PatternShadowWorkflow(
            cohorts=world,
            lifecycle=lifecycle,
            discovery=discovery,
            patterns=patterns,
            evaluation=evaluation,
            predictions=_Predictions(),
            outcomes=outcomes,
        ).run(BOOT)
        frozen_marker = lifecycle.get_discovery_completed(manifest.cohort_id, started_event_id)
    finally:
        world.close()

    assert first.status == "completed"
    assert first.replayed is False
    assert first.shadow_only is True
    assert first.decision_effect == "none"
    assert frozen_marker is not None
    frozen_cutoff = first.formation_cutoff
    frozen_start = first.evaluation_start_not_before
    frozen_fingerprint = first.evaluation_dataset_fingerprint
    frozen_ids = first.selected_hypothesis_ids
    assert frozen_marker.selected_hypothesis_ids == frozen_ids
    assert frozen_marker.formation_cutoff == frozen_cutoff
    assert frozen_marker.evaluation_start_not_before == frozen_start
    assert frozen_marker.evaluation_dataset_fingerprint == frozen_fingerprint

    class _NoRediscovery:
        def discover(self, request):  # noqa: ANN001
            raise AssertionError("rediscovery after sqlite reopen is forbidden")

    restarted = WorldModelStore(path, clock=lambda: LATER_BOOT)
    try:
        later_graph = restarted.load(WorldCohortId(manifest.cohort_id))
        later_started = later_graph.started_event
        assert later_started is not None
        later_evidence = restarted.envelope_for(later_started).require_proven()
        assert later_evidence.first_seen_at == LATER_BOOT
        assert later_evidence.receipt.ready_at == START_READY
        lifecycle = WorldPatternStore(restarted._db, clock=lambda: LATER_BOOT)
        replayed_marker = lifecycle.get_discovery_completed(manifest.cohort_id, later_started.event_id)
        assert replayed_marker == frozen_marker
        listed = lifecycle.list_evaluating_hypotheses(
            evaluation_cohort_id=manifest.cohort_id,
            evaluation_dataset_fingerprint=frozen_fingerprint,
            hypothesis_ids=frozen_ids,
        )
        assert [item.hypothesis_id for item in listed] == list(frozen_ids)
        replay = PatternShadowWorkflow(
            cohorts=restarted,
            lifecycle=lifecycle,
            discovery=_NoRediscovery(),
            patterns=WorldPatternService(hypotheses=lifecycle, occurrences=lifecycle, availability=lifecycle),
            evaluation=evaluation,
            predictions=_Predictions(),
            outcomes=outcomes,
        ).run(BOOT)
    finally:
        restarted.close()

    assert replay.status == "completed"
    assert replay.replayed is True
    assert replay.formation_cutoff == frozen_cutoff
    assert replay.evaluation_start_not_before == frozen_start
    assert replay.evaluation_dataset_fingerprint == frozen_fingerprint
    assert replay.selected_hypothesis_ids == frozen_ids
    assert replay.shadow_only is True
    assert replay.decision_effect == "none"
    assert replay.learning_authority == "shadow_only"
    assert len(discovery.requests) == 1
    assert len(evaluation.requests) == 2
    assert len(outcomes.requests) == 2
    assert evaluation.requests[1].hypothesis_ids == frozen_ids
    assert outcomes.requests[1].hypothesis_ids == frozen_ids
    assert calls == [
        "discover",
        "evaluate",
        "persist",
        "outcome_link",
        "evaluate",
        "persist",
        "outcome_link",
    ]


def test_unripe_formation_does_not_write_empty_discovery_marker() -> None:
    workflow, _graph, lifecycle, discovery, evaluation, *_rest, calls = _workflow(
        candidates=0,
        considered_records=0,
        eligible_records=0,
    )
    result = workflow.run(BOOT)

    assert lifecycle.marker is None
    assert lifecycle.append_calls == 0
    assert result.status == "partial"
    assert _stage(result, "discovery").status == "skipped"
    assert _stage(result, "discovery").reason == "no_ripe_exact_records_at_formation_cutoff"
    assert _stage(result, "evaluation").reason == "discovery_not_completed"
    assert result.selected_hypothesis_ids == ()
    assert evaluation.requests == []
    assert calls == ["discover", "outcome_link"]
    replay = workflow.run(BOOT)
    assert lifecycle.marker is None
    assert replay.status == "partial"
    assert len(discovery.requests) == 2
    assert replay.replayed is False
    assert replay.formation_cutoff == result.formation_cutoff


def test_post_start_exact_records_cannot_form_on_frozen_cutoff() -> None:
    source = _CutoffAwareFormationSource()
    workflow, graph, lifecycle, *_rest = _workflow(discovery=PatternDiscoveryService(source))
    first = workflow.run(BOOT)

    assert first.status == "partial"
    assert lifecycle.marker is None
    assert _stage(first, "discovery").reason == "no_ripe_exact_records_at_formation_cutoff"
    cutoff = first.formation_cutoff
    assert cutoff == START_SEEN

    source.records.append(
        _record(
            as_of=cutoff + timedelta(days=2),
            ontology_revision=graph.manifest.ontology_revision,
            available_at=cutoff + timedelta(days=3),
        )
    )
    second = workflow.run(BOOT + timedelta(days=4))

    assert second.formation_cutoff == cutoff
    assert second.status == "partial"
    assert lifecycle.marker is None
    assert lifecycle.append_calls == 0
    assert second.replayed is False
    assert _stage(second, "discovery").reason == "no_ripe_exact_records_at_formation_cutoff"
    assert _stage(second, "evaluation").reason == "discovery_not_completed"


def test_predecessor_same_revision_records_are_admitted_at_successor_cutoff() -> None:
    source = _CutoffAwareFormationSource()
    workflow, graph, lifecycle, *_rest = _workflow(discovery=PatternDiscoveryService(source))
    as_of = START_SEEN - timedelta(days=2)
    source.records.append(
        _record(
            as_of=as_of,
            ontology_revision=graph.manifest.ontology_revision,
            available_at=as_of + timedelta(days=1, minutes=5),
        )
    )
    result = workflow.run(BOOT)

    assert result.formation_cutoff == START_SEEN
    assert lifecycle.marker is not None
    assert lifecycle.marker.selected_count == 0
    assert _stage(result, "discovery").reason == "discovered"
    assert result.status == "completed"


def test_examined_zero_candidates_remains_a_durable_empty_marker() -> None:
    workflow, _graph, lifecycle, *_rest = _workflow(candidates=0, considered_records=20, eligible_records=20)
    result = workflow.run(BOOT)
    assert lifecycle.marker is not None
    assert lifecycle.marker.selected_count == 0
    assert result.status == "completed"
    assert _stage(result, "discovery").reason == "discovered"


def test_all_incompatible_evaluation_is_visible_on_the_workflow() -> None:
    class _Incompatible(_Evaluation):
        def evaluate(self, request: PatternEvaluationRequest) -> PatternEvaluationResult:
            self.calls.append("evaluate")
            self.requests.append(request)
            return PatternEvaluationResult(
                matches=(),
                hypotheses=(),
                eligible_records=0,
                rejection_counts={"ontology_revision_mismatch": 5},
                source_evidence_ids=("snap-1",),
                request=request,
                considered_records=5,
            )

    calls: list[str] = []
    workflow, *_rest = _workflow(evaluation=_Incompatible(calls))
    result = workflow.run(BOOT)
    assert result.status == "partial"
    assert result.evaluation_considered_records == 5
    assert result.evaluation_eligible_records == 0
    assert result.evaluation_rejection_counts["ontology_revision_mismatch"] == 5
    assert _stage(result, "evaluation").status == "skipped"
    assert _stage(result, "evaluation").reason == "all_records_incompatible"
    payload = result.to_dict()
    assert payload["status"] == "partial"
    assert payload["evaluation_considered_records"] == 5
    assert payload["evaluation_eligible_records"] == 0
    assert payload["evaluation_rejection_counts"]["ontology_revision_mismatch"] == 5


def test_ended_evaluation_cohort_closes_hypotheses_without_rewriting_occurrences() -> None:
    from trader.application.world_model.cohort_service import WorldCohortService

    store, graph = _collecting_graph()
    calls: list[str] = []
    pattern_store = _MemoryPatternStore()
    patterns = WorldPatternService(
        hypotheses=pattern_store,
        occurrences=pattern_store,
        availability=pattern_store,
    )
    workflow = PatternShadowWorkflow(
        cohorts=store,
        lifecycle=_Lifecycle(),
        discovery=_Discovery(calls),
        patterns=patterns,
        evaluation=_Evaluation(calls),
        predictions=_Predictions(),
        outcomes=_Outcomes(calls),
    )
    first = workflow.run(BOOT)
    assert first.status == "completed"
    hypothesis_id = first.selected_hypothesis_ids[0]
    assert pattern_store.load(PatternHypothesisId(hypothesis_id)).status == "evaluating"
    recorded = _record_occurrence(
        patterns,
        hypothesis_id,
        cohort_id=graph.cohort_id,
        forecast=_prediction(model_id=EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY),
    )
    occurrence_id = recorded.event.occurrence_id
    occurrence_events = tuple(event.event_id for event in pattern_store.load(PatternOccurrenceId(occurrence_id)).events)
    hypothesis_events_before = tuple(
        event.event_id for event in pattern_store.load(PatternHypothesisId(hypothesis_id)).events
    )
    WorldCohortService(repository=store, query=store).close(
        graph.cohort_id,
        CloseWorldCohort(reason="fixed_end reached"),
    )
    assert store.load(WorldCohortId(graph.cohort_id)).phase is CohortPhase.COLLECTION_CLOSED
    closed_run = workflow.run(BOOT + timedelta(hours=1))
    loaded = pattern_store.load(PatternHypothesisId(hypothesis_id))
    assert loaded.status == "evaluation_closed"
    assert isinstance(loaded.events[-1], PatternEvaluationClosed)
    assert tuple(event.event_id for event in loaded.events[:-1]) == hypothesis_events_before
    assert tuple(
        event.event_id for event in pattern_store.load(PatternOccurrenceId(occurrence_id)).events
    ) == occurrence_events
    assert _stage(closed_run, "evaluation_close").status == "completed"
    replay = workflow.run(BOOT + timedelta(hours=2))
    replayed = pattern_store.load(PatternHypothesisId(hypothesis_id))
    assert replayed.status == "evaluation_closed"
    assert replayed.events[-1].event_id == loaded.events[-1].event_id
    assert sum(1 for event in replayed.events if isinstance(event, PatternEvaluationClosed)) == 1
    assert _stage(replay, "evaluation_close").reason == "none_due"
    linked = _link(patterns, occurrence_id, _outcome())
    assert linked.event.occurrence_id == occurrence_id


def test_elapsed_collecting_evaluation_cohort_closes_hypotheses_at_fixed_end() -> None:
    store, graph = _collecting_graph()
    calls: list[str] = []
    pattern_store = _MemoryPatternStore()
    patterns = WorldPatternService(
        hypotheses=pattern_store,
        occurrences=pattern_store,
        availability=pattern_store,
    )
    workflow = PatternShadowWorkflow(
        cohorts=store,
        lifecycle=_Lifecycle(),
        discovery=_Discovery(calls),
        patterns=patterns,
        evaluation=_Evaluation(calls),
        predictions=_Predictions(),
        outcomes=_Outcomes(calls),
    )
    first = workflow.run(BOOT)
    hypothesis_id = first.selected_hypothesis_ids[0]
    at_end = workflow.run(graph.manifest.collection_stop_rule.at)
    assert pattern_store.load(PatternHypothesisId(hypothesis_id)).status == "evaluating"
    assert _stage(at_end, "evaluation_close").reason == "none_due"
    after_end = workflow.run(graph.manifest.collection_stop_rule.at + timedelta(microseconds=1))
    assert pattern_store.load(PatternHypothesisId(hypothesis_id)).status == "evaluation_closed"
    assert store.load(WorldCohortId(graph.cohort_id)).phase is CohortPhase.COLLECTING
    assert _stage(after_end, "evaluation_close").status == "completed"


def test_live_evaluation_cohort_does_not_close_hypotheses() -> None:
    live_store, live_graph = _collecting_graph()
    live_calls: list[str] = []
    live_patterns_store = _MemoryPatternStore()
    live_patterns = WorldPatternService(
        hypotheses=live_patterns_store,
        occurrences=live_patterns_store,
        availability=live_patterns_store,
    )
    live_workflow = PatternShadowWorkflow(
        cohorts=live_store,
        lifecycle=_Lifecycle(),
        discovery=_Discovery(live_calls),
        patterns=live_patterns,
        evaluation=_Evaluation(live_calls),
        predictions=_Predictions(),
        outcomes=_Outcomes(live_calls),
    )
    live_first = live_workflow.run(BOOT)
    live_replay = live_workflow.run(BOOT + timedelta(hours=1))
    live_id = live_first.selected_hypothesis_ids[0]
    assert live_patterns_store.load(PatternHypothesisId(live_id)).status == "evaluating"
    assert live_graph.phase is CohortPhase.COLLECTING
    assert _stage(live_replay, "evaluation_close").reason == "none_due"


def test_module_stays_application_owned_without_runtime_or_storage_imports() -> None:
    tree = ast.parse(Path(MODULE_PATH).read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    forbidden = ("trader.infrastructure", "trader.runtime", "trader.interfaces", "trader.reporting")
    leaks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in forbidden):
                leaks.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in forbidden):
                    leaks.append(alias.name)
    assert leaks == []
