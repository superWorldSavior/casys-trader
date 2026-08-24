"""Append-only SQLite cohort ledger: events, receipts, slots, crash, restart."""

from __future__ import annotations

import inspect
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trader.application.world_model.cohort_service import WorldCohortService
from trader.domain.world_availability import AvailabilityEvidence
from trader.domain.world_cohort import (
    AdmitWorldCohortSlot,
    ArmWorldCohort,
    BlockWorldCohortLane,
    CloseWorldCohort,
    CohortPhase,
    LaneBlockReason,
    RegisterWorldCohort,
    StartWorldCohort,
    WorldCohort,
    WorldCohortArmed,
    WorldCohortEventEnvelope,
    WorldCohortId,
    WorldCohortManifest,
    WorldCohortRegistered,
    WorldCohortSlot,
    WorldCohortSlotAdmitted,
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
    world_v1_feature_contract,
    world_v2_feature_contract,
)
from trader.infrastructure.state_db.world_model_store import (
    WORLD_MODEL_MIGRATIONS,
    WorldModelConflictError,
    WorldModelStore,
)


UTC = timezone.utc
CREATED = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
PLANNED_START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
STOP_AT = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
READY = datetime(2026, 8, 24, 0, 5, tzinfo=UTC)
BOOT = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
ANCHOR_TS = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
GIT = "a" * 40
COHORT_ID = "world_cohort:v1:" + "c" * 64

V1 = world_v1_feature_contract()
V2 = world_v2_feature_contract()
MARKET_MASK = WorldFeatureMask.bind(V1, mask_id="market.v1", selected_groups=("market",))
STATUS_MASK = WorldFeatureMask.bind(V2, mask_id="status_only.v1", selected_groups=("market", "status"))
COMPANY_MASK = WorldFeatureMask.bind(V2, mask_id="company.v1", selected_groups=("market", "status", "company"))

_APPEND_ONLY_TABLES = (
    "world_cohort_manifests",
    "world_cohort_events",
    "world_cohort_slots",
    "world_availability_receipts",
)


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


def _registered(manifest: WorldCohortManifest) -> WorldCohortRegistered:
    cohort = WorldCohort.register(RegisterWorldCohort(manifest=manifest))
    event = cohort.events[0]
    assert isinstance(event, WorldCohortRegistered)
    return event


def _armed(manifest: WorldCohortManifest) -> WorldCohortArmed:
    return WorldCohortArmed(
        cohort_id=manifest.cohort_id,
        manifest_sha256=manifest.manifest_sha256,
        runtime_identity=manifest.runtime_identity,
        satisfied_sensor_ids=("company",),
    )


def _started(manifest: WorldCohortManifest) -> WorldCohortStarted:
    return WorldCohortStarted(
        cohort_id=manifest.cohort_id,
        manifest_sha256=manifest.manifest_sha256,
        runtime_identity=manifest.runtime_identity,
    )


def _msft_slot(cohort: WorldCohort) -> WorldCohortSlot:
    return _slot_for(
        cohort,
        symbol="MSFT",
        comparison_batch_id="batch:v1:anchor-msft",
        episode_refs_by_contract={
            V1.contract_id: "world-episode:v1:" + "3" * 64,
            V2.contract_id: "world-episode:v1:" + "4" * 64,
        },
    )


def _store(tmp_path: Path, *, clock: datetime = READY) -> WorldModelStore:
    return WorldModelStore(tmp_path / "world_model.db", clock=lambda: clock)


def _slot_for(cohort: WorldCohort, **overrides: object) -> WorldCohortSlot:
    started = cohort.started_event
    assert started is not None
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
        "started_event_id": started.event_id,
    }
    values.update(overrides)
    return WorldCohortSlot(**values)  # type: ignore[arg-type]


def _collecting(store: WorldModelStore, manifest: WorldCohortManifest | None = None) -> WorldCohort:
    current = manifest or _manifest()
    service = WorldCohortService(repository=store, query=store)
    service.register(RegisterWorldCohort(manifest=current))
    service.arm(
        ArmWorldCohort(
            cohort_id=current.cohort_id,
            manifest_sha256=current.manifest_sha256,
            runtime_identity=current.runtime_identity,
            satisfied_sensor_ids=("company",),
        )
    )
    service.start(
        StartWorldCohort(
            cohort_id=current.cohort_id,
            manifest_sha256=current.manifest_sha256,
            runtime_identity=current.runtime_identity,
        )
    )
    return store.load(WorldCohortId(current.cohort_id))


def test_ports_surface_has_no_status_slot_append_or_caller_ready_at() -> None:
    assert not hasattr(WorldModelStore, "set_status")
    assert not hasattr(WorldModelStore, "append_slot")
    assert not hasattr(WorldModelStore, "append_slots")
    register = inspect.signature(WorldModelStore.register)
    append_event = inspect.signature(WorldModelStore.append_event)
    assert "ready_at" not in register.parameters
    assert "ready_at" not in append_event.parameters
    assert list(register.parameters) == ["self", "manifest", "event"]
    assert list(append_event.parameters) == ["self", "event", "expected_sequence"]
    assert append_event.parameters["expected_sequence"].kind is inspect.Parameter.KEYWORD_ONLY
    assert append_event.parameters["expected_sequence"].default is inspect.Parameter.empty


def test_register_assigns_store_ready_at_after_subject_commit(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    holder: dict[str, WorldModelStore] = {}
    order: list[str] = []

    def clock() -> datetime:
        store = holder["store"]
        order.append("clock")
        assert store._db.query_one("SELECT event_id FROM world_cohort_events") is not None
        assert store._db.query_one("SELECT receipt_id FROM world_availability_receipts") is None
        return READY

    store = WorldModelStore(path, clock=clock)
    holder["store"] = store
    manifest = _manifest()
    envelope = store.register(manifest, _registered(manifest))
    assert order == ["clock"]
    assert envelope.sequence == 1
    assert envelope.availability_status == "eligible"
    evidence = envelope.require_proven()
    assert isinstance(evidence, AvailabilityEvidence)
    assert evidence.receipt.ready_at == READY
    assert evidence.first_seen_at == READY
    assert evidence.receipt.storage_locator.kind == "sqlite"
    assert evidence.receipt.storage_locator.table == "world_cohort_events"
    assert evidence.receipt.storage_locator.row_id == envelope.event.event_id
    assert evidence.receipt.subject.kind == "world_cohort_event"
    assert evidence.receipt.subject.content_sha256 == world_cohort_event_payload_hash(envelope.event)
    loaded = store.load(WorldCohortId(manifest.cohort_id))
    assert loaded.phase is CohortPhase.REGISTERED
    assert loaded.events[0].event_id == envelope.event_id


def test_crash_before_subject_commit_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    manifest = _manifest()

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("subject commit failed")

    monkeypatch.setattr(store, "_persist_cohort_subject", boom)
    with pytest.raises(OSError, match="subject commit failed"):
        store.register(manifest, _registered(manifest))
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_manifests")[0] == 0
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 0
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    with pytest.raises(LookupError):
        store.load(WorldCohortId(manifest.cohort_id))


def test_crash_between_subject_and_receipt_is_unproven_until_idempotent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=lambda: READY)
    manifest = _manifest()
    event = _registered(manifest)

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("receipt commit failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="receipt commit failed"):
        store.register(manifest, event)
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_manifests")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    loaded = store.load(WorldCohortId(manifest.cohort_id))
    assert loaded.phase is CohortPhase.REGISTERED
    unproven = store.envelope_for(event)
    assert unproven.availability_status == "availability_unproven"
    with pytest.raises(ValueError, match="availability_unproven"):
        unproven.require_proven()

    monkeypatch.undo()
    retried = WorldModelStore(path, clock=lambda: READY)
    recovered = retried.register(manifest, event)
    assert recovered.availability_status == "eligible"
    assert recovered.sequence == 1
    assert recovered.require_proven().receipt.ready_at == READY
    assert retried._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 1
    assert retried._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 1
    assert retried.register(manifest, event).event_id == recovered.event_id


def test_same_cohort_id_different_hash_is_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _manifest()
    store.register(first, _registered(first))
    conflicting = _manifest(question="A different frozen protocol")
    assert conflicting.cohort_id == first.cohort_id
    assert conflicting.manifest_sha256 != first.manifest_sha256
    with pytest.raises(WorldModelConflictError, match="different hash"):
        store.register(conflicting, _registered(conflicting))
    loaded = store.load(WorldCohortId(first.cohort_id))
    assert loaded.manifest.manifest_sha256 == first.manifest_sha256


def test_append_event_is_idempotent_and_sequences_are_monotone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    store.register(manifest, _registered(manifest))
    armed = _armed(manifest)
    first = store.append_event(armed, expected_sequence=1)
    replay = store.append_event(armed, expected_sequence=1)
    assert first.sequence == 2
    assert replay.sequence == 2
    assert replay.event_id == first.event_id
    assert replay.require_proven().receipt.receipt_id == first.require_proven().receipt.receipt_id
    started = _started(manifest)
    third = store.append_event(started, expected_sequence=2)
    assert third.sequence == 3
    loaded = store.load(WorldCohortId(manifest.cohort_id))
    assert [item.event_type for item in loaded.events] == [
        "world_cohort_registered",
        "world_cohort_armed",
        "world_cohort_started",
    ]
    assert loaded.phase is CohortPhase.COLLECTING


def test_slot_projection_is_transactional_and_not_publicly_appendable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cohort = _collecting(store)
    slot = _slot_for(cohort)
    admitted = WorldCohortSlotAdmitted(slot=slot)
    envelope = store.append_event(admitted, expected_sequence=3)
    assert envelope.sequence == 4
    listed = store.list_slots(WorldCohortId(cohort.cohort_id))
    assert listed == (slot,)
    row = store._db.query_one("SELECT slot_id, event_id FROM world_cohort_slots WHERE slot_id=?", (slot.slot_id,))
    assert row is not None
    assert row["event_id"] == admitted.event_id
    replay = store.append_event(admitted, expected_sequence=3)
    assert replay.event_id == envelope.event_id
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_slots")[0] == 1
    reconstructed = store.load(WorldCohortId(cohort.cohort_id))
    assert reconstructed.admitted_slots == (slot,)
    assert not hasattr(store, "append_slot")


def test_append_event_without_manifest_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = _manifest(cohort_id="world_cohort:v1:" + "d" * 64, question="other study")
    orphan = _armed(other)
    with pytest.raises(LookupError):
        store.append_event(orphan, expected_sequence=0)


def test_anti_update_delete_triggers_cover_cohort_and_receipt_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cohort = _collecting(store)
    store.append_event(WorldCohortSlotAdmitted(slot=_slot_for(cohort)), expected_sequence=3)
    mutations = (
        "UPDATE world_cohort_manifests SET manifest_sha256='tampered'",
        "DELETE FROM world_cohort_manifests",
        "UPDATE world_cohort_events SET event_type='tampered'",
        "DELETE FROM world_cohort_events",
        "UPDATE world_cohort_slots SET symbol='MSFT'",
        "DELETE FROM world_cohort_slots",
        "UPDATE world_availability_receipts SET scope='tampered'",
        "DELETE FROM world_availability_receipts",
    )
    for sql in mutations:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store._db.transaction() as cur:
                cur.execute(sql)


def test_restart_first_seen_is_conservative_and_does_not_mutate_ready_at(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    writer = WorldModelStore(path, clock=lambda: READY)
    manifest = _manifest()
    written = writer.register(manifest, _registered(manifest))
    assert written.require_proven().first_seen_at == READY
    assert written.require_proven().receipt.ready_at == READY
    same = writer.envelope_for(written.event)
    assert same.require_proven().first_seen_at == READY
    restarted = WorldModelStore(path, clock=lambda: BOOT)
    replayed = restarted.envelope_for(written.event)
    evidence = replayed.require_proven()
    assert evidence.first_seen_at == BOOT
    assert evidence.effective_ready_at == BOOT
    assert evidence.receipt.ready_at == READY
    assert restarted.load(WorldCohortId(manifest.cohort_id)).phase is CohortPhase.REGISTERED


def test_tampered_event_payload_is_rejected_and_tampered_receipt_is_unproven(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    envelope = store.register(manifest, _registered(manifest))
    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_cohort_events_no_update")
        cur.execute(
            "UPDATE world_cohort_events SET payload_json=? WHERE event_id=?",
            ('{"event_type":"tampered"}', envelope.event_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_cohort_events_no_update
            BEFORE UPDATE ON world_cohort_events
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_events are append-only');
            END
            """
        )
    with pytest.raises(ValueError, match="tamper"):
        store.load(WorldCohortId(manifest.cohort_id))

    clean = _store(tmp_path / "clean")
    proven = clean.register(manifest, _registered(manifest))
    with clean._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_availability_receipts_no_update")
        cur.execute(
            "UPDATE world_availability_receipts SET receipt_sha256=? WHERE receipt_id=?",
            ("0" * 64, proven.require_proven().receipt.receipt_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_availability_receipts_no_update
            BEFORE UPDATE ON world_availability_receipts
            BEGIN
                SELECT RAISE(ABORT, 'world_availability_receipts are append-only');
            END
            """
        )
    unproven = clean.envelope_for(proven.event)
    assert unproven.availability_status == "availability_unproven"
    loaded = clean.load(WorldCohortId(manifest.cohort_id))
    assert loaded.phase is CohortPhase.REGISTERED


def test_reconstruction_round_trip_through_world_cohort_and_service(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = WorldCohortService(repository=store, query=store)
    manifest = _manifest()
    service.register(RegisterWorldCohort(manifest=manifest))
    service.arm(
        ArmWorldCohort(
            cohort_id=manifest.cohort_id,
            manifest_sha256=manifest.manifest_sha256,
            runtime_identity=manifest.runtime_identity,
            satisfied_sensor_ids=("company",),
        )
    )
    started = service.start(
        StartWorldCohort(
            cohort_id=manifest.cohort_id,
            manifest_sha256=manifest.manifest_sha256,
            runtime_identity=manifest.runtime_identity,
        )
    )
    cohort = store.load(WorldCohortId(manifest.cohort_id))
    service.admit_slot(
        AdmitWorldCohortSlot(
            slot=_slot_for(cohort),
            started_evidence=started.require_proven(),
        )
    )
    service.record_config_drift(manifest.cohort_id, lane_id="markov.company")
    service.close(manifest.cohort_id, CloseWorldCohort(reason="fixed_end reached"))
    reconstructed = WorldCohort.reconstruct(
        store.load(WorldCohortId(manifest.cohort_id)).manifest,
        store.load(WorldCohortId(manifest.cohort_id)).events,
    )
    assert reconstructed.phase is CohortPhase.COLLECTION_CLOSED
    assert reconstructed.admitted_slots[0].symbol == "AAPL"
    assert reconstructed.lane_states["markov.company"].reason is LaneBlockReason.CONFIG_DRIFT
    assert store.envelope_for(reconstructed.started_event).availability_status == "eligible"


def test_concurrent_register_replays_or_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    WorldModelStore(path, clock=lambda: READY).close()
    manifest = _manifest()
    event = _registered(manifest)
    barrier = threading.Barrier(8)

    def worker(_: int) -> str:
        barrier.wait()
        local = WorldModelStore(path, clock=lambda: READY)
        try:
            envelope = local.register(manifest, event)
            return envelope.event_id
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(worker, range(8)))
    assert set(ids) == {event.event_id}
    verifier = WorldModelStore(path, clock=lambda: READY)
    try:
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_cohort_manifests")[0] == 1
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 1
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 1
    finally:
        verifier.close()

    conflicting = _manifest(question="other")
    with pytest.raises(WorldModelConflictError, match="different hash"):
        WorldModelStore(path, clock=lambda: READY).register(conflicting, _registered(conflicting))


def test_append_event_content_conflict_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    store.register(manifest, _registered(manifest))
    armed = _armed(manifest)
    store.append_event(armed, expected_sequence=1)
    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_cohort_events_no_update")
        cur.execute(
            "UPDATE world_cohort_events SET payload_sha256=? WHERE event_id=?",
            ("0" * 64, armed.event_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_cohort_events_no_update
            BEFORE UPDATE ON world_cohort_events
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_events are append-only');
            END
            """
        )
    with pytest.raises(WorldModelConflictError, match="different canonical content"):
        store.append_event(armed, expected_sequence=1)


def test_migration_creates_append_only_cohort_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    versions = {
        row["version"]
        for row in store._db.query_all("SELECT version FROM schema_migrations")
    }
    assert 5 in versions
    names = {
        row["name"]
        for row in store._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in _APPEND_ONLY_TABLES:
        assert table in names
    index_sql = " ".join(
        row["sql"] or ""
        for row in store._db.query_all("SELECT sql FROM sqlite_master WHERE type='index'")
    )
    assert "study_cohort_id" in index_sql
    assert "lane_id" in index_sql
    assert "manifest_sha256" in index_sql
    assert "feature_contract_fingerprint" in index_sql
    assert "feature_mask_fingerprint" in index_sql
    catalog_versions = [version for version, _statements in WORLD_MODEL_MIGRATIONS]
    assert 5 in catalog_versions
    assert catalog_versions[-1] == WORLD_MODEL_MIGRATIONS[-1][0]
    assert WORLD_MODEL_MIGRATIONS[-1][0] in versions


def test_block_lane_event_round_trips_through_reconstruction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = WorldCohortService(repository=store, query=store)
    cohort = _collecting(store)
    envelope = service.block_lane(
        cohort.cohort_id,
        BlockWorldCohortLane(lane_id="markov.market", reason=LaneBlockReason.CONFIG_DRIFT),
    )
    assert isinstance(envelope, WorldCohortEventEnvelope)
    loaded = store.load(WorldCohortId(cohort.cohort_id))
    assert loaded.lane_states["markov.market"].reason is LaneBlockReason.CONFIG_DRIFT
    assert store.envelope_for(envelope.event).sequence == envelope.sequence


def test_stale_snapshot_insert_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _manifest()
    store.register(manifest, _registered(manifest))
    store.append_event(_armed(manifest), expected_sequence=1)
    started = _started(manifest)
    with pytest.raises(ValueError, match="sequence"):
        store.append_event(started, expected_sequence=1)
    with pytest.raises(ValueError, match="sequence"):
        store.append_event(started, expected_sequence=99)
    loaded = store.load(WorldCohortId(manifest.cohort_id))
    assert [event.event_type for event in loaded.events] == [
        "world_cohort_registered",
        "world_cohort_armed",
    ]
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 2


def test_identical_event_repairs_missing_receipt_after_head_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=lambda: READY)
    manifest = _manifest()
    store.register(manifest, _registered(manifest))
    armed = _armed(manifest)

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("receipt commit failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="receipt commit failed"):
        store.append_event(armed, expected_sequence=1)
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 2
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 1
    unproven = store.envelope_for(armed)
    assert unproven.availability_status == "availability_unproven"
    assert unproven.sequence == 2
    with pytest.raises(ValueError, match="availability_unproven"):
        unproven.require_proven()

    monkeypatch.undo()
    started = store.append_event(_started(manifest), expected_sequence=2)
    assert started.sequence == 3
    assert started.event.event_type == "world_cohort_started"
    repaired = store.append_event(armed, expected_sequence=1)
    assert repaired.event.event_id == armed.event_id
    assert repaired.event.event_type == "world_cohort_armed"
    assert repaired.sequence == 2
    assert repaired.availability_status == "eligible"
    assert repaired.require_proven().receipt.ready_at == READY
    assert store._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 3
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 3
    loaded = store.load(WorldCohortId(manifest.cohort_id))
    assert [event.event_type for event in loaded.events] == [
        "world_cohort_registered",
        "world_cohort_armed",
        "world_cohort_started",
    ]


def test_concurrent_stale_writer_cannot_append_at_wrong_sequence(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    setup = WorldModelStore(path, clock=lambda: READY)
    manifest = _manifest()
    setup.register(manifest, _registered(manifest))
    setup.append_event(_armed(manifest), expected_sequence=1)
    setup.append_event(_started(manifest), expected_sequence=2)
    cohort = setup.load(WorldCohortId(manifest.cohort_id))
    first_event = WorldCohortSlotAdmitted(slot=_slot_for(cohort))
    racing = WorldCohortSlotAdmitted(slot=_msft_slot(cohort))
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(event: WorldCohortSlotAdmitted) -> None:
        barrier.wait()
        local = WorldModelStore(path, clock=lambda: READY)
        try:
            envelope = local.append_event(event, expected_sequence=3)
            with lock:
                outcomes.append(("ok", envelope.event.event_id))
        except ValueError as exc:
            with lock:
                outcomes.append(("conflict", str(exc)))
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(worker, first_event), executor.submit(worker, racing))
        for future in futures:
            future.result()

    oks = [item for item in outcomes if item[0] == "ok"]
    conflicts = [item for item in outcomes if item[0] == "conflict"]
    assert len(oks) == 1
    assert len(conflicts) == 1
    assert "sequence" in conflicts[0][1]
    verifier = WorldModelStore(path, clock=lambda: READY)
    try:
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 4
        loaded = verifier.load(WorldCohortId(manifest.cohort_id))
        admitted_ids = {
            event.event_id for event in loaded.events if isinstance(event, WorldCohortSlotAdmitted)
        }
        assert admitted_ids == {oks[0][1]}
        leftover = racing if oks[0][1] == first_event.event_id else first_event
        with pytest.raises(ValueError, match="sequence"):
            verifier.append_event(leftover, expected_sequence=3)
        winner = first_event if oks[0][1] == first_event.event_id else racing
        replayed = verifier.append_event(winner, expected_sequence=3)
        assert replayed.sequence == 4
        assert replayed.event.event_id == oks[0][1]
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 4
    finally:
        verifier.close()


def test_append_event_keeps_restart_conservative_first_seen(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    writer = WorldModelStore(path, clock=lambda: READY)
    manifest = _manifest()
    writer.register(manifest, _registered(manifest))
    armed = _armed(manifest)
    written = writer.append_event(armed, expected_sequence=1)
    assert written.require_proven().first_seen_at == READY
    assert written.require_proven().receipt.ready_at == READY
    writer.close()

    restarted = WorldModelStore(path, clock=lambda: BOOT)
    replayed = restarted.append_event(armed, expected_sequence=1)
    evidence = replayed.require_proven()
    assert evidence.first_seen_at == BOOT
    assert evidence.effective_ready_at == BOOT
    assert evidence.receipt.ready_at == READY
    looked_up = restarted.envelope_for(armed)
    assert looked_up.require_proven().first_seen_at == BOOT
    assert looked_up.require_proven().receipt.ready_at == READY
    assert replayed.sequence == 2
    assert restarted._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 2
