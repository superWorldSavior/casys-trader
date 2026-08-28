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
from trader.domain.world_cohort import (
    AdmitWorldCohortSlot,
    ArmWorldCohort,
    BlockWorldCohortLane,
    CloseWorldCohort,
    CohortPhase,
    CompleteWorldCohort,
    InvalidateWorldCohort,
    InvalidationReason,
    LaneBlockReason,
    LaneOperationalStatus,
    RegisterWorldCohort,
    RestoreWorldCohortLane,
    StartWorldCohort,
    WORLD_COHORT_COMMANDS,
    WorldCohort,
    WorldCohortCollectionClosed,
    WorldCohortCommand,
    WorldCohortCompletionEvidence,
    WorldCohortCompletionHorizonLeaf,
    WorldCohortCompletionSlotEvidence,
    WorldCohortEvent,
    WorldCohortEventEnvelope,
    WorldCohortId,
    WorldCohortLaneBlocked,
    WorldCohortManifest,
    WorldCohortRegistered,
    WorldCohortSlot,
    WorldCohortStarted,
    WorldContrastDefinition,
    WorldContrastTerm,
    WorldLaneDefinition,
    WorldRuntimeIdentity,
    WorldSensorRequirement,
    WorldStatisticalProtocol,
    WorldSupportGates,
    world_cohort_event_payload_hash,
)
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_feature_contract import (
    WorldFeatureMask,
    market_feature_contract,
    context_feature_contract,
)


UTC = timezone.utc
CREATED = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
PLANNED_START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
STOP_AT = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
START_READY = datetime(2026, 8, 24, 0, 5, tzinfo=UTC)
START_SEEN = datetime(2026, 8, 24, 0, 6, tzinfo=UTC)
ANCHOR_TS = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
LATER_TS = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
GIT = "a" * 40
COHORT_ID = "world_cohort:v1:" + "c" * 64
STORE_ID = "world-cohort-jsonl.v1"
_PORTS_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "cohort_ports.py"
_SERVICE_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "cohort_service.py"

V1 = market_feature_contract()
V2 = context_feature_contract()
MARKET_MASK = WorldFeatureMask.bind(V1, mask_id="market.v1", selected_groups=("market",))
STATUS_MASK = WorldFeatureMask.bind(V2, mask_id="status_only.v1", selected_groups=("market", "status"))
COMPANY_MASK = WorldFeatureMask.bind(V2, mask_id="company.v1", selected_groups=("market", "status", "company"))


def _runtime(**overrides: object) -> WorldRuntimeIdentity:
    values: dict[str, object] = {
        "git_commit": GIT,
        "python_version": "3.11.9",
        "numpy_version": "1.26.4",
        "application_build_id": "casys-trader.world.20260823",
    }
    values.update(overrides)
    return WorldRuntimeIdentity(**values)  # type: ignore[arg-type]


def _lane(
    lane_id: str,
    *,
    contract=V1,
    mask=MARKET_MASK,
    role: str = "primary_control",
) -> WorldLaneDefinition:
    return WorldLaneDefinition(
        lane_id=lane_id,
        model_family="markov",
        model_id="hierarchical_dirichlet_world_baseline",
        model_version=f"cohort.{lane_id}.v1",
        feature_contract_id=contract.contract_id,
        feature_contract_fingerprint=contract.fingerprint,
        feature_mask_id=mask.mask_id,
        feature_mask_fingerprint=mask.fingerprint,
        seed=0,
        sequence_length=None,
        hyperparameters_sha256=canonical_sha256({"family": "markov", "lane": lane_id}),
        role=role,
    )


def _pilot_lanes() -> tuple[WorldLaneDefinition, ...]:
    return (
        _lane("markov.market", contract=V1, mask=MARKET_MASK, role="primary_control"),
        _lane("markov.status_only", contract=V2, mask=STATUS_MASK, role="process_control"),
        _lane("markov.company", contract=V2, mask=COMPANY_MASK, role="pilot_treatment"),
    )


def _pilot_contrasts() -> tuple[WorldContrastDefinition, ...]:
    return (
        WorldContrastDefinition(
            contrast_id="markov.status_only_minus_market.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.status_only", coefficient=1),
                WorldContrastTerm(lane_id="markov.market", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="pipeline_control",
        ),
        WorldContrastDefinition(
            contrast_id="markov.company_minus_status_only.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.company", coefficient=1),
                WorldContrastTerm(lane_id="markov.status_only", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="pilot_treatment",
        ),
    )


def _manifest(**overrides: object) -> WorldCohortManifest:
    values: dict[str, object] = {
        "cohort_id": COHORT_ID,
        "study_kind": "pipeline_pilot",
        "created_at": CREATED,
        "question": "Can paired lanes be captured without causal violations?",
        "planned_start_not_before": PLANNED_START,
        "collection_stop_rule": {"kind": "fixed_end", "at": STOP_AT},
        "venues": ("EU", "TW", "US"),
        "bar_interval": "1h",
        "horizons": ("elapsed_4h.v1", "elapsed_1d.v1"),
        "primary_horizon": "elapsed_1d.v1",
        "label_contract": "simple_return_band_50bp.v1",
        "sampling_policy_version": "active_tradable_completed_bar.v1",
        "market_feature_contract": V1.contract_id,
        "context_feature_contract": V2.contract_id,
        "ontology_revision": "semantic_catalog.v1",
        "scope_mapping": None,
        "sensor_requirements": (
            WorldSensorRequirement(
                sensor_id="company",
                source_contract_id="company_intelligence_brief.v1",
                projection_contract_id="company_context_projection.v1",
                mode="required",
                lane_ids=("markov.company",),
            ),
        ),
        "lanes": _pilot_lanes(),
        "contrasts": _pilot_contrasts(),
        "statistical_protocol": WorldStatisticalProtocol(
            pair_unit="unique_market_anchor",
            block_key="venue_session",
            ci_method="deterministic_block_bootstrap.v1",
            ci_level=0.95,
            bootstrap_resamples=2000,
            seed=20260823,
        ),
        "support_gates": WorldSupportGates(
            mode="descriptive_only",
            descriptive_minimum_unique_anchors=20,
            formal_minimum_unique_anchors=None,
        ),
        "runtime_identity": _runtime(),
        "authority": "shadow_only",
        "decision_effect": "none",
        "causal_claim": False,
        "pnl_claim": False,
    }
    values.update(overrides)
    return WorldCohortManifest(**values)  # type: ignore[arg-type]


def _attested_receipt(*, subject: WorldAvailabilitySubjectRef, ready: datetime, scope: str) -> WorldAvailabilityReceipt:
    locator = WorldStorageLocator(kind="jsonl", store_id=STORE_ID, path="cohort/events.jsonl")
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


def _evidence_for(
    event: Any, *, ready: datetime = START_READY, first_seen: datetime = START_SEEN
) -> AvailabilityEvidence:
    subject = WorldAvailabilitySubjectRef(
        kind="world_cohort_event",
        subject_id=event.event_id,
        content_sha256=world_cohort_event_payload_hash(event),
    )
    receipt = _attested_receipt(subject=subject, ready=ready, scope=f"world_cohort:{event.cohort_id}")
    return AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)


class _MemoryWorldCohortStore:
    """In-memory consumer of the application ports. Store-assigns receipts; tests own the clock."""

    def __init__(
        self,
        *,
        crash_before_receipt: int = 0,
        crash_event_types: frozenset[str] | None = None,
    ) -> None:
        self.crash_before_receipt = crash_before_receipt
        self.crash_event_types = crash_event_types
        self.before_insert = None
        self.manifests: dict[str, WorldCohortManifest] = {}
        self.events: dict[str, list[WorldCohortEvent]] = {}
        self.envelopes: dict[str, WorldCohortEventEnvelope] = {}
        self.sequences: dict[str, int] = {}

    def register(self, manifest: WorldCohortManifest, event: WorldCohortRegistered) -> WorldCohortEventEnvelope:
        existing = self.manifests.get(manifest.cohort_id)
        if existing is not None:
            if existing.manifest_sha256 != manifest.manifest_sha256:
                raise ValueError("cohort_id reused with a different hash")
            stored = self.events[manifest.cohort_id]
            if not stored or stored[0].event_id != event.event_id:
                raise ValueError("registered event does not match the durable manifest")
            return self._seal(event)
        self.manifests[manifest.cohort_id] = manifest
        self.events[manifest.cohort_id] = [event]
        return self._seal(event)

    def append_event(self, event: WorldCohortEvent, *, expected_sequence: int) -> WorldCohortEventEnvelope:
        if not isinstance(expected_sequence, int) or isinstance(expected_sequence, bool) or expected_sequence < 0:
            raise ValueError("expected_sequence must be a non-negative int")
        stored = self.events.get(event.cohort_id)
        if stored is None:
            raise LookupError(event.cohort_id)
        existing = self.envelopes.get(event.event_id)
        if existing is not None:
            if world_cohort_event_payload_hash(existing.event) != world_cohort_event_payload_hash(event):
                raise ValueError("event_id reused with a different payload")
            return self._seal(event)
        if self.before_insert is not None:
            hook = self.before_insert
            self.before_insert = None
            hook()
            stored = self.events[event.cohort_id]
        if len(stored) != expected_sequence:
            raise ValueError("conflicting sequence")
        stored.append(event)
        return self._seal(event)

    def load(self, cohort_id: WorldCohortId) -> WorldCohort:
        key = cohort_id.value
        if key not in self.manifests:
            raise LookupError(key)
        return WorldCohort.reconstruct(self.manifests[key], self.events[key])

    def list_slots(self, cohort_id: WorldCohortId) -> tuple[WorldCohortSlot, ...]:
        return self.load(cohort_id).admitted_slots

    def list_collecting_cohorts(self) -> tuple[WorldCohort, ...]:
        collecting: list[WorldCohort] = []
        for cohort_id in sorted(self.manifests):
            try:
                cohort = self.load(WorldCohortId(cohort_id))
            except (LookupError, TypeError, ValueError):
                continue
            if cohort.phase is CohortPhase.COLLECTING and cohort.started_event is not None:
                collecting.append(cohort)
        return tuple(collecting)

    def list_live_cohorts(self) -> tuple[WorldCohort, ...]:
        live: list[WorldCohort] = []
        for cohort_id in sorted(self.manifests):
            try:
                cohort = self.load(WorldCohortId(cohort_id))
            except (LookupError, TypeError, ValueError):
                continue
            if cohort.phase in {CohortPhase.REGISTERED, CohortPhase.ARMED, CohortPhase.COLLECTING}:
                live.append(cohort)
        return tuple(live)

    def envelope_for(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope:
        envelope = self.envelopes.get(event.event_id)
        if envelope is None:
            raise LookupError(event.event_id)
        return envelope

    def _seal(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope:
        current = self.envelopes.get(event.event_id)
        if current is None:
            self.sequences[event.cohort_id] = self.sequences.get(event.cohort_id, 0) + 1
            current = WorldCohortEventEnvelope.bind(
                event,
                sequence=self.sequences[event.cohort_id],
                evidence=None,
            )
            self.envelopes[event.event_id] = current
        if current.evidence is not None:
            return current
        crash_applies = self.crash_event_types is None or event.event_type in self.crash_event_types
        if crash_applies and self.crash_before_receipt > 0:
            self.crash_before_receipt -= 1
            return current
        sealed = WorldCohortEventEnvelope.bind(
            event,
            sequence=current.sequence,
            evidence=_evidence_for(event),
        )
        self.envelopes[event.event_id] = sealed
        return sealed


def _service(*, crash_before_receipt: int = 0, crash_event_types: frozenset[str] | None = None, factory=None):
    from trader.application.world_model.cohort_service import WorldCohortService

    store = _MemoryWorldCohortStore(
        crash_before_receipt=crash_before_receipt,
        crash_event_types=crash_event_types,
    )
    kwargs: dict[str, object] = {"repository": store, "query": store}
    if factory is not None:
        kwargs["cold_lane_factory"] = factory
    return WorldCohortService(**kwargs), store  # type: ignore[arg-type]


def _arm_command(manifest: WorldCohortManifest, *, sensors: tuple[str, ...] = ("company",)) -> ArmWorldCohort:
    return ArmWorldCohort(
        cohort_id=manifest.cohort_id,
        manifest_sha256=manifest.manifest_sha256,
        runtime_identity=manifest.runtime_identity,
        satisfied_sensor_ids=sensors,
    )


def _start_command(manifest: WorldCohortManifest) -> StartWorldCohort:
    return StartWorldCohort(
        cohort_id=manifest.cohort_id,
        manifest_sha256=manifest.manifest_sha256,
        runtime_identity=manifest.runtime_identity,
    )


def _slot_for(cohort: WorldCohort, **overrides: object) -> WorldCohortSlot:
    started = cohort.started_event
    values: dict[str, object] = {
        "cohort_id": cohort.cohort_id,
        "manifest_sha256": cohort.manifest.manifest_sha256,
        "venue": "US",
        "symbol": "AAPL",
        "bar_interval": "1h",
        "as_of_bar_ts": ANCHOR_TS,
        "anchor_end_at": ANCHOR_TS,
        "comparison_batch_id": "batch:v1:anchor-aapl",
        "episode_refs_by_contract": {
            V1.contract_id: "world-episode:v1:" + "1" * 64,
            V2.contract_id: "world-episode:v1:" + "2" * 64,
        },
        "expected_lane_ids": tuple(lane.lane_id for lane in cohort.manifest.lanes),
        "feature_contract_fingerprints": {
            lane.lane_id: lane.feature_contract_fingerprint for lane in cohort.manifest.lanes
        },
        "feature_mask_fingerprints": {lane.lane_id: lane.feature_mask_fingerprint for lane in cohort.manifest.lanes},
        "scope_resolution": None,
        "started_event_id": None if started is None else started.event_id,
    }
    values.update(overrides)
    return WorldCohortSlot(**values)  # type: ignore[arg-type]


def _admit_command(
    cohort: WorldCohort, store: _MemoryWorldCohortStore, **slot_overrides: object
) -> AdmitWorldCohortSlot:
    started = cohort.started_event
    assert started is not None
    envelope = store.envelope_for(started)
    return AdmitWorldCohortSlot(slot=_slot_for(cohort, **slot_overrides), started_evidence=envelope.require_proven())


def _msft_slot(cohort: WorldCohort) -> WorldCohortSlot:
    return _slot_for(
        cohort,
        as_of_bar_ts=LATER_TS,
        anchor_end_at=LATER_TS,
        symbol="MSFT",
        comparison_batch_id="batch:v1:anchor-msft",
        episode_refs_by_contract={
            V1.contract_id: "world-episode:v1:" + "3" * 64,
            V2.contract_id: "world-episode:v1:" + "4" * 64,
        },
    )


def _block_company() -> BlockWorldCohortLane:
    return BlockWorldCohortLane(
        lane_id="markov.company",
        reason=LaneBlockReason.CONFIG_DRIFT,
        affected_from=LATER_TS,
    )


def _completion_evidence(cohort: WorldCohort) -> WorldCohortCompletionEvidence:
    return WorldCohortCompletionEvidence(
        slots=tuple(
            WorldCohortCompletionSlotEvidence(
                slot_id=slot.slot_id,
                leaves=(
                    WorldCohortCompletionHorizonLeaf(
                        horizon_id="elapsed_4h.v1",
                        status="observed",
                        digest="4" * 64,
                    ),
                    WorldCohortCompletionHorizonLeaf(
                        horizon_id="elapsed_1d.v1",
                        status="observed",
                        digest="d" * 64,
                    ),
                ),
            )
            for slot in cohort.admitted_slots
        )
    )


def _collecting(service, store, manifest: WorldCohortManifest | None = None):
    current = manifest or _manifest()
    service.register(RegisterWorldCohort(manifest=current))
    service.arm(_arm_command(current))
    service.start(_start_command(current))
    return store.load(WorldCohortId(current.cohort_id))


def test_memory_store_lists_collecting_cohorts_only() -> None:
    service, store = _service()
    registered = service.register(RegisterWorldCohort(manifest=_manifest()))
    assert store.list_collecting_cohorts() == ()
    live_registered = store.list_live_cohorts()
    assert [item.cohort_id for item in live_registered] == [registered.event.cohort_id]
    collecting = _collecting(service, store)
    listed = store.list_collecting_cohorts()
    assert [item.cohort_id for item in listed] == [collecting.cohort_id]
    assert listed[0].phase is CohortPhase.COLLECTING
    assert [item.cohort_id for item in store.list_live_cohorts()] == [collecting.cohort_id]


def test_ports_are_consumer_owned_typed_contracts() -> None:
    from trader.application.world_model.cohort_ports import WorldCohortQuery, WorldCohortRepository

    register_hints = get_type_hints(WorldCohortRepository.register)
    assert list(inspect.signature(WorldCohortRepository.register).parameters) == ["self", "manifest", "event"]
    assert register_hints["manifest"] is WorldCohortManifest
    assert register_hints["event"] is WorldCohortRegistered
    assert register_hints["return"] is WorldCohortEventEnvelope

    append_hints = get_type_hints(WorldCohortRepository.append_event)
    append_signature = inspect.signature(WorldCohortRepository.append_event)
    assert list(append_signature.parameters) == ["self", "event", "expected_sequence"]
    assert append_signature.parameters["expected_sequence"].kind is inspect.Parameter.KEYWORD_ONLY
    assert append_hints["event"] == WorldCohortEvent
    assert append_hints["expected_sequence"] is int
    assert append_hints["return"] is WorldCohortEventEnvelope

    load_hints = get_type_hints(WorldCohortRepository.load)
    assert list(inspect.signature(WorldCohortRepository.load).parameters) == ["self", "cohort_id"]
    assert load_hints["cohort_id"] is WorldCohortId
    assert load_hints["return"] is WorldCohort

    query_hints = get_type_hints(WorldCohortQuery.list_slots)
    assert list(inspect.signature(WorldCohortQuery.list_slots).parameters) == ["self", "cohort_id"]
    assert query_hints["cohort_id"] is WorldCohortId
    assert query_hints["return"] == tuple[WorldCohortSlot, ...]

    collecting_hints = get_type_hints(WorldCohortQuery.list_collecting_cohorts)
    assert list(inspect.signature(WorldCohortQuery.list_collecting_cohorts).parameters) == ["self"]
    assert collecting_hints["return"] == tuple[WorldCohort, ...]

    live_hints = get_type_hints(WorldCohortQuery.list_live_cohorts)
    assert list(inspect.signature(WorldCohortQuery.list_live_cohorts).parameters) == ["self"]
    assert live_hints["return"] == tuple[WorldCohort, ...]

    envelope_hints = get_type_hints(WorldCohortQuery.envelope_for)
    assert list(inspect.signature(WorldCohortQuery.envelope_for).parameters) == ["self", "event"]
    assert envelope_hints["event"] == WorldCohortEvent
    assert envelope_hints["return"] is WorldCohortEventEnvelope


def test_ports_and_service_reject_ready_at_untyped_mappings_and_layer_leaks() -> None:
    from trader.application.world_model import cohort_ports, cohort_service

    forbidden = ("trader.runtime", "trader.infrastructure", "trader.reporting")
    violations: list[str] = []
    for path in (_PORTS_PATH, _SERVICE_PATH):
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
        assert "set_status" not in text
        assert "def backfill" not in text
        assert "WorldModelLedger" not in text
        assert "list_eligible_episodes" not in text
        assert "NewsMacroBrief" not in text
    assert violations == []

    for module in (cohort_ports, cohort_service):
        source = inspect.getsource(module)
        assert "trader.infrastructure" not in source
        assert "trader.runtime" not in source
        assert "trader.reporting" not in source

    for name in ("WorldCohortRepository", "WorldCohortQuery"):
        cls = getattr(cohort_ports, name)
        for method in cls.__dict__.values():
            if not callable(method) or method.__name__.startswith("_"):
                continue
            signature = inspect.signature(method)
            assert "ready_at" not in signature.parameters
            assert "clock" not in signature.parameters
            for parameter in signature.parameters.values():
                assert parameter.annotation is not dict
                assert get_origin(parameter.annotation) is not dict

    from trader.application.world_model.cohort_service import WorldCohortService, build_cold_lanes

    assert "ready_at" not in inspect.signature(WorldCohortService).parameters
    assert "clock" not in inspect.signature(WorldCohortService).parameters
    assert not hasattr(WorldCohortService, "set_status")
    assert not hasattr(WorldCohortService, "backfill")
    assert "ready_at" not in inspect.signature(build_cold_lanes).parameters


def test_typed_handlers_are_the_only_mutation_surface() -> None:
    from trader.application.world_model.cohort_service import WorldCohortService

    expected = {
        "register": RegisterWorldCohort,
        "arm": ArmWorldCohort,
        "start": StartWorldCohort,
        "admit_slot": AdmitWorldCohortSlot,
        "block_lane": BlockWorldCohortLane,
        "restore_lane": RestoreWorldCohortLane,
        "close": CloseWorldCohort,
        "complete": CompleteWorldCohort,
        "invalidate": InvalidateWorldCohort,
    }
    for method_name, command_type in expected.items():
        method = getattr(WorldCohortService, method_name)
        hints = get_type_hints(method)
        assert command_type in hints.values()
    assert set(WORLD_COHORT_COMMANDS) == set(expected.values())
    service, _store = _service()
    with pytest.raises(TypeError):
        service.handle({"event_type": "world_cohort_registered"})  # type: ignore[arg-type]
    assert inspect.isclass(WorldCohortCommand) or WorldCohortCommand is not None


def test_register_crash_before_receipt_retries_idempotently_and_preserves_identity() -> None:
    manifest = _manifest()
    service, store = _service(crash_before_receipt=1)
    first = service.register(RegisterWorldCohort(manifest=manifest))
    assert first.event.event_type == "world_cohort_registered"
    assert first.availability_status == "availability_unproven"
    loaded = store.load(WorldCohortId(manifest.cohort_id))
    assert loaded.phase is CohortPhase.REGISTERED
    assert loaded.manifest.manifest_sha256 == manifest.manifest_sha256
    retry = service.register(RegisterWorldCohort(manifest=manifest))
    assert retry.event.event_id == first.event.event_id
    assert retry.payload_hash == first.payload_hash
    assert retry.availability_status == "eligible"
    again = service.register(RegisterWorldCohort(manifest=manifest))
    assert again.event.event_id == first.event.event_id
    other = _manifest(question="A different pre-registered question")
    assert other.cohort_id == manifest.cohort_id
    assert other.manifest_sha256 != manifest.manifest_sha256
    with pytest.raises(ValueError, match="hash"):
        service.register(RegisterWorldCohort(manifest=other))


def test_orchestration_goes_through_the_aggregate_and_keeps_shadow_authority() -> None:
    from trader.application.world_model.cohort_service import WorldCohortColdLane

    manifest = _manifest()
    service, store = _service()
    registered = service.handle(RegisterWorldCohort(manifest=manifest))
    assert isinstance(registered.event, WorldCohortRegistered)
    assert registered.availability_status == "eligible"
    armed = service.arm(_arm_command(manifest))
    assert armed.event.event_type == "world_cohort_armed"
    started = service.start(_start_command(manifest))
    assert isinstance(started.event, WorldCohortStarted)
    assert "ready_at" not in started.event.to_dict()
    cohort = store.load(WorldCohortId(manifest.cohort_id))
    assert cohort.phase is CohortPhase.COLLECTING
    assert cohort.manifest.authority == "shadow_only"
    assert cohort.manifest.decision_effect == "none"
    cold = service.cold_lanes(WorldCohortId(manifest.cohort_id))
    assert len(cold) == len(manifest.lanes)
    assert {item.lane_id for item in cold} == {lane.lane_id for lane in manifest.lanes}
    for item in cold:
        assert isinstance(item, WorldCohortColdLane)
        assert item.started_event_id == cohort.started_event.event_id
        assert item.replay_bound_event_id == cohort.started_event.event_id
        assert item.trained_through is None
        assert item.prior_training_lineage == ()
        assert item.authority == "shadow_only"
        assert item.decision_effect == "none"
        assert item.recommendation == "NO_GO"
    admitted = service.admit_slot(_admit_command(cohort, store))
    assert admitted.event.event_type == "world_cohort_slot_admitted"
    blocked = service.block_lane(
        WorldCohortId(manifest.cohort_id),
        BlockWorldCohortLane(
            lane_id="markov.company",
            reason=LaneBlockReason.CONFIG_DRIFT,
            affected_from=LATER_TS,
        ),
    )
    assert isinstance(blocked.event, WorldCohortLaneBlocked)
    assert blocked.event.state.reason is LaneBlockReason.CONFIG_DRIFT
    restored = service.restore_lane(
        WorldCohortId(manifest.cohort_id),
        RestoreWorldCohortLane(lane_id="markov.company"),
    )
    assert restored.event.event_type == "world_cohort_lane_restored"
    closed = service.close(WorldCohortId(manifest.cohort_id), CloseWorldCohort(reason="fixed_end reached"))
    assert isinstance(closed.event, WorldCohortCollectionClosed)
    closed_cohort = store.load(WorldCohortId(manifest.cohort_id))
    with pytest.raises(ValueError, match="admit|collection_closed|closed"):
        service.admit_slot(_admit_command(closed_cohort, store))
    completed = service.complete(
        WorldCohortId(manifest.cohort_id),
        CompleteWorldCohort(evidence=_completion_evidence(closed_cohort)),
    )
    assert completed.event.event_type == "world_cohort_completed"
    done = store.load(WorldCohortId(manifest.cohort_id))
    assert done.phase is CohortPhase.COMPLETE
    with pytest.raises(ValueError, match="complete|terminal"):
        service.invalidate(
            WorldCohortId(manifest.cohort_id),
            InvalidateWorldCohort(
                reason=InvalidationReason.FUTURE_LEAK,
                scope="cohort",
                proofs=("proof:future-leak",),
                occurred_at=LATER_TS,
            ),
        )


def test_start_is_idempotent_and_unproven_start_cannot_admit() -> None:
    manifest = _manifest()
    service, store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_started"}),
    )
    service.register(RegisterWorldCohort(manifest=manifest))
    service.arm(_arm_command(manifest))
    first = service.start(_start_command(manifest))
    assert first.availability_status == "availability_unproven"
    loaded = store.load(WorldCohortId(manifest.cohort_id))
    assert loaded.phase is CohortPhase.COLLECTING
    with pytest.raises(ValueError, match="availability_unproven|evidence"):
        service.admit_slot(
            AdmitWorldCohortSlot(
                slot=_slot_for(loaded),
                started_evidence=_evidence_for(loaded.started_event),
            )
        )
    retry = service.start(_start_command(manifest))
    assert retry.event.event_id == first.event.event_id
    assert retry.availability_status == "eligible"
    proven = store.load(WorldCohortId(manifest.cohort_id))
    admitted = service.admit_slot(_admit_command(proven, store))
    assert admitted.availability_status == "eligible"


def test_admit_uses_store_attested_start_evidence_and_rejects_pre_start_and_caller_clock() -> None:
    service, store = _service()
    cohort = _collecting(service, store)
    started = cohort.started_event
    assert started is not None
    store_envelope = store.envelope_for(started)
    assert store_envelope.require_proven().effective_ready_at == START_SEEN
    too_early = _slot_for(cohort, anchor_end_at=START_SEEN, as_of_bar_ts=START_SEEN)
    with pytest.raises(ValueError, match="strictly|after"):
        service.admit_slot(AdmitWorldCohortSlot(slot=too_early, started_evidence=store_envelope.require_proven()))
    minted = _evidence_for(
        started, ready=datetime(2026, 8, 23, 0, 0, tzinfo=UTC), first_seen=datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="store-attested|started_evidence|availability"):
        service.admit_slot(AdmitWorldCohortSlot(slot=_slot_for(cohort), started_evidence=minted))
    admitted = service.admit_slot(_admit_command(cohort, store))
    assert admitted.event.slot.anchor_end_at > store_envelope.require_proven().effective_ready_at
    same = service.admit_slot(_admit_command(store.load(WorldCohortId(cohort.cohort_id)), store))
    assert same.event.event_id == admitted.event.event_id


def test_zero_backfill_does_not_invent_missed_downtime_slots() -> None:
    service, store = _service()
    cohort = _collecting(service, store)
    first = service.admit_slot(_admit_command(cohort, store, as_of_bar_ts=ANCHOR_TS, anchor_end_at=ANCHOR_TS))
    later = service.admit_slot(
        _admit_command(
            store.load(WorldCohortId(cohort.cohort_id)),
            store,
            as_of_bar_ts=LATER_TS,
            anchor_end_at=LATER_TS,
            symbol="MSFT",
            comparison_batch_id="batch:v1:anchor-msft",
            episode_refs_by_contract={
                V1.contract_id: "world-episode:v1:" + "3" * 64,
                V2.contract_id: "world-episode:v1:" + "4" * 64,
            },
        )
    )
    slots = service.list_slots(WorldCohortId(cohort.cohort_id))
    assert tuple(slot.slot_id for slot in slots) == (first.event.slot.slot_id, later.event.slot.slot_id)
    gap = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
    assert all(slot.as_of_bar_ts != gap for slot in slots)
    assert not hasattr(service, "backfill")
    source = _SERVICE_PATH.read_text(encoding="utf-8")
    assert "def backfill" not in source
    assert "list_eligible_episodes" not in source


def test_cold_factories_require_proven_start_and_refuse_warm_models() -> None:
    from trader.application.world_model.cohort_service import WorldCohortColdLane, build_cold_lanes

    service, store = _service()
    manifest = _manifest()
    service.register(RegisterWorldCohort(manifest=manifest))
    with pytest.raises(ValueError, match="start"):
        service.cold_lanes(WorldCohortId(manifest.cohort_id))
    service.arm(_arm_command(manifest))
    armed = store.load(WorldCohortId(manifest.cohort_id))
    with pytest.raises(TypeError):
        build_cold_lanes(armed)
    with pytest.raises(ValueError, match="start"):
        build_cold_lanes(armed, started_envelope=store.envelope_for(armed.events[0]))
    unproven_service, unproven_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_started"}),
    )
    unproven_service.register(RegisterWorldCohort(manifest=manifest))
    unproven_service.arm(_arm_command(manifest))
    unproven_start = unproven_service.start(_start_command(manifest))
    assert unproven_start.availability_status == "availability_unproven"
    unproven_cohort = unproven_store.load(WorldCohortId(manifest.cohort_id))
    with pytest.raises(ValueError, match="availability_unproven|evidence"):
        build_cold_lanes(unproven_cohort, started_envelope=unproven_store.envelope_for(unproven_cohort.started_event))
    with pytest.raises(ValueError, match="availability_unproven|evidence"):
        unproven_service.cold_lanes(WorldCohortId(manifest.cohort_id))
    service.start(_start_command(manifest))
    cohort = store.load(WorldCohortId(manifest.cohort_id))
    proven = store.envelope_for(cohort.started_event)
    with pytest.raises(ValueError, match="started_envelope|WorldCohortStarted|prove"):
        build_cold_lanes(cohort, started_envelope=store.envelope_for(cohort.events[0]))
    built = build_cold_lanes(cohort, started_envelope=proven)
    assert built[0].study_cohort_id == cohort.cohort_id
    assert built[0].manifest_sha256 == cohort.manifest.manifest_sha256
    with pytest.raises((TypeError, ValueError)):
        WorldCohortColdLane(
            lane_id="markov.market",
            model_family="markov",
            model_id="hierarchical_dirichlet_world_baseline",
            model_version="warm.v1",
            seed=0,
            sequence_length=None,
            feature_contract_id=V1.contract_id,
            feature_contract_fingerprint=V1.fingerprint,
            feature_mask_id=MARKET_MASK.mask_id,
            feature_mask_fingerprint=MARKET_MASK.fingerprint,
            hyperparameters_sha256="a" * 64,
            study_cohort_id=cohort.cohort_id,
            manifest_sha256=cohort.manifest.manifest_sha256,
            started_event_id=cohort.started_event.event_id,
            replay_bound_event_id=cohort.started_event.event_id,
            trained_through="pre-start-lineage",
        )

    def warm_factory(lane, *, started, manifest):
        spec = build_cold_lanes(cohort, started_envelope=proven)[0]
        object.__setattr__(spec, "trained_through", "pre-start-lineage")
        return spec

    warm_service, _warm_store = _service()
    warm_service.register(RegisterWorldCohort(manifest=manifest))
    warm_service.arm(_arm_command(manifest))
    warm_service.start(_start_command(manifest))
    object.__setattr__(warm_service, "cold_lane_factory", warm_factory)
    with pytest.raises(ValueError, match="cold|warm|trained"):
        warm_service.cold_lanes(WorldCohortId(manifest.cohort_id))


def test_config_drift_is_a_typed_event_not_a_free_status() -> None:
    service, store = _service()
    cohort = _collecting(service, store)
    with pytest.raises(TypeError):
        service.block_lane(
            WorldCohortId(cohort.cohort_id),
            BlockWorldCohortLane(lane_id="markov.company", reason="config_drift"),  # type: ignore[arg-type]
        )
    drifted = service.record_config_drift(
        WorldCohortId(cohort.cohort_id),
        lane_id="markov.company",
        affected_from=LATER_TS,
    )
    assert drifted.event.state.reason is LaneBlockReason.CONFIG_DRIFT
    assert drifted.event.state.status is LaneOperationalStatus.BLOCKED
    loaded = store.load(WorldCohortId(cohort.cohort_id))
    assert loaded.phase is CohortPhase.COLLECTING
    assert loaded.lane_states["markov.market"].status is LaneOperationalStatus.ACTIVE
    source = _SERVICE_PATH.read_text(encoding="utf-8")
    assert "set_status" not in source
    assert "LaneBlockReason.CONFIG_DRIFT" in source


def test_invalidate_from_collecting_is_terminal_and_keeps_no_go() -> None:
    service, store = _service()
    cohort = _collecting(service, store)
    invalidated = service.invalidate(
        WorldCohortId(cohort.cohort_id),
        InvalidateWorldCohort(
            reason=InvalidationReason.PRE_START_EPISODE,
            scope="cohort",
            proofs=("proof:pre-start",),
            occurred_at=LATER_TS,
        ),
    )
    assert invalidated.event.event_type == "world_cohort_invalidated"
    loaded = store.load(WorldCohortId(cohort.cohort_id))
    assert loaded.phase is CohortPhase.INVALIDATED
    assert loaded.manifest.authority == "shadow_only"
    assert loaded.manifest.decision_effect == "none"
    with pytest.raises(ValueError, match="invalidat"):
        service.admit_slot(_admit_command(cohort, store))


def test_append_event_contract_documents_cas_and_identical_event_receipt_repair() -> None:
    from trader.application.world_model.cohort_ports import WorldCohortRepository

    doc = WorldCohortRepository.append_event.__doc__ or ""
    source = _PORTS_PATH.read_text(encoding="utf-8")
    assert "expected_sequence" in source
    assert "conflicting sequence" in source or "sequence conflict" in source or "CAS" in source
    assert "repair" in source.lower() or "receipt" in source.lower()
    assert "expected_sequence" in doc or "expected_sequence" in source


def test_idempotent_retry_seals_the_command_event_not_the_head() -> None:
    manifest = _manifest()

    arm_service, arm_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_armed"}),
    )
    arm_service.register(RegisterWorldCohort(manifest=manifest))
    armed = arm_service.arm(_arm_command(manifest))
    assert armed.availability_status == "availability_unproven"
    arm_retry = arm_service.arm(_arm_command(manifest))
    assert arm_retry.event.event_id == armed.event.event_id
    assert arm_retry.event.event_type == "world_cohort_armed"
    assert arm_retry.availability_status == "eligible"

    start_service, start_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_started"}),
    )
    start_service.register(RegisterWorldCohort(manifest=manifest))
    start_service.arm(_arm_command(manifest))
    started = start_service.start(_start_command(manifest))
    assert started.availability_status == "availability_unproven"
    blocked_after_unproven_start = start_service.block_lane(WorldCohortId(manifest.cohort_id), _block_company())
    assert blocked_after_unproven_start.event.event_type == "world_cohort_lane_blocked"
    start_retry = start_service.start(_start_command(manifest))
    assert start_retry.event.event_id == started.event.event_id
    assert start_retry.event.event_type == "world_cohort_started"
    assert start_retry.availability_status == "eligible"
    assert (
        start_store.envelope_for(blocked_after_unproven_start.event).event.event_id
        == blocked_after_unproven_start.event.event_id
    )

    admit_service, admit_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_slot_admitted"}),
    )
    first_cohort = _collecting(admit_service, admit_store, manifest)
    first_admit = admit_service.admit_slot(_admit_command(first_cohort, admit_store))
    assert first_admit.availability_status == "availability_unproven"
    second = admit_service.admit_slot(
        _admit_command(
            admit_store.load(WorldCohortId(manifest.cohort_id)),
            admit_store,
            **{
                "as_of_bar_ts": LATER_TS,
                "anchor_end_at": LATER_TS,
                "symbol": "MSFT",
                "comparison_batch_id": "batch:v1:anchor-msft",
                "episode_refs_by_contract": {
                    V1.contract_id: "world-episode:v1:" + "3" * 64,
                    V2.contract_id: "world-episode:v1:" + "4" * 64,
                },
            },
        )
    )
    assert second.event.event_type == "world_cohort_slot_admitted"
    assert second.event.event_id != first_admit.event.event_id
    admit_retry = admit_service.admit_slot(
        _admit_command(admit_store.load(WorldCohortId(manifest.cohort_id)), admit_store)
    )
    assert admit_retry.event.event_id == first_admit.event.event_id
    assert admit_retry.availability_status == "eligible"

    block_service, block_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_lane_blocked"}),
    )
    _collecting(block_service, block_store, manifest)
    blocked = block_service.block_lane(WorldCohortId(manifest.cohort_id), _block_company())
    assert blocked.availability_status == "availability_unproven"
    block_service.admit_slot(_admit_command(block_store.load(WorldCohortId(manifest.cohort_id)), block_store))
    block_retry = block_service.block_lane(WorldCohortId(manifest.cohort_id), _block_company())
    assert block_retry.event.event_id == blocked.event.event_id
    assert block_retry.event.event_type == "world_cohort_lane_blocked"
    assert block_retry.availability_status == "eligible"

    restore_service, restore_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_lane_restored"}),
    )
    _collecting(restore_service, restore_store, manifest)
    restore_service.block_lane(WorldCohortId(manifest.cohort_id), _block_company())
    restored = restore_service.restore_lane(
        WorldCohortId(manifest.cohort_id),
        RestoreWorldCohortLane(lane_id="markov.company"),
    )
    assert restored.availability_status == "availability_unproven"
    restore_service.admit_slot(_admit_command(restore_store.load(WorldCohortId(manifest.cohort_id)), restore_store))
    restore_retry = restore_service.restore_lane(
        WorldCohortId(manifest.cohort_id),
        RestoreWorldCohortLane(lane_id="markov.company"),
    )
    assert restore_retry.event.event_id == restored.event.event_id
    assert restore_retry.event.event_type == "world_cohort_lane_restored"
    assert restore_retry.availability_status == "eligible"

    close_service, close_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_collection_closed"}),
    )
    _collecting(close_service, close_store, manifest)
    closed = close_service.close(WorldCohortId(manifest.cohort_id), CloseWorldCohort(reason="fixed_end reached"))
    assert closed.availability_status == "availability_unproven"
    close_retry = close_service.close(WorldCohortId(manifest.cohort_id), CloseWorldCohort(reason="fixed_end reached"))
    assert close_retry.event.event_id == closed.event.event_id
    assert close_retry.event.event_type == "world_cohort_collection_closed"
    assert close_retry.availability_status == "eligible"

    complete_service, complete_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_completed"}),
    )
    complete_cohort = _collecting(complete_service, complete_store, manifest)
    complete_service.admit_slot(_admit_command(complete_cohort, complete_store))
    complete_service.close(WorldCohortId(manifest.cohort_id), CloseWorldCohort(reason="stop"))
    closed_for_complete = complete_store.load(WorldCohortId(manifest.cohort_id))
    completed = complete_service.complete(
        WorldCohortId(manifest.cohort_id),
        CompleteWorldCohort(evidence=_completion_evidence(closed_for_complete)),
    )
    assert completed.availability_status == "availability_unproven"
    complete_retry = complete_service.complete(
        WorldCohortId(manifest.cohort_id),
        CompleteWorldCohort(evidence=_completion_evidence(closed_for_complete)),
    )
    assert complete_retry.event.event_id == completed.event.event_id
    assert complete_retry.event.event_type == "world_cohort_completed"
    assert complete_retry.availability_status == "eligible"

    invalidate_service, invalidate_store = _service(
        crash_before_receipt=1,
        crash_event_types=frozenset({"world_cohort_invalidated"}),
    )
    _collecting(invalidate_service, invalidate_store, manifest)
    command = InvalidateWorldCohort(
        reason=InvalidationReason.PRE_START_EPISODE,
        scope="cohort",
        proofs=("proof:pre-start",),
        occurred_at=LATER_TS,
    )
    invalidated = invalidate_service.invalidate(WorldCohortId(manifest.cohort_id), command)
    assert invalidated.availability_status == "availability_unproven"
    invalidate_retry = invalidate_service.invalidate(WorldCohortId(manifest.cohort_id), command)
    assert invalidate_retry.event.event_id == invalidated.event.event_id
    assert invalidate_retry.event.event_type == "world_cohort_invalidated"
    assert invalidate_retry.availability_status == "eligible"


def test_stale_snapshot_insert_is_rejected_and_identical_event_may_repair_after_head_advances() -> None:
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import WorldCohortSlotAdmitted

    service, store = _service()
    cohort = _collecting(service, store)
    expected = len(cohort.events)
    first_event = WorldCohortSlotAdmitted(slot=_slot_for(cohort))
    first = store.append_event(first_event, expected_sequence=expected)
    assert first.sequence == expected + 1
    racing = WorldCohortSlotAdmitted(slot=_msft_slot(cohort))
    with pytest.raises(ValueError, match="sequence"):
        store.append_event(racing, expected_sequence=expected)
    second = store.append_event(racing, expected_sequence=expected + 1)
    assert second.sequence == expected + 2
    repaired = store.append_event(first_event, expected_sequence=expected)
    assert repaired.event.event_id == first.event.event_id
    assert repaired.sequence == first.sequence

    race_service, race_store = _service()
    race_cohort = _collecting(race_service, race_store)
    started = race_cohort.started_event
    assert started is not None
    evidence = race_store.envelope_for(started).require_proven()

    def sneak() -> None:
        WorldCohortService(repository=race_store, query=race_store).admit_slot(
            AdmitWorldCohortSlot(
                slot=_msft_slot(race_store.load(WorldCohortId(race_cohort.cohort_id))), started_evidence=evidence
            )
        )

    race_store.before_insert = sneak
    with pytest.raises(ValueError, match="sequence"):
        race_service.admit_slot(AdmitWorldCohortSlot(slot=_slot_for(race_cohort), started_evidence=evidence))
