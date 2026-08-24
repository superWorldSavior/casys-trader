from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.application.test_world_cohort_service import _MemoryWorldCohortStore
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_cohort import (
    CohortPhase,
    InvalidationReason,
    LaneOperationalStatus,
    RegisterWorldCohort,
    WorldCohortId,
    WorldRuntimeIdentity,
)
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_VERSION,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
)
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_VERSION,
    WORLD_GRAPH_V3_ONTOLOGY_REVISION,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)
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
    application_build_id="casys-trader.world.shadow_pilot.v2",
)
IDENTITY_B = WorldRuntimeIdentity(
    git_commit="c" * 40,
    python_version="3.11.9",
    numpy_version="1.26.4",
    application_build_id="casys-trader.world.shadow_pilot.v2",
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


def _service():
    store = _MemoryWorldCohortStore()
    return WorldCohortService(repository=store, query=store), store


def _matching_graph_proof():
    from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof

    return WorldOntologyHeadsProof(
        revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        content_sha256="d" * 64,
        scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
        scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
        entity_heads_hash="e" * 64,
        structural_heads_hash="f" * 64,
    )


def _activate(*, cohort_service, identity=IDENTITY_A, ontology_proof=None, now=BOOT, environ=None):
    from trader.application.world_model.pilot_activation import activate_world_shadow_pilot

    return activate_world_shadow_pilot(
        cohort_service=cohort_service,
        config_dir=CONFIG_DIR,
        now=now,
        environ={} if environ is None else environ,
        runtime_identity=_FixedIdentity(identity),
        ontology_proof=_FixedOntologyProof(ontology_proof),
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
                "bar_interval": interval,
                "session_phase": "regular",
                "market_regime": "trend_up",
                "volatility_state": "normal",
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
    assert payload["schema_version"] == WORLD_SHADOW_PILOT_SCHEMA == "world_shadow_pilot.v2"
    assert payload["pilot_id"] == "world_shadow_pilot.v2"
    assert payload["supersedes_pilot_id"] == V1_PILOT_ID
    assert "runtime_identity" not in payload
    assert "git_commit" not in source
    intent = payload["runtime_identity_intent"]
    assert intent["schema_version"] == "world_runtime_identity_intent.v1"
    assert intent["application_build_id"] == "casys-trader.world.shadow_pilot.v2"
    assert "git_commit" not in intent
    hashed = {key: value for key, value in payload.items() if key != "content_sha256"}
    assert payload["content_sha256"] == canonical_sha256(hashed)
    by_key = {item["key"]: item for item in payload["cohorts"]}
    assert by_key["technical_c1"].get("ontology_revision", payload["ontology_revision"]) == "semantic_catalog.v1"
    assert by_key["graph_v3"]["ontology_revision"] == WORLD_GRAPH_V3_ONTOLOGY_REVISION


def test_graph_unavailable_stays_registered_and_never_collecting() -> None:
    service, store = _service()
    report = _activate(cohort_service=service, ontology_proof=None)
    by_key = {item["key"]: item for item in report.cohorts}
    c1 = store.load(WorldCohortId(by_key["technical_c1"]["cohort_id"]))
    graph = store.load(WorldCohortId(by_key["graph_v3"]["cohort_id"]))
    assert c1.phase is CohortPhase.COLLECTING
    assert graph.phase is CohortPhase.REGISTERED
    assert graph.started_event is None
    assert by_key["graph_v3"]["blocked_reason"] == "graph_ontology_unpublished"
    assert graph.manifest.ontology_revision == WORLD_GRAPH_V3_ONTOLOGY_REVISION
    assert c1.manifest.ontology_revision == "semantic_catalog.v1"
    assert report.authority == "shadow_only"
    assert report.decision_effect == "none"
    assert report.recommendation == "NO_GO"


def test_exact_graph_ontology_proof_starts_graph_cohort() -> None:
    service, store = _service()
    report = _activate(cohort_service=service, ontology_proof=_matching_graph_proof())
    by_key = {item["key"]: item for item in report.cohorts}
    graph = store.load(WorldCohortId(by_key["graph_v3"]["cohort_id"]))
    assert graph.phase is CohortPhase.COLLECTING
    assert graph.started_event is not None
    assert by_key["graph_v3"].get("blocked_reason") in {None, ""}
    assert graph.manifest.ontology_revision == WORLD_GRAPH_V3_ONTOLOGY_REVISION
    assert graph.manifest.scope_mapping.mapping_id == WORLD_SCOPE_MAPPING_ID
    assert graph.manifest.scope_mapping.mapping_sha256 == WORLD_SCOPE_MAPPING_SHA256


def test_wrong_ontology_revision_or_mapping_heads_keep_graph_registered() -> None:
    from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof

    service, store = _service()
    wrong_revision = WorldOntologyHeadsProof(
        revision_id="semantic_catalog.v1",
        content_sha256="d" * 64,
        scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
        scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
        entity_heads_hash="e" * 64,
        structural_heads_hash="f" * 64,
    )
    report = _activate(cohort_service=service, ontology_proof=wrong_revision)
    graph = store.load(WorldCohortId({item["key"]: item for item in report.cohorts}["graph_v3"]["cohort_id"]))
    assert graph.phase is CohortPhase.REGISTERED
    drifted_mapping = WorldOntologyHeadsProof(
        revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        content_sha256="d" * 64,
        scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
        scope_mapping_hash="a" * 64,
        entity_heads_hash="e" * 64,
        structural_heads_hash="f" * 64,
    )
    second = _activate(cohort_service=service, ontology_proof=drifted_mapping)
    graph = store.load(WorldCohortId({item["key"]: item for item in second.cohorts}["graph_v3"]["cohort_id"]))
    assert graph.phase is CohortPhase.REGISTERED


def test_measured_runtime_drift_blocks_and_does_not_rewrite_manifest() -> None:
    service, store = _service()
    first = _activate(cohort_service=service, identity=IDENTITY_A, ontology_proof=_matching_graph_proof())
    by_key = {item["key"]: item for item in first.cohorts}
    before = {
        key: store.load(WorldCohortId(item["cohort_id"]))
        for key, item in by_key.items()
    }
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
        )
        by_key = {item["key"]: item for item in report.cohorts}
        c1_id = WorldCohortId(by_key["technical_c1"]["cohort_id"])
        graph_id = WorldCohortId(by_key["graph_v3"]["cohort_id"])
        resolver = WorldScopeResolver.load(CONFIG_DIR)
        runtime = WorldModelService(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=cohort_service,
            scope_resolver=resolver,
        )
        mapped_v1 = _episode(venue="TW", symbol="2301.TW", contract=MARKET_FEATURE_CONTRACT_VERSION)
        mapped_v3 = _episode(venue="TW", symbol="2301.TW", contract=GRAPH_FEATURE_CONTRACT_VERSION)
        unmapped_v1 = _episode(venue="US", symbol="AAPL", contract=MARKET_FEATURE_CONTRACT_VERSION)
        unmapped_v3 = _episode(venue="US", symbol="AAPL", contract=GRAPH_FEATURE_CONTRACT_VERSION)
        captured = runtime.capture_and_predict(
            (mapped_v1, mapped_v3, unmapped_v1, unmapped_v3),
            now=CAPTURE,
        )
        assert captured["errors"] == []
        c1_slots = {slot.symbol: slot for slot in cohort_service.list_slots(c1_id)}
        graph_slots = {slot.symbol: slot for slot in cohort_service.list_slots(graph_id)}
        assert set(c1_slots) == {"2301.TW", "AAPL"}
        assert set(graph_slots) == {"2301.TW", "AAPL"}
        assert set(c1_slots["2301.TW"].episode_refs_by_contract) == {MARKET_FEATURE_CONTRACT_VERSION}
        assert graph_slots["2301.TW"].episode_refs_by_contract == {
            GRAPH_FEATURE_CONTRACT_VERSION: mapped_v3.episode_id
        }
        assert GRAPH_FEATURE_CONTRACT_VERSION not in c1_slots["2301.TW"].episode_refs_by_contract
        assert MARKET_FEATURE_CONTRACT_VERSION not in graph_slots["2301.TW"].episode_refs_by_contract
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
        episode = _episode(venue="TW", symbol="2301.TW", contract=MARKET_FEATURE_CONTRACT_VERSION)
        runtime.capture_and_predict((episode,), now=CAPTURE)
        collecting = store.list_collecting_cohort_ids()
        assert collecting
        for cohort_id in collecting:
            assert cohort_service.list_slots(cohort_id) == ()
    finally:
        store.close()
