from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.application.test_world_cohort_service import _MemoryWorldCohortStore
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_cohort import (
    CloseWorldCohort,
    CohortPhase,
    InvalidationReason,
    LaneOperationalStatus,
    RegisterWorldCohort,
    WorldCohortId,
    WorldRuntimeIdentity,
)
from trader.domain.world_scope_lifecycle import WorldScopeMappingGeneration
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
)
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_ID,
    MARKET_ONTOLOGY_REVISION,
    WORLD_SCOPE_MAPPING_ID,
)
from trader.domain.world_ontology_lifecycle import market_ontology_revision_id
from trader.domain.world_scope import WorldMarketAnchorRef


UTC = timezone.utc
BOOT = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
CAPTURE = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
CONFIG_DIR = REPO_ROOT / "config"
V1_PILOT_ID = "world_shadow_pilot.v1"
IDENTITY_A = WorldRuntimeIdentity(
    git_commit="b" * 40,
    python_version="3.11.9",
    numpy_version="1.26.4",
    application_build_id="casys-trader.world.shadow_pilot.v1",
    lane_code_hash="d" * 64,
)
IDENTITY_B = WorldRuntimeIdentity(
    git_commit="c" * 40,
    python_version="3.11.9",
    numpy_version="1.26.4",
    application_build_id="casys-trader.world.shadow_pilot.v1",
    lane_code_hash="e" * 64,
)


@dataclass(frozen=True)
class _FixedIdentity:
    identity: WorldRuntimeIdentity

    def measure(self) -> WorldRuntimeIdentity:
        return self.identity


@dataclass(frozen=True)
class _FixedOntologyProof:
    proof: object | None = None

    def proven_heads(
        self,
        *,
        revision_id: str,
        scope_mapping_id: str,
        scope_mapping_hash: str,
        at: datetime,
    ):
        if self.proof is None:
            return None
        if (
            revision_id != getattr(self.proof, "revision_id", None)
            or scope_mapping_id != getattr(self.proof, "scope_mapping_id", None)
            or scope_mapping_hash != getattr(self.proof, "scope_mapping_hash", None)
        ):
            return None
        return self.proof


class _MemoryMappingGenerationStore:
    def __init__(self) -> None:
        self.generations: dict[tuple[str, str], WorldScopeMappingGeneration] = {}

    def persist_mapping_generation(self, mapping) -> WorldScopeMappingGeneration:
        generation = WorldScopeMappingGeneration.from_mapping(mapping)
        key = (generation.mapping_id, generation.mapping_sha256)
        existing = self.generations.get(key)
        if existing is not None and existing.mapping_sha256 != generation.mapping_sha256:
            raise ValueError("mapping generation already exists with different canonical content")
        self.generations[key] = generation
        return generation

    def load_mapping_generation(self, mapping_id: str, mapping_sha256: str) -> WorldScopeMappingGeneration | None:
        return self.generations.get((mapping_id, mapping_sha256))


def _service():
    store = _MemoryWorldCohortStore()
    return WorldCohortService(repository=store, query=store), store


def _live_mapping():
    return WorldScopeResolver.load(CONFIG_DIR).mapping


def _heads_proof(revision_id, mapping=None):
    from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof

    resolved = mapping if mapping is not None else _live_mapping()
    return WorldOntologyHeadsProof(
        revision_id=revision_id,
        content_sha256="d" * 64,
        scope_mapping_id=resolved.mapping_id,
        scope_mapping_hash=resolved.content_sha256,
        entity_heads_hash="e" * 64,
        structural_heads_hash="f" * 64,
    )


def _matching_graph_proof(mapping=None):
    resolved = mapping if mapping is not None else _live_mapping()
    return _heads_proof(market_ontology_revision_id(resolved), resolved)


_PIN_DEFAULT = object()


def _activate(
    *,
    cohort_service,
    identity=IDENTITY_A,
    ontology_proof=None,
    ontology_revision=_PIN_DEFAULT,
    now=BOOT,
    environ=None,
    mapping=None,
    config_dir=CONFIG_DIR,
    mapping_generations=None,
):
    from trader.application.world_model.pilot_activation import activate_world_shadow_pilot

    live = mapping if mapping is not None else _live_mapping()
    # Default stand-in pin: the committed id for the mapping under test
    # (production passes ensure_published().revision_id, extended). Tests
    # probing pin behavior pass explicit values (including None for the
    # unpinned path).
    pin = market_ontology_revision_id(live) if ontology_revision is _PIN_DEFAULT else ontology_revision
    return activate_world_shadow_pilot(
        cohort_service=cohort_service,
        config_dir=config_dir,
        now=now,
        environ={} if environ is None else environ,
        runtime_identity=_FixedIdentity(identity),
        ontology_proof=_FixedOntologyProof(ontology_proof),
        ontology_revision=pin,
        mapping=live,
        mapping_generations=mapping_generations,
    )


def _episode(*, venue: str, symbol: str, contract: str, at: datetime = CAPTURE, interval: str = "15m"):
    return WorldEpisode(
        observation=WorldObservation(
            venue=venue,
            symbol=symbol,
            bar_interval=interval,
            as_of_bar_ts=at,
            feature_contract_version=contract,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=at,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000.0,
                source="fixture",
            ),
            available_at=at,
            captured_at=at + timedelta(minutes=1),
            freshness="fresh",
            categorical_features={
                "asset_family": "equities",
                "venue": venue,
            },
            numeric_features={"return": 0.01, "atr_pct": 0.01},
        )
    )


def test_committed_pilot_separates_operator_intent_from_measured_runtime_identity() -> None:
    import yaml

    from trader.application.world_model.pilot_activation import WORLD_SHADOW_PILOT_SCHEMA
    from trader.domain.world_episode import canonical_sha256

    payload = yaml.safe_load((CONFIG_DIR / "world_shadow_pilot.yaml").read_text(encoding="utf-8"))
    source = (CONFIG_DIR / "world_shadow_pilot.yaml").read_text(encoding="utf-8")
    assert payload["schema_version"] == WORLD_SHADOW_PILOT_SCHEMA == "world_shadow_pilot.v1"
    assert payload["pilot_id"] == "world_shadow_pilot.v1"
    assert "supersedes_pilot_id" not in payload
    assert "runtime_identity" not in payload
    assert "git_commit" not in source
    intent = payload["runtime_identity_intent"]
    assert intent["schema_version"] == "world_runtime_identity_intent.v1"
    assert intent["application_build_id"] == "casys-trader.world.shadow_pilot.v1"
    assert "git_commit" not in intent
    hashed = {key: value for key, value in payload.items() if key != "content_sha256"}
    assert payload["content_sha256"] == canonical_sha256(hashed)
    by_key = {item["key"]: item for item in payload["cohorts"]}
    assert by_key["technical_c1"].get("ontology_revision", payload["ontology_revision"]) == "semantic_catalog.v1"
    assert by_key["graph"]["ontology_revision"] == MARKET_ONTOLOGY_REVISION


def test_graph_unavailable_stays_registered_and_never_collecting() -> None:
    service, store = _service()
    report = _activate(cohort_service=service, ontology_proof=None)
    by_key = {item["key"]: item for item in report.cohorts}
    c1 = store.load(WorldCohortId(by_key["technical_c1"]["cohort_id"]))
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    assert c1.phase is CohortPhase.COLLECTING
    assert graph.phase is CohortPhase.REGISTERED
    assert graph.started_event is None
    assert by_key["graph"]["blocked_reason"] == "graph_ontology_unpublished"
    assert graph.manifest.ontology_revision == market_ontology_revision_id(_live_mapping())
    assert c1.manifest.ontology_revision == "semantic_catalog.v1"
    assert report.authority == "shadow_only"
    assert report.decision_effect == "none"
    assert report.recommendation == "NO_GO"


def test_graph_without_pin_is_blocked_loudly_and_registers_nothing() -> None:
    service, store = _service()
    report = _activate(cohort_service=service, ontology_proof=None, ontology_revision=None)
    by_key = {item["key"]: item for item in report.cohorts}
    assert report.status == "blocked"
    graph_report = by_key["graph"]
    assert graph_report["blocked_reason"] == "graph_ontology_unpinned"
    assert graph_report["reason"] == "graph_ontology_unpinned"
    assert graph_report["cohort_id"] is None
    c1 = store.load(WorldCohortId(by_key["technical_c1"]["cohort_id"]))
    assert c1.phase is CohortPhase.COLLECTING
    assert len(store.manifests) == 1


def test_family_tag_pin_fails_activation_loudly_without_graph_cohort() -> None:
    service, store = _service()
    report = _activate(cohort_service=service, ontology_revision=MARKET_ONTOLOGY_REVISION)
    assert report.status == "skipped"
    assert report.reason == "activation_error"
    for manifest in store.manifests.values():
        assert not any(lane.lane_id.endswith(".graph") for lane in manifest.lanes)


def test_graph_pins_attestation_revision_verbatim_not_derived_legacy() -> None:
    service, store = _service()
    mapping = _live_mapping()
    # Stand-in for an extended attestation revision: admitted to the family
    # but not derivable from the mapping alone.
    committed = f"market_ontology:v1:{'ab' * 32}"
    assert committed != market_ontology_revision_id(mapping)
    proof = _heads_proof(committed, mapping)
    report = _activate(cohort_service=service, ontology_proof=proof, ontology_revision=committed)
    by_key = {item["key"]: item for item in report.cohorts}
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    assert graph.phase is CohortPhase.COLLECTING
    assert graph.manifest.ontology_revision == committed
    assert by_key["graph"].get("blocked_reason") in {None, ""}
    assert report.status == "started"


def test_same_mapping_legacy_pin_does_not_shadow_extended_pin() -> None:
    """Exact 2026-09-17 incident: same mapping, legacy vs extended pin."""
    service, store = _service()
    mapping = _live_mapping()
    legacy = market_ontology_revision_id(mapping)
    stale = _activate(cohort_service=service, mapping=mapping, ontology_proof=None, ontology_revision=legacy)
    stale_graph_id = {item["key"]: item for item in stale.cohorts}["graph"]["cohort_id"]
    assert store.load(WorldCohortId(stale_graph_id)).phase is CohortPhase.REGISTERED
    extended = f"market_ontology:v1:{'cd' * 32}"
    fresh = _activate(
        cohort_service=service,
        mapping=mapping,
        ontology_proof=_heads_proof(extended, mapping),
        ontology_revision=extended,
        now=BOOT + timedelta(hours=1),
    )
    by_key = {item["key"]: item for item in fresh.cohorts}
    assert by_key["graph"]["cohort_id"] != stale_graph_id
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    assert graph.phase is CohortPhase.COLLECTING
    assert graph.manifest.ontology_revision == extended
    assert graph.manifest.scope_mapping.mapping_sha256 == mapping.content_sha256
    assert store.load(WorldCohortId(stale_graph_id)).phase is CohortPhase.REGISTERED


def test_stale_never_started_graph_cohort_does_not_shadow_fresh_pin() -> None:
    mapping_a, mapping_b = _mapping_generation_b()
    service, store = _service()
    stale = _activate(cohort_service=service, mapping=mapping_a, ontology_proof=None)
    stale_graph_id = {item["key"]: item for item in stale.cohorts}["graph"]["cohort_id"]
    assert store.load(WorldCohortId(stale_graph_id)).phase is CohortPhase.REGISTERED
    fresh = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        now=BOOT + timedelta(hours=1),
    )
    by_key = {item["key"]: item for item in fresh.cohorts}
    assert by_key["graph"]["cohort_id"] != stale_graph_id
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    assert graph.phase is CohortPhase.COLLECTING
    assert graph.manifest.ontology_revision == market_ontology_revision_id(mapping_b)
    assert store.load(WorldCohortId(stale_graph_id)).phase is CohortPhase.REGISTERED


def test_exact_graph_ontology_proof_starts_graph_cohort() -> None:
    service, store = _service()
    report = _activate(cohort_service=service, ontology_proof=_matching_graph_proof())
    by_key = {item["key"]: item for item in report.cohorts}
    graph = store.load(WorldCohortId(by_key["graph"]["cohort_id"]))
    assert graph.phase is CohortPhase.COLLECTING
    assert graph.started_event is not None
    assert by_key["graph"].get("blocked_reason") in {None, ""}
    mapping = _live_mapping()
    assert graph.manifest.ontology_revision == market_ontology_revision_id(mapping)
    assert graph.manifest.scope_mapping.mapping_id == WORLD_SCOPE_MAPPING_ID
    assert graph.manifest.scope_mapping.mapping_sha256 == mapping.content_sha256


def test_wrong_ontology_revision_or_mapping_heads_keep_graph_registered() -> None:
    from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof

    service, store = _service()
    wrong_revision = WorldOntologyHeadsProof(
        revision_id="semantic_catalog.v1",
        content_sha256="d" * 64,
        scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
        scope_mapping_hash=_live_mapping().content_sha256,
        entity_heads_hash="e" * 64,
        structural_heads_hash="f" * 64,
    )
    report = _activate(cohort_service=service, ontology_proof=wrong_revision)
    graph = store.load(WorldCohortId({item["key"]: item for item in report.cohorts}["graph"]["cohort_id"]))
    assert graph.phase is CohortPhase.REGISTERED
    drifted_mapping = WorldOntologyHeadsProof(
        revision_id=MARKET_ONTOLOGY_REVISION,
        content_sha256="d" * 64,
        scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
        scope_mapping_hash="a" * 64,
        entity_heads_hash="e" * 64,
        structural_heads_hash="f" * 64,
    )
    second = _activate(cohort_service=service, ontology_proof=drifted_mapping)
    graph = store.load(WorldCohortId({item["key"]: item for item in second.cohorts}["graph"]["cohort_id"]))
    assert graph.phase is CohortPhase.REGISTERED


def test_measured_runtime_drift_blocks_and_does_not_rewrite_manifest() -> None:
    service, store = _service()
    first = _activate(cohort_service=service, identity=IDENTITY_A, ontology_proof=_matching_graph_proof())
    by_key = {item["key"]: item for item in first.cohorts}
    before = {key: store.load(WorldCohortId(item["cohort_id"])) for key, item in by_key.items()}
    hashes = {key: cohort.manifest.manifest_sha256 for key, cohort in before.items()}
    identities = {key: cohort.manifest.runtime_identity for key, cohort in before.items()}
    second = _activate(cohort_service=service, identity=IDENTITY_B, ontology_proof=_matching_graph_proof())
    assert {item["cohort_id"] for item in second.cohorts} == {item["cohort_id"] for item in first.cohorts}
    for item in second.cohorts:
        cohort = store.load(WorldCohortId(item["cohort_id"]))
        assert cohort.manifest.manifest_sha256 == hashes[item["key"]]
        assert cohort.manifest.runtime_identity == identities[item["key"]] == IDENTITY_A
        assert cohort.manifest.runtime_identity != IDENTITY_B
        assert item["reason"] == "runtime_identity_drift"
        if cohort.phase is CohortPhase.COLLECTING:
            assert all(state.status is LaneOperationalStatus.BLOCKED for state in cohort.lane_states.values())


def test_commit_only_change_without_lane_change_keeps_collecting() -> None:
    service, store = _service()
    first = _activate(cohort_service=service, identity=IDENTITY_A, ontology_proof=_matching_graph_proof())
    relabeled = WorldRuntimeIdentity(
        git_commit="f" * 40,
        python_version="3.11.9",
        numpy_version="1.26.4",
        application_build_id="casys-trader.world.shadow_pilot.v1",
        lane_code_hash="d" * 64,
    )
    second = _activate(cohort_service=service, identity=relabeled, ontology_proof=_matching_graph_proof())
    assert {item["cohort_id"] for item in second.cohorts} == {item["cohort_id"] for item in first.cohorts}
    for item in second.cohorts:
        assert item["reason"] == "operator_authorized_on_boot"
        cohort = store.load(WorldCohortId(item["cohort_id"]))
        assert cohort.phase is CohortPhase.COLLECTING


def test_restart_with_exact_measured_identity_keeps_window_and_fingerprints() -> None:
    service, store = _service()
    first = _activate(cohort_service=service, identity=IDENTITY_A, ontology_proof=_matching_graph_proof())
    second = _activate(
        cohort_service=service,
        identity=IDENTITY_A,
        ontology_proof=_matching_graph_proof(),
        now=BOOT + timedelta(days=1),
    )
    assert {item["cohort_id"] for item in first.cohorts} == {item["cohort_id"] for item in second.cohorts}
    assert second.window["planned_start_not_before"] == BOOT
    assert second.window["collection_stop_at"] == BOOT + timedelta(days=7)
    for item in second.cohorts:
        cohort = store.load(WorldCohortId(item["cohort_id"]))
        assert cohort.phase is CohortPhase.COLLECTING
        assert cohort.manifest.runtime_identity == IDENTITY_A
        assert cohort.manifest.planned_start_not_before == BOOT
        started = [event for event in cohort.events if event.event_type == "world_cohort_started"]
        assert len(started) == 1
        assert all(state.status is LaneOperationalStatus.ACTIVE for state in cohort.lane_states.values())


def _mapping_generation_b():
    from trader.domain.world_scope import WorldCanonicalScopeRef, WorldScopeMapping, WorldScopeMappingEntry

    mapping_a = _live_mapping()
    mapping_b = WorldScopeMapping(
        mapping_id=mapping_a.mapping_id,
        entries=(
            *mapping_a.entries,
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="US", instrument="ZZZZ"),
                venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XNYS"),
                country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:US"),
                region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:021"),
                world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
                provider_proofs=("provider:zzzz",),
                taxonomy_version="sessions_mic.v1",
            ),
        ),
    )
    assert mapping_b.content_sha256 != mapping_a.content_sha256
    return mapping_a, mapping_b


def test_content_rotation_reuses_collecting_same_shape_cohort() -> None:
    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    first_hashes = {
        key: store.load(WorldCohortId(cohort_id)).manifest.manifest_sha256 for key, cohort_id in first_ids.items()
    }
    first_events = {
        key: tuple(event.event_id for event in store.load(WorldCohortId(cohort_id)).events)
        for key, cohort_id in first_ids.items()
    }
    assert {store.load(WorldCohortId(cohort_id)).phase for cohort_id in first_ids.values()} == {CohortPhase.COLLECTING}
    second = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=BOOT + timedelta(days=1),
    )
    second_ids = {item["key"]: item["cohort_id"] for item in second.cohorts}
    assert second_ids == first_ids
    assert second.window["planned_start_not_before"] == BOOT
    assert second.window["collection_stop_at"] == BOOT + timedelta(days=7)
    for key, cohort_id in first_ids.items():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert all(event.event_type != "world_cohort_invalidated" for event in loaded.events)
        assert loaded.manifest.manifest_sha256 == first_hashes[key]
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_a.content_sha256
        assert tuple(event.event_id for event in loaded.events) == first_events[key]
        if key == "graph":
            assert loaded.manifest.ontology_revision == market_ontology_revision_id(mapping_a)
    assert generations.load_mapping_generation(mapping_a.mapping_id, mapping_a.content_sha256) is not None
    assert generations.load_mapping_generation(mapping_b.mapping_id, mapping_b.content_sha256) is not None


def test_scope_resolution_uses_pinned_generation_and_skips_later_anchors() -> None:
    from trader.application.world_model.service import _AnchorDeferredToNextCohort, _scope_resolution_for

    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    report = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    cohort = store.load(WorldCohortId(report.cohorts[0]["cohort_id"]))
    live_resolver = WorldScopeResolver(mapping=mapping_b, operator_config_sha256="0" * 64)
    pinned_entry = mapping_a.entries[0]
    resolution = _scope_resolution_for(
        cohort,
        venue=pinned_entry.anchor.market_venue,
        symbol=pinned_entry.anchor.instrument,
        resolver=live_resolver,
        mapping_generations=generations,
    )
    assert resolution is not None
    assert resolution.mapping_sha256 == mapping_a.content_sha256
    assert resolution.status == "resolved"
    new_entry = mapping_b.entries[-1]
    with pytest.raises(_AnchorDeferredToNextCohort, match="anchor_introduced_in_later_generation"):
        _scope_resolution_for(
            cohort,
            venue=new_entry.anchor.market_venue,
            symbol=new_entry.anchor.instrument,
            resolver=live_resolver,
            mapping_generations=generations,
        )


def test_unrelated_collecting_cohort_is_untouched_by_mapping_generation() -> None:
    from tests.application.test_world_cohort_service import _collecting, _manifest

    mapping_a, mapping_b = _mapping_generation_b()
    service, store = _service()
    _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
    )
    unrelated = _collecting(
        service,
        store,
        _manifest(cohort_id="world_cohort:v1:" + "e" * 64),
    )
    unrelated_hash = unrelated.manifest.manifest_sha256
    unrelated_events = tuple(event.event_id for event in unrelated.events)
    _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        now=BOOT + timedelta(days=1),
    )
    loaded = store.load(WorldCohortId(unrelated.cohort_id))
    assert loaded.phase is CohortPhase.COLLECTING
    assert loaded.manifest.manifest_sha256 == unrelated_hash
    assert tuple(event.event_id for event in loaded.events) == unrelated_events


def test_mapping_generation_b_reactivation_is_idempotent() -> None:
    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_b = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=BOOT + timedelta(days=1),
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    assert {item["key"]: item["cohort_id"] for item in first_b.cohorts} == first_ids
    first_event_ids = {
        key: tuple(event.event_id for event in store.load(WorldCohortId(cohort_id)).events)
        for key, cohort_id in first_ids.items()
    }
    second_b = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=BOOT + timedelta(days=2),
    )
    assert {item["cohort_id"] for item in second_b.cohorts} == set(first_ids.values())
    for key, cohort_id in first_ids.items():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_a.content_sha256
        assert tuple(event.event_id for event in loaded.events) == first_event_ids[key]


def test_closed_cohort_lets_activation_start_successor_pinned_to_current_mapping() -> None:
    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    for cohort_id in first_ids.values():
        service.close(WorldCohortId(cohort_id), CloseWorldCohort(reason="fixed_end reached"))
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.COLLECTION_CLOSED
    successor = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=BOOT + timedelta(days=1),
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    assert set(successor_ids.values()).isdisjoint(set(first_ids.values()))
    for key, cohort_id in first_ids.items():
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.COLLECTION_CLOSED
    for key, cohort_id in successor_ids.items():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_b.content_sha256
        if key == "graph":
            assert loaded.manifest.ontology_revision == market_ontology_revision_id(mapping_b)


def test_expired_collecting_cohort_is_closed_and_successor_pins_current_mapping() -> None:
    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    expired_at = BOOT + timedelta(days=7, microseconds=1)
    successor = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=expired_at,
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    assert set(successor_ids.values()).isdisjoint(set(first_ids.values()))
    for key, cohort_id in first_ids.items():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTION_CLOSED
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_a.content_sha256
    for key, cohort_id in successor_ids.items():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_b.content_sha256
        assert loaded.manifest.planned_start_not_before == expired_at
        if key == "graph":
            assert loaded.manifest.ontology_revision == market_ontology_revision_id(mapping_b)
    replay = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=expired_at + timedelta(hours=1),
    )
    assert {item["key"]: item["cohort_id"] for item in replay.cohorts} == successor_ids
    for cohort_id in successor_ids.values():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        started = [event for event in loaded.events if event.event_type == "world_cohort_started"]
        assert len(started) == 1


def test_expired_same_mapping_mints_successor_without_reviving_closed_id() -> None:
    mapping_a, _mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    expired_at = BOOT + timedelta(days=7, microseconds=1)
    successor = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
        now=expired_at,
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    assert set(successor_ids.values()).isdisjoint(set(first_ids.values()))
    for cohort_id in first_ids.values():
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.COLLECTION_CLOSED
    for cohort_id in successor_ids.values():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_a.content_sha256
        assert loaded.manifest.planned_start_not_before == expired_at


def test_mapping_rotation_during_successor_window_reuses_successor_not_a_second_collecting_graph() -> None:
    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    expired_at = BOOT + timedelta(days=7, microseconds=1)
    successor = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
        now=expired_at,
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    rotated = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=expired_at + timedelta(days=1),
    )
    rotated_ids = {item["key"]: item["cohort_id"] for item in rotated.cohorts}
    assert rotated_ids == successor_ids
    collecting = store.list_collecting_cohorts()
    graph_collecting = [
        cohort for cohort in collecting if cohort.manifest.has_graph_lanes()
    ]
    assert [cohort.cohort_id for cohort in graph_collecting] == [successor_ids["graph"]]
    loaded = store.load(WorldCohortId(successor_ids["graph"]))
    assert loaded.phase is CohortPhase.COLLECTING
    assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_a.content_sha256
    assert loaded.manifest.ontology_revision == market_ontology_revision_id(mapping_a)


def test_already_invalidated_cohort_is_not_revived_by_mapping_rotation() -> None:
    from trader.domain.world_cohort import InvalidateWorldCohort

    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    for cohort_id in first_ids.values():
        service.invalidate(
            WorldCohortId(cohort_id),
            InvalidateWorldCohort(
                reason=InvalidationReason.ACCEPTED_DRIFT,
                scope="cohort",
                proofs=("historical",),
                occurred_at=BOOT + timedelta(hours=1),
            ),
        )
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.INVALIDATED
    successor = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=BOOT + timedelta(days=1),
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    assert set(successor_ids.values()).isdisjoint(set(first_ids.values()))
    for cohort_id in first_ids.values():
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.INVALIDATED
    for cohort_id in successor_ids.values():
        assert store.load(WorldCohortId(cohort_id)).manifest.scope_mapping.mapping_sha256 == mapping_b.content_sha256


def test_old_immutable_cohort_ids_are_not_rewritten_or_auto_claimed() -> None:
    from trader.application.world_model.pilot_activation import (
        invalidate_superseded_world_shadow_cohorts,
        superseded_world_shadow_cohort_ids,
    )
    from tests.application.test_world_cohort_service import _manifest

    service, store = _service()
    prior_ids = superseded_world_shadow_cohort_ids()
    assert prior_ids
    prior_id = prior_ids[0]
    prior_manifest = _manifest(cohort_id=prior_id)
    service.register(RegisterWorldCohort(manifest=prior_manifest))
    prior_hash = store.manifests[prior_id].manifest_sha256
    prior_events = tuple(store.events[prior_id])
    report = _activate(cohort_service=service, identity=IDENTITY_A)
    new_ids = {item["cohort_id"] for item in report.cohorts}
    assert prior_id not in new_ids
    loaded = store.load(WorldCohortId(prior_id))
    assert loaded.manifest.manifest_sha256 == prior_hash
    assert tuple(event.event_id for event in loaded.events) == tuple(event.event_id for event in prior_events)
    assert loaded.phase is CohortPhase.REGISTERED
    dry = invalidate_superseded_world_shadow_cohorts(
        cohort_service=service,
        config_dir=CONFIG_DIR,
        now=BOOT,
        apply=False,
    )
    assert dry.status == "dry_run"
    assert prior_id in dry.prior_cohort_ids
    assert store.load(WorldCohortId(prior_id)).phase is CohortPhase.REGISTERED
    applied = invalidate_superseded_world_shadow_cohorts(
        cohort_service=service,
        config_dir=CONFIG_DIR,
        now=BOOT,
        apply=True,
    )
    invalidated = store.load(WorldCohortId(prior_id))
    assert applied.status == "invalidated"
    assert invalidated.phase is CohortPhase.INVALIDATED
    assert invalidated.events[-1].reason is InvalidationReason.ACCEPTED_DRIFT
    assert store.manifests[prior_id].manifest_sha256 == prior_hash


def test_two_concurrent_disjoint_cohorts_filter_foreign_refs_and_propagate_scope(
    tmp_path: Path,
) -> None:
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.service import WorldModelService
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    store = WorldModelStore(tmp_path / "world_model.db", clock=lambda: BOOT)
    try:
        cohort_service = WorldCohortService(repository=store, query=store)
        report = _activate(
            cohort_service=cohort_service,
            identity=IDENTITY_A,
            ontology_proof=_matching_graph_proof(),
            mapping_generations=store,
        )
        by_key = {item["key"]: item for item in report.cohorts}
        c1_id = WorldCohortId(by_key["technical_c1"]["cohort_id"])
        graph_id = WorldCohortId(by_key["graph"]["cohort_id"])
        resolver = WorldScopeResolver.load(CONFIG_DIR)
        runtime = WorldModelService(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=cohort_service,
            scope_resolver=resolver,
            mapping_generations=store,
        )
        mapped_v1 = _episode(venue="TW", symbol="2301.TW", contract=MARKET_FEATURE_CONTRACT_ID)
        mapped_v3 = _episode(venue="TW", symbol="2301.TW", contract=GRAPH_FEATURE_CONTRACT_ID)
        unmapped_v1 = _episode(venue="US", symbol="AAPL", contract=MARKET_FEATURE_CONTRACT_ID)
        unmapped_v3 = _episode(venue="US", symbol="AAPL", contract=GRAPH_FEATURE_CONTRACT_ID)
        captured = runtime.capture_and_predict(
            (mapped_v1, mapped_v3, unmapped_v1, unmapped_v3),
            now=CAPTURE,
        )
        assert captured["errors"] == []
        c1_slots = {slot.symbol: slot for slot in cohort_service.list_slots(c1_id)}
        graph_slots = {slot.symbol: slot for slot in cohort_service.list_slots(graph_id)}
        assert set(c1_slots) == {"2301.TW", "AAPL"}
        assert set(graph_slots) == {"2301.TW", "AAPL"}
        assert set(c1_slots["2301.TW"].episode_refs_by_contract) == {MARKET_FEATURE_CONTRACT_ID}
        assert graph_slots["2301.TW"].episode_refs_by_contract == {GRAPH_FEATURE_CONTRACT_ID: mapped_v3.episode_id}
        assert GRAPH_FEATURE_CONTRACT_ID not in c1_slots["2301.TW"].episode_refs_by_contract
        assert MARKET_FEATURE_CONTRACT_ID not in graph_slots["2301.TW"].episode_refs_by_contract
        mapped = resolver.resolve(WorldMarketAnchorRef(market_venue="TW", instrument="2301.TW"))
        assert mapped.status == "resolved"
        assert c1_slots["2301.TW"].scope_resolution == mapped
        assert graph_slots["2301.TW"].scope_resolution == mapped
        unmapped = resolver.resolve(WorldMarketAnchorRef(market_venue="US", instrument="AAPL"))
        assert unmapped.status == "unmapped"
        assert c1_slots["AAPL"].scope_resolution == unmapped
        assert graph_slots["AAPL"].scope_resolution == unmapped
        assert c1_slots["AAPL"].scope_resolution.scopes == ()
        assert "mic:" not in c1_slots["AAPL"].symbol
    finally:
        store.close()


def test_scope_resolution_is_required_when_manifest_declares_mapping(tmp_path: Path) -> None:
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.service import WorldModelService
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    store = WorldModelStore(tmp_path / "world_model.db", clock=lambda: BOOT)
    try:
        cohort_service = WorldCohortService(repository=store, query=store)
        _activate(cohort_service=cohort_service, identity=IDENTITY_A, ontology_proof=_matching_graph_proof())
        runtime = WorldModelService(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=cohort_service,
            scope_resolver=None,
        )
        episode = _episode(venue="TW", symbol="2301.TW", contract=MARKET_FEATURE_CONTRACT_ID)
        runtime.capture_and_predict((episode,), now=CAPTURE)
        collecting = store.list_collecting_cohort_ids()
        assert collecting
        for cohort_id in collecting:
            assert cohort_service.list_slots(cohort_id) == ()
    finally:
        store.close()


def test_pinned_generation_admits_a_anchors_and_skips_b_only_without_failing_batch(tmp_path: Path) -> None:
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.service import WorldModelService
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    mapping_a, mapping_b = _mapping_generation_b()
    store = WorldModelStore(tmp_path / "world_model.db", clock=lambda: BOOT)
    try:
        cohort_service = WorldCohortService(repository=store, query=store)
        report = _activate(
            cohort_service=cohort_service,
            identity=IDENTITY_A,
            mapping=mapping_a,
            ontology_proof=_matching_graph_proof(mapping_a),
            mapping_generations=store,
        )
        by_key = {item["key"]: item for item in report.cohorts}
        c1_id = WorldCohortId(by_key["technical_c1"]["cohort_id"])
        runtime = WorldModelService(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=cohort_service,
            scope_resolver=WorldScopeResolver(mapping=mapping_b, operator_config_sha256="0" * 64),
            mapping_generations=store,
        )
        pinned_entry = next(entry for entry in mapping_a.entries if entry.anchor.market_venue == "TW")
        new_entry = mapping_b.entries[-1]
        pinned_episode = _episode(
            venue=pinned_entry.anchor.market_venue,
            symbol=pinned_entry.anchor.instrument,
            contract=MARKET_FEATURE_CONTRACT_ID,
        )
        later_episode = _episode(
            venue=new_entry.anchor.market_venue,
            symbol=new_entry.anchor.instrument,
            contract=MARKET_FEATURE_CONTRACT_ID,
        )
        captured = runtime.capture_and_predict((pinned_episode, later_episode), now=CAPTURE)
        assert captured["errors"] == []
        assert captured["cohort_slots_deferred_to_next_generation"] == 1
        slots = {slot.symbol: slot for slot in cohort_service.list_slots(c1_id)}
        assert pinned_entry.anchor.instrument in slots
        assert slots[pinned_entry.anchor.instrument].scope_resolution.mapping_sha256 == mapping_a.content_sha256
        assert slots[pinned_entry.anchor.instrument].scope_resolution.status == "resolved"
        assert new_entry.anchor.instrument not in slots
    finally:
        store.close()


def test_prior_lifecycle_generation_is_not_reused_while_still_collecting(tmp_path: Path) -> None:
    import yaml

    from tests.application.test_world_pilot_activation import CONFIG_PATH, _write_hashed_pilot_config

    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        identity=IDENTITY_A,
        ontology_proof=_matching_graph_proof(),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    assert {store.load(WorldCohortId(cohort_id)).phase for cohort_id in first_ids.values()} == {CohortPhase.COLLECTING}
    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    payload["lifecycle_generation"] = int(payload["lifecycle_generation"]) + 1
    next_dir = _write_hashed_pilot_config(tmp_path / "lifecycle_generation_next", payload)
    successor = _activate(
        cohort_service=service,
        identity=IDENTITY_A,
        ontology_proof=_matching_graph_proof(),
        mapping_generations=generations,
        config_dir=next_dir,
        now=BOOT + timedelta(days=1),
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    assert set(successor_ids) == {"technical_c1", "graph"}
    assert set(successor_ids.values()).isdisjoint(set(first_ids.values()))
    for cohort_id in first_ids.values():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert all(event.event_type != "world_cohort_invalidated" for event in loaded.events)
        assert loaded.manifest.planned_start_not_before == BOOT
    for cohort_id in successor_ids.values():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert loaded.manifest.planned_start_not_before == BOOT + timedelta(days=1)


def test_expired_prior_lifecycle_mapping_cohort_is_closed_without_touching_unrelated(
    tmp_path: Path,
) -> None:
    import yaml

    from tests.application.test_world_cohort_service import _collecting, _manifest
    from tests.application.test_world_pilot_activation import CONFIG_PATH, _write_hashed_pilot_config
    from tests.read_models.test_world_graph_report import _graph_manifest, _required_sensors
    from trader.domain.world_cohort import ArmWorldCohort, StartWorldCohort

    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    first_events = {
        cohort_id: tuple(event.event_id for event in store.load(WorldCohortId(cohort_id)).events)
        for cohort_id in first_ids.values()
    }
    unrelated = _collecting(
        service,
        store,
        _manifest(cohort_id="world_cohort:v1:" + "e" * 64),
    )
    unrelated_events = tuple(event.event_id for event in unrelated.events)
    manual_graph = _graph_manifest(
        cohort_id="world_cohort:v1:" + "f" * 64,
        question="manual graph cohort must stay collecting",
        planned_start_not_before=BOOT,
        collection_stop_rule={"kind": "fixed_end", "at": BOOT + timedelta(days=1)},
        scope_mapping={"mapping_id": mapping_a.mapping_id, "mapping_sha256": mapping_a.content_sha256},
    )
    service.register(RegisterWorldCohort(manifest=manual_graph))
    service.arm(
        ArmWorldCohort(
            cohort_id=manual_graph.cohort_id,
            manifest_sha256=manual_graph.manifest_sha256,
            runtime_identity=manual_graph.runtime_identity,
            satisfied_sensor_ids=_required_sensors(manual_graph),
        )
    )
    service.start(
        StartWorldCohort(
            cohort_id=manual_graph.cohort_id,
            manifest_sha256=manual_graph.manifest_sha256,
            runtime_identity=manual_graph.runtime_identity,
        )
    )
    manual_events = tuple(event.event_id for event in store.load(WorldCohortId(manual_graph.cohort_id)).events)
    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    payload["lifecycle_generation"] = int(payload["lifecycle_generation"]) + 1
    next_dir = _write_hashed_pilot_config(tmp_path / "expired_prior_lifecycle", payload)
    expired_at = BOOT + timedelta(days=7, microseconds=1)
    successor = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        config_dir=next_dir,
        now=expired_at,
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    assert set(successor_ids.values()).isdisjoint(set(first_ids.values()))
    for key, cohort_id in first_ids.items():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTION_CLOSED
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_a.content_sha256
        assert loaded.cohort_id == cohort_id
        assert tuple(event.event_id for event in loaded.events[:-1]) == first_events[cohort_id]
        assert loaded.events[-1].event_type == "world_cohort_collection_closed"
        if key == "graph":
            assert loaded.manifest.ontology_revision == market_ontology_revision_id(mapping_a)
    for key, cohort_id in successor_ids.items():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert loaded.manifest.scope_mapping.mapping_sha256 == mapping_b.content_sha256
        assert loaded.manifest.planned_start_not_before == expired_at
        if key == "graph":
            assert loaded.manifest.ontology_revision == market_ontology_revision_id(mapping_b)
    graph_collecting = [
        cohort
        for cohort in store.list_collecting_cohorts()
        if cohort.manifest.has_graph_lanes()
        and cohort.manifest.question != "manual graph cohort must stay collecting"
    ]
    assert [cohort.cohort_id for cohort in graph_collecting] == [successor_ids["graph"]]
    loaded_unrelated = store.load(WorldCohortId(unrelated.cohort_id))
    assert loaded_unrelated.phase is CohortPhase.COLLECTING
    assert tuple(event.event_id for event in loaded_unrelated.events) == unrelated_events
    loaded_manual = store.load(WorldCohortId(manual_graph.cohort_id))
    assert loaded_manual.phase is CohortPhase.COLLECTING
    assert tuple(event.event_id for event in loaded_manual.events) == manual_events
    replay = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        config_dir=next_dir,
        now=expired_at + timedelta(hours=1),
    )
    assert {item["key"]: item["cohort_id"] for item in replay.cohorts} == successor_ids
    for cohort_id in first_ids.values():
        closed = store.load(WorldCohortId(cohort_id))
        assert closed.phase is CohortPhase.COLLECTION_CLOSED
        assert sum(1 for event in closed.events if event.event_type == "world_cohort_collection_closed") == 1
    for cohort_id in successor_ids.values():
        loaded = store.load(WorldCohortId(cohort_id))
        assert loaded.phase is CohortPhase.COLLECTING
        assert len([event for event in loaded.events if event.event_type == "world_cohort_started"]) == 1


def test_collection_window_stays_open_at_fixed_end_and_closes_after() -> None:
    mapping_a, mapping_b = _mapping_generation_b()
    generations = _MemoryMappingGenerationStore()
    service, store = _service()
    first = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
        mapping_generations=generations,
    )
    first_ids = {item["key"]: item["cohort_id"] for item in first.cohorts}
    stop_at = BOOT + timedelta(days=7)
    at_end = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=stop_at,
    )
    assert {item["key"]: item["cohort_id"] for item in at_end.cohorts} == first_ids
    for cohort_id in first_ids.values():
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.COLLECTING
    successor = _activate(
        cohort_service=service,
        mapping=mapping_b,
        ontology_proof=_matching_graph_proof(mapping_b),
        mapping_generations=generations,
        now=stop_at + timedelta(microseconds=1),
    )
    successor_ids = {item["key"]: item["cohort_id"] for item in successor.cohorts}
    assert set(successor_ids.values()).isdisjoint(set(first_ids.values()))
    for cohort_id in first_ids.values():
        assert store.load(WorldCohortId(cohort_id)).phase is CohortPhase.COLLECTION_CLOSED
    graph_collecting = [
        cohort.cohort_id
        for cohort in store.list_collecting_cohorts()
        if cohort.manifest.has_graph_lanes()
    ]
    assert graph_collecting == [successor_ids["graph"]]


def test_configured_mapping_generation_query_fails_closed_when_pin_is_missing() -> None:
    from trader.application.world_model.service import _scope_resolution_for

    mapping_a, _mapping_b = _mapping_generation_b()
    empty = _MemoryMappingGenerationStore()
    service, store = _service()
    report = _activate(
        cohort_service=service,
        mapping=mapping_a,
        ontology_proof=_matching_graph_proof(mapping_a),
    )
    cohort = store.load(WorldCohortId(report.cohorts[0]["cohort_id"]))
    entry = mapping_a.entries[0]
    with pytest.raises(ValueError, match="pinned_generation_unavailable"):
        _scope_resolution_for(
            cohort,
            venue=entry.anchor.market_venue,
            symbol=entry.anchor.instrument,
            resolver=WorldScopeResolver(mapping=mapping_a, operator_config_sha256="0" * 64),
            mapping_generations=empty,
        )


def test_capture_records_pinned_generation_unavailable_without_admitting(tmp_path: Path) -> None:
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.application.world_model.service import WorldModelService
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    store = WorldModelStore(tmp_path / "world_model.db", clock=lambda: BOOT)
    try:
        cohort_service = WorldCohortService(repository=store, query=store)
        report = _activate(
            cohort_service=cohort_service,
            identity=IDENTITY_A,
            ontology_proof=_matching_graph_proof(),
        )
        by_key = {item["key"]: item for item in report.cohorts}
        c1_id = WorldCohortId(by_key["technical_c1"]["cohort_id"])
        runtime = WorldModelService(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=cohort_service,
            scope_resolver=WorldScopeResolver.load(CONFIG_DIR),
            mapping_generations=store,
        )
        episode = _episode(venue="TW", symbol="2301.TW", contract=MARKET_FEATURE_CONTRACT_ID)
        captured = runtime.capture_and_predict((episode,), now=CAPTURE)
        assert captured["errors"]
        assert any("pinned_generation_unavailable" in str(item.get("error")) for item in captured["errors"])
        assert captured["cohort_slots_deferred_to_next_generation"] == 0
        assert cohort_service.list_slots(c1_id) == ()
    finally:
        store.close()
