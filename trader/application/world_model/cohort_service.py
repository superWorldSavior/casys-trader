"""Typed command handlers for the prospective World cohort aggregate.

The use case loads a reconstructed ``WorldCohort``, asks it to arm/start/admit
or otherwise transition, then persists the domain event. Status is never assigned
as a free string: config drift is ``WorldCohortLaneBlocked``. Infrastructure,
runtime, and reporting stay behind the consumer-owned ports. Models are minted
cold after a proven ``WorldCohortStarted``; missed bars are never backfilled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.application.world_model.cohort_ports import WorldCohortQuery, WorldCohortRepository
from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    AdmitWorldCohortSlot,
    ArmWorldCohort,
    BlockWorldCohortLane,
    CloseWorldCohort,
    CompleteWorldCohort,
    InvalidateWorldCohort,
    LaneBlockReason,
    ModelFamily,
    RegisterWorldCohort,
    RestoreWorldCohortLane,
    StartWorldCohort,
    WORLD_COHORT_COMMANDS,
    WorldCohort,
    WorldCohortCommand,
    WorldCohortEvent,
    WorldCohortEventEnvelope,
    WorldCohortId,
    WorldCohortManifest,
    WorldCohortRegistered,
    WorldCohortSlot,
    WorldCohortStarted,
    WorldLaneDefinition,
)


def _require_command(command: object, expected: type) -> None:
    if not isinstance(command, expected):
        raise TypeError(f"{expected.__name__} is required")


def _cohort_id(value: WorldCohortId | str) -> WorldCohortId:
    return value if isinstance(value, WorldCohortId) else WorldCohortId(value)


@dataclass(frozen=True)
class WorldCohortColdLane:
    """Identity of a lane model that starts at zero after ``WorldCohortStarted``."""

    lane_id: str
    model_family: ModelFamily
    model_id: str
    model_version: str
    seed: int
    sequence_length: int | None
    feature_contract_id: str
    feature_contract_fingerprint: str
    feature_mask_id: str
    feature_mask_fingerprint: str
    hyperparameters_sha256: str
    study_cohort_id: str
    manifest_sha256: str
    started_event_id: str
    replay_bound_event_id: str
    trained_through: str | None = None
    prior_training_lineage: tuple[str, ...] = ()
    authority: str = COHORT_AUTHORITY
    decision_effect: str = COHORT_DECISION_EFFECT
    recommendation: str = COHORT_RECOMMENDATION

    def __post_init__(self) -> None:
        if self.trained_through is not None:
            raise ValueError("cold lane must not carry prior training")
        if self.prior_training_lineage:
            raise ValueError("cold lane must not reuse a warm lineage")
        if self.authority != COHORT_AUTHORITY:
            raise ValueError("cold lane authority must be shadow_only")
        if self.decision_effect != COHORT_DECISION_EFFECT:
            raise ValueError("cold lane decision_effect must be none")
        if self.recommendation != COHORT_RECOMMENDATION:
            raise ValueError("cold lane recommendation must be NO_GO")
        if self.replay_bound_event_id != self.started_event_id:
            raise ValueError("cold lane replay must be bound to WorldCohortStarted")


class WorldCohortColdLaneFactory(Protocol):
    def __call__(
        self,
        lane: WorldLaneDefinition,
        *,
        started: WorldCohortStarted,
        manifest: WorldCohortManifest,
    ) -> WorldCohortColdLane: ...


def create_cold_lane(
    lane: WorldLaneDefinition,
    *,
    started: WorldCohortStarted,
    manifest: WorldCohortManifest,
) -> WorldCohortColdLane:
    return WorldCohortColdLane(
        lane_id=lane.lane_id,
        model_family=lane.model_family,
        model_id=lane.model_id,
        model_version=lane.model_version,
        seed=lane.seed,
        sequence_length=lane.sequence_length,
        feature_contract_id=lane.feature_contract_id,
        feature_contract_fingerprint=lane.feature_contract_fingerprint,
        feature_mask_id=lane.feature_mask_id,
        feature_mask_fingerprint=lane.feature_mask_fingerprint,
        hyperparameters_sha256=lane.hyperparameters_sha256,
        study_cohort_id=manifest.cohort_id,
        manifest_sha256=manifest.manifest_sha256,
        started_event_id=started.event_id,
        replay_bound_event_id=started.event_id,
    )


def build_cold_lanes(cohort: WorldCohort) -> tuple[WorldCohortColdLane, ...]:
    started = cohort.started_event
    if started is None:
        raise ValueError("cold factories require a proven start")
    return tuple(create_cold_lane(lane, started=started, manifest=cohort.manifest) for lane in cohort.manifest.lanes)


def _reject_warm_lane(item: WorldCohortColdLane, *, started: WorldCohortStarted) -> WorldCohortColdLane:
    if item.trained_through is not None or item.prior_training_lineage:
        raise ValueError("cold factory must not reuse a warm model")
    if item.started_event_id != started.event_id or item.replay_bound_event_id != started.event_id:
        raise ValueError("cold factory replay must be bound to WorldCohortStarted")
    return item


@dataclass(frozen=True)
class WorldCohortService:
    repository: WorldCohortRepository
    query: WorldCohortQuery
    cold_lane_factory: WorldCohortColdLaneFactory = create_cold_lane

    def handle(
        self,
        command: WorldCohortCommand,
        *,
        cohort_id: WorldCohortId | str | None = None,
    ) -> WorldCohortEventEnvelope:
        if not isinstance(command, WORLD_COHORT_COMMANDS):
            raise TypeError(f"unsupported command: {type(command).__name__}")
        if isinstance(command, RegisterWorldCohort):
            return self.register(command)
        if isinstance(command, ArmWorldCohort):
            return self.arm(command)
        if isinstance(command, StartWorldCohort):
            return self.start(command)
        if isinstance(command, AdmitWorldCohortSlot):
            return self.admit_slot(command)
        if cohort_id is None:
            raise TypeError(f"{type(command).__name__} requires cohort_id")
        if isinstance(command, BlockWorldCohortLane):
            return self.block_lane(cohort_id, command)
        if isinstance(command, RestoreWorldCohortLane):
            return self.restore_lane(cohort_id, command)
        if isinstance(command, CloseWorldCohort):
            return self.close(cohort_id, command)
        if isinstance(command, CompleteWorldCohort):
            return self.complete(cohort_id, command)
        if isinstance(command, InvalidateWorldCohort):
            return self.invalidate(cohort_id, command)
        raise TypeError(f"unsupported command: {type(command).__name__}")

    def register(self, command: RegisterWorldCohort) -> WorldCohortEventEnvelope:
        _require_command(command, RegisterWorldCohort)
        existing = self._try_load(WorldCohortId(command.manifest.cohort_id))
        cohort = WorldCohort.register(command, existing=existing)
        event = cohort.events[0]
        if not isinstance(event, WorldCohortRegistered):
            raise TypeError("register must persist WorldCohortRegistered")
        return self.repository.register(cohort.manifest, event)

    def arm(self, command: ArmWorldCohort) -> WorldCohortEventEnvelope:
        _require_command(command, ArmWorldCohort)
        return self._apply(self.repository.load(WorldCohortId(command.cohort_id)), command)

    def start(self, command: StartWorldCohort) -> WorldCohortEventEnvelope:
        _require_command(command, StartWorldCohort)
        return self._apply(self.repository.load(WorldCohortId(command.cohort_id)), command)

    def admit_slot(self, command: AdmitWorldCohortSlot) -> WorldCohortEventEnvelope:
        _require_command(command, AdmitWorldCohortSlot)
        cohort = self.repository.load(WorldCohortId(command.slot.cohort_id))
        started = cohort.started_event
        if started is None:
            raise ValueError("slots can only be admitted while collecting")
        evidence = self.query.envelope_for(started).require_proven()
        if command.started_evidence != evidence:
            raise ValueError("started_evidence must be the store-attested start envelope evidence")
        return self._apply(cohort, AdmitWorldCohortSlot(slot=command.slot, started_evidence=evidence))

    def block_lane(self, cohort_id: WorldCohortId | str, command: BlockWorldCohortLane) -> WorldCohortEventEnvelope:
        _require_command(command, BlockWorldCohortLane)
        return self._apply(self.repository.load(_cohort_id(cohort_id)), command)

    def restore_lane(self, cohort_id: WorldCohortId | str, command: RestoreWorldCohortLane) -> WorldCohortEventEnvelope:
        _require_command(command, RestoreWorldCohortLane)
        return self._apply(self.repository.load(_cohort_id(cohort_id)), command)

    def close(self, cohort_id: WorldCohortId | str, command: CloseWorldCohort) -> WorldCohortEventEnvelope:
        _require_command(command, CloseWorldCohort)
        return self._apply(self.repository.load(_cohort_id(cohort_id)), command)

    def complete(self, cohort_id: WorldCohortId | str, command: CompleteWorldCohort) -> WorldCohortEventEnvelope:
        _require_command(command, CompleteWorldCohort)
        return self._apply(self.repository.load(_cohort_id(cohort_id)), command)

    def invalidate(self, cohort_id: WorldCohortId | str, command: InvalidateWorldCohort) -> WorldCohortEventEnvelope:
        _require_command(command, InvalidateWorldCohort)
        return self._apply(self.repository.load(_cohort_id(cohort_id)), command)

    def record_config_drift(
        self,
        cohort_id: WorldCohortId | str,
        *,
        lane_id: str,
        affected_from: datetime | None = None,
        affected_until: datetime | None = None,
    ) -> WorldCohortEventEnvelope:
        return self.block_lane(
            cohort_id,
            BlockWorldCohortLane(
                lane_id=lane_id,
                reason=LaneBlockReason.CONFIG_DRIFT,
                affected_from=affected_from,
                affected_until=affected_until,
            ),
        )

    def cold_lanes(self, cohort_id: WorldCohortId | str) -> tuple[WorldCohortColdLane, ...]:
        cohort = self.repository.load(_cohort_id(cohort_id))
        started = cohort.started_event
        if started is None:
            raise ValueError("cold factories require a proven start")
        self.query.envelope_for(started).require_proven()
        return tuple(
            _reject_warm_lane(
                self.cold_lane_factory(lane, started=started, manifest=cohort.manifest),
                started=started,
            )
            for lane in cohort.manifest.lanes
        )

    def list_slots(self, cohort_id: WorldCohortId | str) -> tuple[WorldCohortSlot, ...]:
        return self.query.list_slots(_cohort_id(cohort_id))

    def _try_load(self, cohort_id: WorldCohortId) -> WorldCohort | None:
        try:
            return self.repository.load(cohort_id)
        except LookupError:
            return None

    def _apply(self, cohort: WorldCohort, command: WorldCohortCommand) -> WorldCohortEventEnvelope:
        next_cohort = cohort.handle(command)
        event: WorldCohortEvent = next_cohort.events[-1]
        return self.repository.append_event(event)


__all__ = [
    "WorldCohortColdLane",
    "WorldCohortColdLaneFactory",
    "WorldCohortService",
    "build_cold_lanes",
    "create_cold_lane",
]
