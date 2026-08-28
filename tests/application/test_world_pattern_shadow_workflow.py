from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.application.test_world_cohort_service import START_READY, START_SEEN, _arm_command, _start_command
from tests.application.test_world_pattern_service import _MemoryPatternStore, _spec
from tests.application.test_world_pilot_activation import BOOT, LATER_BOOT, _activate, _service as _pilot_service
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.pattern_discovery import (
    PatternDiscoveryCandidate,
    PatternDiscoveryResult,
)
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
from trader.domain.world_cohort import WorldCohort, WorldCohortEvent, WorldCohortEventEnvelope, WorldCohortId
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_feature_contract import graph_content_mask, graph_feature_contract
from trader.domain.world_pattern import (
    EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
    PatternDiscoveryCompleted,
    PatternHypothesisId,
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
    def __init__(self, calls: list[str], *, candidates: int = 1, failure: Exception | None = None) -> None:
        self.calls = calls
        self.candidates = candidates
        self.failure = failure
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
                ontology_revision="market_ontology.v1",
            ),
            semantic_signature="instrument:TRADED_ON:forward:venue",
        )
        return PatternDiscoveryResult(
            candidates=() if self.candidates == 0 else (candidate,),
            formation_dataset_fingerprint=FORMATION_FP,
            eligible_records=20,
            rejection_counts={},
            source_evidence_ids=(),
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

    def envelope_for(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope:
        assert self.delegate is not None
        return self.delegate.envelope_for(event)


def _collecting_graph() -> tuple[object, WorldCohort]:
    service, store = _pilot_service()
    report = _activate(cohort_service=service, now=BOOT)
    graph_id = next(item["cohort_id"] for item in report.cohorts if item["key"] == "graph")
    registered = store.load(WorldCohortId(graph_id))
    service.arm(_arm_command(registered.manifest, sensors=("graph",)))
    service.start(_start_command(registered.manifest))
    return store, store.load(WorldCohortId(graph_id))


def _workflow(
    *,
    query=None,
    candidates: int = 1,
    discovery_failure: Exception | None = None,
    evaluate_failure: Exception | None = None,
    persist_failure: Exception | None = None,
    outcome_failure: Exception | None = None,
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
    discovery = _Discovery(calls, candidates=candidates, failure=discovery_failure)
    evaluation = _Evaluation(
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
