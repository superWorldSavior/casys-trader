from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

from tests.application.test_world_cohort_lifecycle import IDENTITY_A, _FixedIdentity
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.pilot_activation import activate_world_shadow_pilot
from trader.domain.world_cohort import CohortPhase, WorldCohortId
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_feature_contract import (
    WORLD_SCOPE_MAPPING_ID,
)
from trader.domain.world_ontology_lifecycle import market_ontology_revision_id
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.runtime.world_model_runtime import (
    WorldModelBackgroundRunner,
    compose_pattern_shadow_workflow,
    compose_world_resource_guard,
)


UTC = timezone.utc
BOOT = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
CONFIG_DIR = REPO_ROOT / "config"


def _activate(store: WorldModelStore, *, ontology_proof=None):
    return activate_world_shadow_pilot(
        cohort_service=WorldCohortService(repository=store, query=store),
        config_dir=CONFIG_DIR,
        now=BOOT,
        environ={},
        runtime_identity=_FixedIdentity(IDENTITY_A),
        ontology_proof=ontology_proof,
    )


def _phases(store: WorldModelStore, report) -> dict[str, CohortPhase]:
    return {item["key"]: store.load(WorldCohortId(item["cohort_id"])).phase for item in report.cohorts}


def test_boot_composition_collects_graph_only_after_exact_bootstrap_proof(tmp_path: Path) -> None:
    from trader.runtime import daemon
    from trader.runtime.world_model_runtime import (
        compose_local_graph_lanes,
        compose_world_ontology_attestation,
    )

    store = WorldModelStore(tmp_path / "world_model.db", clock=lambda: BOOT)
    try:
        first = _activate(store, ontology_proof=None)
        phases = _phases(store, first)
        assert phases["technical_c1"] is CohortPhase.COLLECTING
        assert phases["graph"] is CohortPhase.REGISTERED
        assert first.decision_effect == "none"
        assert first.authority == "shadow_only"

        attestation = compose_world_ontology_attestation(
            store=store,
            config_dir=CONFIG_DIR,
            clock=lambda: BOOT,
        )
        assert attestation is not None
        assert attestation.readiness(BOOT).status == "unpublished"
        published = attestation.ensure_published(now=BOOT)
        assert published.status == "ready"
        mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
        proof = attestation.proven_heads(
            revision_id=market_ontology_revision_id(mapping),
            scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
            scope_mapping_hash=mapping.content_sha256,
            at=BOOT,
        )
        assert proof is not None
        assert proof.scope_mapping_hash == mapping.content_sha256
        assert proof.revision_id == market_ontology_revision_id(mapping)

        repaired = _activate(store, ontology_proof=attestation)
        repaired_phases = _phases(store, repaired)
        assert repaired_phases["technical_c1"] is CohortPhase.COLLECTING
        assert repaired_phases["graph"] is CohortPhase.COLLECTING
        assert repaired.decision_effect == "none"
        assert store.counts()["episodes"] == 0

        guard = compose_world_resource_guard(db_path=store.path, config_dir=CONFIG_DIR, clock=lambda: BOOT)
        predictors, enricher = compose_local_graph_lanes(
            enabled=True,
            store=store,
            config_dir=CONFIG_DIR,
            study_cohort_id=repaired.graph_cohort_id,
            ontology_attestation=attestation,
        )
        assert enricher is not None
        assert predictors
        pattern_workflow = compose_pattern_shadow_workflow(
            enabled=True,
            store=store,
            macro_root=tmp_path / "world_macro",
        )
        assert pattern_workflow is not None
        pattern_result = pattern_workflow.run(BOOT)
        assert pattern_result.status == "completed"
        assert pattern_result.discovered_count == 0
        assert pattern_result.selected_hypothesis_ids == ()
        runner = WorldModelBackgroundRunner(
            runtime=object(),  # type: ignore[arg-type]
            resource_guard=guard,
            graph_enricher=enricher,
            pattern_workflow=pattern_workflow,
        )
        assert runner.resource_guard is guard
        assert runner.pattern_workflow is pattern_workflow
        signature = inspect.signature(WorldModelBackgroundRunner.__init__)
        for name in signature.parameters:
            lowered = name.lower()
            assert "trader" not in lowered
            assert "callback" not in lowered
            assert "brain" not in lowered
            assert "broker" not in lowered
        boot = Path(daemon.__file__).read_text(encoding="utf-8")
        world_boot = boot[
            boot.index('_world_model_context = _env_int("CASYS_WORLD_MODEL_CONTEXT_ENABLED"') : boot.index(
                "claimed_resources.learning_sync_runner"
            )
        ]
        assert "compose_world_scope_mapping_reconcile(" in world_boot
        assert "compose_world_ontology_attestation(" in world_boot
        assert "ontology_proof=" in world_boot
        assert world_boot.index("compose_world_scope_mapping_reconcile(") < world_boot.index(
            "compose_world_ontology_attestation("
        )
        assert world_boot.index("compose_world_scope_mapping_reconcile(") < world_boot.index(
            "wire_world_macro_runtime("
        )
        assert world_boot.index("compose_world_scope_mapping_reconcile(") < world_boot.index(
            "load_world_shadow_pilot_config("
        )
        assert world_boot.index("compose_world_ontology_attestation(") < world_boot.index(
            "activate_world_shadow_pilot("
        )
        assert world_boot.index("ensure_published") < world_boot.index("activate_world_shadow_pilot(")
        assert "graph_enabled=_world_model_graph" in boot
        assert "compose_world_resource_guard(" in world_boot
        assert "compose_pattern_shadow_workflow(" in world_boot
        assert "pattern_workflow=" in world_boot
        assert "resource_guard=" in world_boot
        assert "trader_callback" not in world_boot
        assert "mapping reconcile cannot block market/Trader" in boot
    finally:
        store.close()


def test_boot_mapping_reconcile_is_idempotent_and_provider_failure_is_fail_open(tmp_path: Path) -> None:
    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService
    from trader.application.world_model.world_scope_resolver import WorldScopeResolver
    from trader.domain.world_scope_listing import UnresolvedInstrumentListing
    from trader.infrastructure.files.world_scope_mapping_config import YamlWorldScopeMappingStore

    mapping_copy = tmp_path / "world_scope_mapping.yaml"
    mapping_copy.write_text((CONFIG_DIR / "world_scope_mapping.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    store = YamlWorldScopeMappingStore(mapping_copy)
    current = store.load()

    class _Universe:
        def current_anchors(self):
            return tuple((entry.anchor.market_venue, entry.anchor.instrument) for entry in current.entries)

    class _Listings:
        def lookup(self, *, market_venue: str, instrument: str):
            raise RuntimeError("provider down")

    service = WorldScopeMappingReconcileService(universe=_Universe(), listings=_Listings(), store=store)
    first = service.reconcile(persist=True)
    second = service.reconcile(persist=True)
    assert first.action == second.action == "ready"
    assert first.mapping.content_sha256 == current.content_sha256 == second.mapping.content_sha256
    assert WorldScopeResolver.load(mapping_copy).mapping.content_sha256 == current.content_sha256

    class _GrowingUniverse:
        def current_anchors(self):
            return (*_Universe().current_anchors(), ("US", "NOPE"))

    failed = WorldScopeMappingReconcileService(
        universe=_GrowingUniverse(),
        listings=_Listings(),
        store=store,
    ).reconcile(persist=True)
    assert failed.action == "ready"
    assert failed.unresolved == (
        UnresolvedInstrumentListing(
            market_venue="US",
            instrument="NOPE",
            status="unresolved",
            reason="provider_failure",
        ),
    )
    assert WorldScopeResolver.load(mapping_copy).mapping.content_sha256 == current.content_sha256
