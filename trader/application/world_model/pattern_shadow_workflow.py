"""Automatic, fail-open lifecycle for prospective graph-pattern evaluation.

This application service coordinates the already separated discovery,
prospective matching, persistence, and outcome-linking use cases.  It has no
runtime, infrastructure, CLI, trading, or broker dependency.  Formation may
read historical labels through its own port; prospective evaluation remains
unlabelled and the outcome service is invoked last.

Evaluating hypotheses are closed through ``ClosePatternEvaluation`` when
their bound evaluation cohort is ``collection_closed`` or past its inclusive
fixed end. Occurrences, outcomes, snapshots, markers, and slots stay
append-only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Literal, Protocol

from trader.application.world_model.cohort_ports import WorldCohortQuery
from trader.application.world_model.pattern_discovery import PatternDiscoveryResult
from trader.application.world_model.pattern_evaluation_ports import PatternPredictionWriter
from trader.application.world_model.pattern_evaluation_request import (
    PatternEvaluationPersistResult,
    PatternEvaluationRequest,
    PatternEvaluationResult,
)
from trader.application.world_model.pattern_formation_request import PatternFormationRequest
from trader.application.world_model.pattern_lifecycle_ports import PatternLifecycleLedger
from trader.application.world_model.pattern_outcome_link import (
    PatternOutcomeLinkRequest,
    PatternOutcomeLinkResult,
)
from trader.application.world_model.pattern_service import (
    ClosePatternEvaluation,
    RegisterPatternHypothesis,
    StartPatternEvaluation,
    WorldPatternService,
)
from trader.domain.world_availability import AvailabilityEvidence
from trader.domain.world_cohort import CohortPhase, WorldCohort, WorldCohortId
from trader.domain.world_episode import canonical_sha256, parse_bar_interval, parse_utc_timestamp
from trader.domain.world_feature_contract import graph_content_mask, graph_feature_contract
from trader.domain.world_pattern import EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY, PatternDiscoveryCompleted


PATTERN_EVALUATION_DATASET_SCHEMA = "pattern_evaluation_dataset.v1"
PatternShadowStageStatus = Literal["completed", "skipped", "failed"]
PatternShadowWorkflowStatus = Literal["completed", "partial", "skipped"]


class PatternDiscoveryPort(Protocol):
    def discover(self, request: PatternFormationRequest) -> PatternDiscoveryResult: ...


class PatternEvaluationPort(Protocol):
    def evaluate(self, request: PatternEvaluationRequest) -> PatternEvaluationResult: ...

    def persist(
        self,
        result: PatternEvaluationResult,
        *,
        patterns: WorldPatternService,
        predictions: PatternPredictionWriter,
    ) -> PatternEvaluationPersistResult: ...


class PatternOutcomeLinkPort(Protocol):
    def link(
        self,
        request: PatternOutcomeLinkRequest,
        *,
        patterns: WorldPatternService,
    ) -> PatternOutcomeLinkResult: ...


@dataclass(frozen=True)
class PatternShadowStageResult:
    stage: str
    status: PatternShadowStageStatus
    reason: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        stage = str(self.stage).strip()
        if not stage:
            raise ValueError("stage must be a non-empty string")
        if self.status not in {"completed", "skipped", "failed"}:
            raise ValueError("status must be completed, skipped, or failed")
        if self.status == "failed" and not self.error:
            raise ValueError("failed stage requires an error")
        object.__setattr__(self, "stage", stage)

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "status": self.status,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass(frozen=True)
class PatternShadowWorkflowResult:
    as_of: datetime | str
    status: PatternShadowWorkflowStatus
    stages: tuple[PatternShadowStageResult, ...]
    evaluation_cohort_id: str | None = None
    formation_cutoff: datetime | str | None = None
    evaluation_start_not_before: datetime | str | None = None
    evaluation_dataset_fingerprint: str | None = None
    selected_hypothesis_ids: tuple[str, ...] = ()
    replayed: bool = False
    discovered_count: int = 0
    persisted_prediction_count: int = 0
    persisted_occurrence_count: int = 0
    linked_outcome_count: int = 0
    evaluation_considered_records: int = 0
    evaluation_eligible_records: int = 0
    evaluation_rejection_counts: Mapping[str, int] | None = None
    shadow_only: bool = True
    decision_effect: str = "none"
    learning_authority: str = "shadow_only"

    def __post_init__(self) -> None:
        as_of = parse_utc_timestamp(self.as_of, "as_of")
        formation = (
            None if self.formation_cutoff is None else parse_utc_timestamp(self.formation_cutoff, "formation_cutoff")
        )
        evaluation_start = (
            None
            if self.evaluation_start_not_before is None
            else parse_utc_timestamp(self.evaluation_start_not_before, "evaluation_start_not_before")
        )
        if self.status not in {"completed", "partial", "skipped"}:
            raise ValueError("status must be completed, partial, or skipped")
        if self.shadow_only is not True:
            raise ValueError("shadow_only must remain true")
        if self.decision_effect != "none":
            raise ValueError("decision_effect must be none")
        if self.learning_authority != "shadow_only":
            raise ValueError("learning_authority must be shadow_only")
        for name in (
            "discovered_count",
            "persisted_prediction_count",
            "persisted_occurrence_count",
            "linked_outcome_count",
            "evaluation_considered_records",
            "evaluation_eligible_records",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        rejection_counts = self.evaluation_rejection_counts
        if rejection_counts is None:
            frozen_rejections: Mapping[str, int] = MappingProxyType({})
        else:
            if not isinstance(rejection_counts, Mapping):
                raise TypeError("evaluation_rejection_counts must be a mapping")
            frozen_rejections = MappingProxyType(
                {str(key): int(value) for key, value in rejection_counts.items()}
            )
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "formation_cutoff", formation)
        object.__setattr__(self, "evaluation_start_not_before", evaluation_start)
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "selected_hypothesis_ids", tuple(self.selected_hypothesis_ids))
        object.__setattr__(self, "evaluation_rejection_counts", frozen_rejections)
        object.__setattr__(self, "shadow_only", True)
        object.__setattr__(self, "decision_effect", "none")
        object.__setattr__(self, "learning_authority", "shadow_only")

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "status": self.status,
            "stages": [item.to_dict() for item in self.stages],
            "evaluation_cohort_id": self.evaluation_cohort_id,
            "formation_cutoff": None if self.formation_cutoff is None else self.formation_cutoff.isoformat(),
            "evaluation_start_not_before": (
                None if self.evaluation_start_not_before is None else self.evaluation_start_not_before.isoformat()
            ),
            "evaluation_dataset_fingerprint": self.evaluation_dataset_fingerprint,
            "selected_hypothesis_ids": list(self.selected_hypothesis_ids),
            "replayed": self.replayed,
            "discovered_count": self.discovered_count,
            "persisted_prediction_count": self.persisted_prediction_count,
            "persisted_occurrence_count": self.persisted_occurrence_count,
            "linked_outcome_count": self.linked_outcome_count,
            "evaluation_considered_records": self.evaluation_considered_records,
            "evaluation_eligible_records": self.evaluation_eligible_records,
            "evaluation_rejection_counts": dict(self.evaluation_rejection_counts),
            "shadow_only": self.shadow_only,
            "decision_effect": self.decision_effect,
            "learning_authority": self.learning_authority,
        }


def next_canonical_bar_boundary(cutoff: datetime | str, bar_interval: str) -> datetime:
    """Return the first UTC epoch-grid boundary strictly after ``cutoff``."""

    resolved = parse_utc_timestamp(cutoff, "cutoff")
    duration = parse_bar_interval(bar_interval)
    if duration is None:
        raise ValueError(f"unsupported bar_interval: {bar_interval!r}")
    duration_microseconds = round(duration.total_seconds() * 1_000_000)
    if duration_microseconds <= 0:
        raise ValueError("bar_interval must have a positive duration")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = resolved - epoch
    elapsed_microseconds = elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    next_microseconds = (elapsed_microseconds // duration_microseconds + 1) * duration_microseconds
    return epoch + timedelta(microseconds=next_microseconds)


def pattern_evaluation_dataset_fingerprint(
    cohort: WorldCohort,
    *,
    formation_cutoff: datetime | str,
    evaluation_start_not_before: datetime | str,
) -> str:
    """Bind the prospective dataset to the proven cohort start, never later slots."""

    if not isinstance(cohort, WorldCohort):
        raise TypeError("cohort must be a WorldCohort")
    started = cohort.started_event
    if started is None:
        raise ValueError("cohort must have a started_event")
    formation = parse_utc_timestamp(formation_cutoff, "formation_cutoff")
    evaluation_start = parse_utc_timestamp(evaluation_start_not_before, "evaluation_start_not_before")
    contract = graph_feature_contract()
    mask = graph_content_mask()
    return canonical_sha256(
        {
            "schema_version": PATTERN_EVALUATION_DATASET_SCHEMA,
            "evaluation_cohort_id": cohort.cohort_id,
            "manifest_sha256": cohort.manifest.manifest_sha256,
            "started_event_id": started.event_id,
            "formation_cutoff": formation.isoformat(),
            "evaluation_start_not_before": evaluation_start.isoformat(),
            "feature_contract_id": contract.contract_id,
            "feature_contract_fingerprint": contract.fingerprint,
            "feature_mask_id": mask.mask_id,
            "feature_mask_fingerprint": mask.fingerprint,
            "model_identity": EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
        }
    )


def _graph_only(cohort: WorldCohort) -> bool:
    lane_ids = tuple(lane.lane_id for lane in cohort.manifest.lanes)
    return bool(lane_ids) and all(lane_id.endswith(".graph") for lane_id in lane_ids)


def _collection_window_elapsed(cohort: WorldCohort, now: datetime) -> bool:
    """Inclusive end: the window is still open at collection_stop_rule.at."""

    return now > cohort.manifest.collection_stop_rule.at


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}:{exc}"


def _require_stable_discovery_marker(
    marker: PatternDiscoveryCompleted,
    cohort: WorldCohort,
    evidence: AvailabilityEvidence,
) -> None:
    """Replay identity/manifest/receipt facts. Process-local first_seen_at is not a fact."""

    started = cohort.started_event
    if started is None or marker.started_event_id != started.event_id:
        raise ValueError("discovery lifecycle marker conflicts with the proven cohort start")
    if marker.evaluation_cohort_id != cohort.cohort_id:
        raise ValueError("discovery lifecycle marker conflicts with the proven cohort start")
    if marker.manifest_sha256 != cohort.manifest.manifest_sha256:
        raise ValueError("discovery lifecycle marker conflicts with the proven cohort start")
    if evidence.receipt.ready_at > marker.formation_cutoff:
        raise ValueError("discovery lifecycle marker conflicts with the proven cohort start")
    expected_start = next_canonical_bar_boundary(marker.formation_cutoff, cohort.manifest.bar_interval)
    if marker.evaluation_start_not_before != expected_start:
        raise ValueError("discovery lifecycle marker conflicts with the proven cohort start")
    expected_fingerprint = pattern_evaluation_dataset_fingerprint(
        cohort,
        formation_cutoff=marker.formation_cutoff,
        evaluation_start_not_before=marker.evaluation_start_not_before,
    )
    if marker.evaluation_dataset_fingerprint != expected_fingerprint:
        raise ValueError("discovery lifecycle marker conflicts with the proven cohort start")


@dataclass(frozen=True)
class PatternShadowWorkflow:
    cohorts: WorldCohortQuery
    lifecycle: PatternLifecycleLedger
    discovery: PatternDiscoveryPort
    patterns: WorldPatternService
    evaluation: PatternEvaluationPort
    predictions: PatternPredictionWriter
    outcomes: PatternOutcomeLinkPort

    def _close_ended_evaluations(self, as_of: datetime) -> PatternShadowStageResult:
        load = getattr(self.cohorts, "load", None)
        if not callable(load):
            return PatternShadowStageResult("evaluation_close", "skipped", reason="close_path_unavailable")
        closed = 0
        try:
            for hypothesis in self.patterns.hypotheses.list_hypotheses():
                if hypothesis.status != "evaluating":
                    continue
                cohort_id = hypothesis.evaluation_cohort_id
                if not cohort_id:
                    continue
                try:
                    cohort = load(WorldCohortId(cohort_id))
                except LookupError:
                    continue
                if not isinstance(cohort, WorldCohort):
                    continue
                ended = cohort.phase in {CohortPhase.COLLECTION_CLOSED, CohortPhase.COMPLETE} or (
                    cohort.phase is CohortPhase.COLLECTING and _collection_window_elapsed(cohort, as_of)
                )
                if not ended:
                    continue
                self.patterns.close(
                    ClosePatternEvaluation(hypothesis_id=hypothesis.hypothesis_id, closed_at=as_of)
                )
                closed += 1
        except Exception as exc:  # noqa: BLE001 - pattern close cannot affect Trader
            return PatternShadowStageResult("evaluation_close", "failed", error=_error(exc))
        if closed == 0:
            return PatternShadowStageResult("evaluation_close", "skipped", reason="none_due")
        return PatternShadowStageResult("evaluation_close", "completed", reason="closed")

    def run(self, as_of: datetime | str) -> PatternShadowWorkflowResult:
        resolved_as_of = parse_utc_timestamp(as_of, "as_of")
        stages: list[PatternShadowStageResult] = []
        stages.append(self._close_ended_evaluations(resolved_as_of))
        try:
            graph_cohorts = tuple(filter(_graph_only, self.cohorts.list_collecting_cohorts()))
        except Exception as exc:  # noqa: BLE001 - shadow workflow is fail-open
            stages.append(PatternShadowStageResult("cohort_select", "failed", error=_error(exc)))
            return PatternShadowWorkflowResult(resolved_as_of, "partial", tuple(stages))
        if not graph_cohorts:
            stages.append(PatternShadowStageResult("cohort_select", "skipped", reason="no_graph_cohort"))
            return PatternShadowWorkflowResult(resolved_as_of, "skipped", tuple(stages))
        if len(graph_cohorts) != 1:
            stages.append(PatternShadowStageResult("cohort_select", "skipped", reason="ambiguous_graph_cohort"))
            return PatternShadowWorkflowResult(resolved_as_of, "skipped", tuple(stages))

        cohort = graph_cohorts[0]
        started = cohort.started_event
        if started is None:
            stages.append(PatternShadowStageResult("cohort_select", "skipped", reason="missing_started_event"))
            return PatternShadowWorkflowResult(
                resolved_as_of,
                "skipped",
                tuple(stages),
                evaluation_cohort_id=cohort.cohort_id,
            )
        try:
            started_envelope = self.cohorts.envelope_for(started)
        except Exception as exc:  # noqa: BLE001 - shadow workflow is fail-open
            stages.append(PatternShadowStageResult("cohort_select", "failed", error=_error(exc)))
            return PatternShadowWorkflowResult(
                resolved_as_of,
                "partial",
                tuple(stages),
                evaluation_cohort_id=cohort.cohort_id,
            )
        if started_envelope.evidence is None:
            stages.append(PatternShadowStageResult("cohort_select", "skipped", reason="started_availability_unproven"))
            return PatternShadowWorkflowResult(
                resolved_as_of,
                "skipped",
                tuple(stages),
                evaluation_cohort_id=cohort.cohort_id,
            )

        evidence = started_envelope.require_proven()
        existing_marker: PatternDiscoveryCompleted | None = None
        marker_lookup_error: Exception | None = None
        try:
            existing_marker = self.lifecycle.get_discovery_completed(cohort.cohort_id, started.event_id)
        except Exception as exc:  # noqa: BLE001 - ledger read cannot affect Trader
            marker_lookup_error = exc

        if existing_marker is not None:
            # Marker is the frozen window. first_seen_at is process-local.
            formation_cutoff = existing_marker.formation_cutoff
            evaluation_start = existing_marker.evaluation_start_not_before
            evaluation_fingerprint = existing_marker.evaluation_dataset_fingerprint
        else:
            formation_cutoff = evidence.effective_ready_at
            try:
                evaluation_start = next_canonical_bar_boundary(formation_cutoff, cohort.manifest.bar_interval)
                evaluation_fingerprint = pattern_evaluation_dataset_fingerprint(
                    cohort,
                    formation_cutoff=formation_cutoff,
                    evaluation_start_not_before=evaluation_start,
                )
            except Exception as exc:  # noqa: BLE001 - unsupported manifest stays fail-open
                stages.append(
                    PatternShadowStageResult(
                        "cohort_select", "skipped", reason="invalid_evaluation_window", error=_error(exc)
                    )
                )
                return PatternShadowWorkflowResult(
                    resolved_as_of,
                    "skipped",
                    tuple(stages),
                    evaluation_cohort_id=cohort.cohort_id,
                    formation_cutoff=formation_cutoff,
                )
        if resolved_as_of < formation_cutoff:
            stages.append(PatternShadowStageResult("cohort_select", "skipped", reason="as_of_before_formation_cutoff"))
            return PatternShadowWorkflowResult(
                resolved_as_of,
                "skipped",
                tuple(stages),
                evaluation_cohort_id=cohort.cohort_id,
                formation_cutoff=formation_cutoff,
            )
        stages.append(PatternShadowStageResult("cohort_select", "completed"))

        selected_ids: tuple[str, ...] = ()
        discovered_count = 0
        replayed = False
        marker: PatternDiscoveryCompleted | None = None
        try:
            if marker_lookup_error is not None:
                raise marker_lookup_error
            if existing_marker is not None:
                _require_stable_discovery_marker(existing_marker, cohort, evidence)
                marker = existing_marker
                selected_ids = marker.selected_hypothesis_ids
                discovered_count = marker.selected_count
                replayed = True
            else:
                discovery_result = self.discovery.discover(
                    PatternFormationRequest(
                        formation_cutoff=formation_cutoff,
                        evaluation_start_not_before=evaluation_start,
                        horizons=cohort.manifest.horizons,
                        ontology_revision=cohort.manifest.ontology_revision,
                    )
                )
                discovered_count = len(discovery_result.candidates)
                if (
                    not discovery_result.candidates
                    and discovery_result.eligible_records == 0
                    and discovery_result.considered_records == 0
                ):
                    stages.append(
                        PatternShadowStageResult(
                            "discovery",
                            "skipped",
                            reason="no_ripe_exact_records_at_formation_cutoff",
                        )
                    )
                else:
                    registered_ids: list[str] = []
                    for candidate in discovery_result.candidates:
                        spec = candidate.spec
                        if (
                            spec.formation_cutoff != formation_cutoff
                            or spec.evaluation_start_not_before != evaluation_start
                        ):
                            raise ValueError("discovery candidate window does not match the proven cohort start")
                        if spec.ontology_revision != cohort.manifest.ontology_revision:
                            raise ValueError("discovery candidate ontology_revision does not match the cohort")
                        registered = self.patterns.register(
                            RegisterPatternHypothesis(spec=spec, registered_at=formation_cutoff)
                        )
                        hypothesis_id = registered.event.hypothesis_id
                        self.patterns.start(
                            StartPatternEvaluation(
                                hypothesis_id=hypothesis_id,
                                evaluation_cohort_id=cohort.cohort_id,
                                started_at=evaluation_start,
                                evaluation_dataset_fingerprint=evaluation_fingerprint,
                            )
                        )
                        registered_ids.append(hypothesis_id)
                    candidate_marker = PatternDiscoveryCompleted(
                        evaluation_cohort_id=cohort.cohort_id,
                        manifest_sha256=cohort.manifest.manifest_sha256,
                        formation_dataset_fingerprint=discovery_result.formation_dataset_fingerprint,
                        evaluation_dataset_fingerprint=evaluation_fingerprint,
                        started_event_id=started.event_id,
                        formation_cutoff=formation_cutoff,
                        evaluation_start_not_before=evaluation_start,
                        selected_hypothesis_ids=tuple(registered_ids),
                        selected_count=len(registered_ids),
                    )
                    self.lifecycle.append_discovery_completed(candidate_marker)
                    committed_marker = self.lifecycle.get_discovery_completed(cohort.cohort_id, started.event_id)
                    if committed_marker is None:
                        raise RuntimeError("discovery lifecycle marker is not durable after append")
                    if committed_marker != candidate_marker:
                        raise ValueError("discovery lifecycle marker changed during durable append")
                    marker = committed_marker
                    selected_ids = marker.selected_hypothesis_ids
                    stages.append(PatternShadowStageResult("discovery", "completed", reason="discovered"))
            if marker is not None and existing_marker is not None:
                stages.append(PatternShadowStageResult("discovery", "completed", reason="replayed"))
        except Exception as exc:  # noqa: BLE001 - discovery cannot affect Trader
            stages.append(PatternShadowStageResult("discovery", "failed", error=_error(exc)))

        persisted_prediction_count = 0
        persisted_occurrence_count = 0
        evaluation_considered_records = 0
        evaluation_eligible_records = 0
        evaluation_rejection_counts: dict[str, int] = {}
        if marker is None:
            stages.append(PatternShadowStageResult("evaluation", "skipped", reason="discovery_not_completed"))
        elif not selected_ids:
            stages.append(PatternShadowStageResult("evaluation", "skipped", reason="no_selected_hypotheses"))
        elif resolved_as_of <= evaluation_start:
            stages.append(PatternShadowStageResult("evaluation", "skipped", reason="evaluation_window_not_started"))
        else:
            try:
                evaluation_result = self.evaluation.evaluate(
                    PatternEvaluationRequest(
                        as_of=resolved_as_of,
                        evaluation_cohort_id=cohort.cohort_id,
                        evaluation_dataset_fingerprint=evaluation_fingerprint,
                        hypothesis_ids=selected_ids,
                    )
                )
                evaluation_considered_records = evaluation_result.considered_records
                evaluation_eligible_records = evaluation_result.eligible_records
                evaluation_rejection_counts = dict(evaluation_result.rejection_counts)
                persisted = self.evaluation.persist(
                    evaluation_result,
                    patterns=self.patterns,
                    predictions=self.predictions,
                )
                persisted_prediction_count = len(persisted.persisted_prediction_ids)
                persisted_occurrence_count = len(persisted.persisted_occurrence_ids)
                if evaluation_considered_records == 0:
                    eval_status: PatternShadowStageStatus = "completed"
                    eval_reason = "no_new_data"
                elif evaluation_eligible_records == 0:
                    eval_status = "skipped"
                    eval_reason = "all_records_incompatible"
                else:
                    eval_status = "completed"
                    eval_reason = None
                stages.append(PatternShadowStageResult("evaluation", eval_status, reason=eval_reason))
            except Exception as exc:  # noqa: BLE001 - evaluation cannot affect Trader
                stages.append(PatternShadowStageResult("evaluation", "failed", error=_error(exc)))

        linked_outcome_count = 0
        try:
            linked = self.outcomes.link(
                PatternOutcomeLinkRequest(
                    as_of=resolved_as_of,
                    evaluation_cohort_id=cohort.cohort_id,
                    hypothesis_ids=selected_ids,
                ),
                patterns=self.patterns,
            )
            linked_outcome_count = linked.linked_count
            stages.append(PatternShadowStageResult("outcome_link", "completed"))
        except Exception as exc:  # noqa: BLE001 - outcome linking cannot affect Trader
            stages.append(PatternShadowStageResult("outcome_link", "failed", error=_error(exc)))

        incompatible = any(
            item.stage == "evaluation" and item.reason == "all_records_incompatible" for item in stages
        )
        empty_at_cutoff = any(
            item.stage == "discovery" and item.reason == "no_ripe_exact_records_at_formation_cutoff"
            for item in stages
        )
        status: PatternShadowWorkflowStatus = (
            "partial"
            if incompatible or empty_at_cutoff or any(item.status == "failed" for item in stages)
            else "completed"
        )
        return PatternShadowWorkflowResult(
            resolved_as_of,
            status,
            tuple(stages),
            evaluation_cohort_id=cohort.cohort_id,
            formation_cutoff=formation_cutoff,
            evaluation_start_not_before=evaluation_start,
            evaluation_dataset_fingerprint=evaluation_fingerprint,
            selected_hypothesis_ids=selected_ids,
            replayed=replayed,
            discovered_count=discovered_count,
            persisted_prediction_count=persisted_prediction_count,
            persisted_occurrence_count=persisted_occurrence_count,
            linked_outcome_count=linked_outcome_count,
            evaluation_considered_records=evaluation_considered_records,
            evaluation_eligible_records=evaluation_eligible_records,
            evaluation_rejection_counts=evaluation_rejection_counts,
        )


__all__ = [
    "PATTERN_EVALUATION_DATASET_SCHEMA",
    "PatternDiscoveryPort",
    "PatternEvaluationPort",
    "PatternOutcomeLinkPort",
    "PatternShadowStageResult",
    "PatternShadowWorkflow",
    "PatternShadowWorkflowResult",
    "next_canonical_bar_boundary",
    "pattern_evaluation_dataset_fingerprint",
]
