"""Automatic, fail-open lifecycle for prospective graph-pattern evaluation.

This application service coordinates the already separated discovery,
prospective matching, persistence, and outcome-linking use cases.  It has no
runtime, infrastructure, CLI, trading, or broker dependency.  Formation may
read historical labels through its own port; prospective evaluation remains
unlabelled and the outcome service is invoked last.

The pilot never closes or archives hypotheses. ``PatternEvaluationClosed``
stays a domain event for a later operator-owned end-of-life; this workflow
does not emit it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    RegisterPatternHypothesis,
    StartPatternEvaluation,
    WorldPatternService,
)
from trader.domain.world_availability import AvailabilityEvidence
from trader.domain.world_cohort import WorldCohort
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
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "formation_cutoff", formation)
        object.__setattr__(self, "evaluation_start_not_before", evaluation_start)
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "selected_hypothesis_ids", tuple(self.selected_hypothesis_ids))
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

    def run(self, as_of: datetime | str) -> PatternShadowWorkflowResult:
        resolved_as_of = parse_utc_timestamp(as_of, "as_of")
        stages: list[PatternShadowStageResult] = []
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
                    )
                )
                discovered_count = len(discovery_result.candidates)
                registered_ids: list[str] = []
                for candidate in discovery_result.candidates:
                    spec = candidate.spec
                    if (
                        spec.formation_cutoff != formation_cutoff
                        or spec.evaluation_start_not_before != evaluation_start
                    ):
                        raise ValueError("discovery candidate window does not match the proven cohort start")
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
            stages.append(
                PatternShadowStageResult("discovery", "completed", reason="replayed" if replayed else "discovered")
            )
        except Exception as exc:  # noqa: BLE001 - discovery cannot affect Trader
            stages.append(PatternShadowStageResult("discovery", "failed", error=_error(exc)))

        persisted_prediction_count = 0
        persisted_occurrence_count = 0
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
                persisted = self.evaluation.persist(
                    evaluation_result,
                    patterns=self.patterns,
                    predictions=self.predictions,
                )
                persisted_prediction_count = len(persisted.persisted_prediction_ids)
                persisted_occurrence_count = len(persisted.persisted_occurrence_ids)
                stages.append(PatternShadowStageResult("evaluation", "completed"))
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

        status: PatternShadowWorkflowStatus = (
            "partial" if any(item.status == "failed" for item in stages) else "completed"
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
