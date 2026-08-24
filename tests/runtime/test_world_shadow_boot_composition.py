from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

from tests.application.test_world_cohort_lifecycle import IDENTITY_A, _FixedIdentity
from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.pilot_activation import activate_world_shadow_pilot
from trader.domain.world_cohort import CohortPhase, WorldCohortId
from trader.domain.world_feature_contract import (
    MARKET_ONTOLOGY_REVISION,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.runtime.world_model_runtime import WorldModelBackgroundRunner, compose_world_resource_guard


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
        proof = attestation.proven_heads(
            revision_id=MARKET_ONTOLOGY_REVISION,
            scope_mapping_id=WORLD_SCOPE_MAPPING_ID,
            scope_mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
            at=BOOT,
        )
        assert proof is not None
        assert proof.scope_mapping_hash == WORLD_SCOPE_MAPPING_SHA256

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
        runner = WorldModelBackgroundRunner(
            runtime=object(),  # type: ignore[arg-type]
            resource_guard=guard,
            graph_enricher=enricher,
        )
        assert runner.resource_guard is guard
        signature = inspect.signature(WorldModelBackgroundRunner.__init__)
        for name in signature.parameters:
            lowered = name.lower()
            assert "trader" not in lowered
            assert "callback" not in lowered
            assert "brain" not in lowered
            assert "broker" not in lowered
        boot = Path(daemon.__file__).read_text(encoding="utf-8")
        world_boot = boot[
            boot.index('if _env_int("CASYS_WORLD_MODEL_SHADOW_ENABLED"') : boot.index(
                "claimed_resources.learning_sync_runner"
            )
        ]
        assert "compose_world_ontology_attestation(" in world_boot
        assert "ontology_proof=" in world_boot
        assert world_boot.index("compose_world_ontology_attestation(") < world_boot.index(
            "activate_world_shadow_pilot("
        )
        assert world_boot.index("ensure_published") < world_boot.index("activate_world_shadow_pilot(")
        assert "graph_enabled=_world_model_graph" in boot
        assert "compose_world_resource_guard(" in world_boot
        assert "resource_guard=" in world_boot
        assert "trader_callback" not in world_boot
    finally:
        store.close()
