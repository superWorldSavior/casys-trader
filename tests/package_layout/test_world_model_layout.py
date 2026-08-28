from __future__ import annotations

import ast
import inspect

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations, _module_imports


_FORBIDDEN_APPLICATION_PREFIXES = (
    "trader.runtime",
    "trader.infrastructure",
    "trader.reporting",
)

_SQLITE_SCHEMA_MARKERS = (
    "sqlite3",
    "sqlite_master",
    "world_episodes",
    "world_outcome_events",
    "world_shadow_predictions",
    "payload_json",
    "SELECT ",
    "SELECT\n",
)


def _import_violations(path, prefixes: tuple[str, ...]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel_path = path.relative_to(REPO_ROOT)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in prefixes):
                violations.append(f"{rel_path}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in prefixes):
                    violations.append(f"{rel_path}: import {alias.name}")
    return violations


def _top_level_definitions(path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


_REPORTING_SHIMS = frozenset({"evaluation.py", "impact.py"})


def test_world_pilot_activation_is_an_application_module() -> None:
    path = REPO_ROOT / "trader" / "application" / "world_model" / "pilot_activation.py"
    assert path.exists()
    assert _import_violations(path, _FORBIDDEN_APPLICATION_PREFIXES) == []
    source = path.read_text(encoding="utf-8")
    assert "operator_authorized_on_boot" in source
    assert "persist_mapping_generation" in source
    assert "is_live_same_shape_mapping_cohort" in source
    assert "list_live_cohorts" in source
    assert "_close_prior_mapping_generations" not in source
    assert "MAPPING_GENERATION_DRIFT" not in source
    assert "sqlite3" not in source
    assert "def backfill" not in source


def test_application_world_model_does_not_import_runtime_infrastructure_or_reporting() -> None:
    world_model_dir = REPO_ROOT / "trader" / "application" / "world_model"
    violations: list[str] = []
    for path in sorted(world_model_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        prefixes = (
            ("trader.runtime", "trader.infrastructure")
            if path.name in _REPORTING_SHIMS
            else _FORBIDDEN_APPLICATION_PREFIXES
        )
        violations.extend(_import_violations(path, prefixes))
    assert violations == []


def test_world_gru_does_not_import_baseline() -> None:
    gru_path = REPO_ROOT / "trader" / "application" / "world_model" / "gru.py"
    assert gru_path.exists()
    assert not _module_imports(gru_path, "trader.application.world_model.baseline")
    source = gru_path.read_text(encoding="utf-8")
    assert "world_model.baseline" not in source
    assert "from trader.application.world_model import baseline" not in source


def test_world_model_service_is_application_owned_with_runtime_adapter() -> None:
    from trader.application.world_model.service import WorldModelService
    from trader.runtime.world_model_runtime import (
        WorldModelBackgroundRunner,
        WorldModelRunner,
        WorldModelRuntime,
        WorldModelShadowRuntime,
    )

    assert WorldModelService.__module__ == "trader.application.world_model.service"
    assert WorldModelBackgroundRunner.__module__ == "trader.runtime.world_model_runtime"
    assert WorldModelRuntime.__module__ == "trader.runtime.world_model_runtime"
    assert issubclass(WorldModelRuntime, WorldModelService)
    assert WorldModelShadowRuntime is WorldModelRuntime
    assert WorldModelRunner is WorldModelRuntime

    runtime_source = (REPO_ROOT / "trader" / "runtime" / "world_model_runtime.py").read_text(encoding="utf-8")
    assert "class WorldModelBackgroundRunner" in runtime_source
    assert "class WorldModelRuntime(WorldModelService)" in runtime_source


def test_world_model_cli_contains_no_sqlite_schema_or_query() -> None:
    cli_path = REPO_ROOT / "trader" / "interfaces" / "cli" / "world_model.py"
    source = cli_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(cli_path))

    sqlite_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlite3"):
            sqlite_imports.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            sqlite_imports.extend(
                alias.name for alias in node.names if alias.name == "sqlite3" or alias.name.startswith("sqlite3.")
            )
    assert sqlite_imports == []

    markers = [marker for marker in _SQLITE_SCHEMA_MARKERS if marker in source]
    assert markers == []

    from trader.interfaces.cli.world_model import read_world_model_status
    from trader.reporting.read_models.world_status import read_world_model_status as canonical

    assert read_world_model_status is canonical
    assert read_world_model_status.__module__ == "trader.reporting.read_models.world_status"


def test_world_evaluation_and_impact_are_reporting_read_model_canonical() -> None:
    read_models_dir = REPO_ROOT / "trader" / "reporting" / "read_models"
    application_dir = REPO_ROOT / "trader" / "application" / "world_model"

    assert (read_models_dir / "world_evaluation.py").exists()
    assert (read_models_dir / "world_impact.py").exists()
    assert (read_models_dir / "world_status.py").exists()
    assert (REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_model_query.py").exists()

    from trader.application.world_model.evaluation import evaluate_shadow as application_evaluate_shadow
    from trader.application.world_model.impact import evaluate_world_shadow_impact as application_evaluate_impact
    from trader.reporting.read_models.world_evaluation import evaluate_shadow
    from trader.reporting.read_models.world_impact import evaluate_world_shadow_impact

    assert evaluate_shadow.__module__ == "trader.reporting.read_models.world_evaluation"
    assert evaluate_world_shadow_impact.__module__ == "trader.reporting.read_models.world_impact"
    assert application_evaluate_shadow is evaluate_shadow
    assert application_evaluate_impact is evaluate_world_shadow_impact

    for name, forbidden in (
        ("evaluation.py", {"evaluate_shadow"}),
        ("impact.py", {"evaluate_world_shadow_impact"}),
    ):
        path = application_dir / name
        assert path.exists()
        definitions = _top_level_definitions(path)
        assert definitions.isdisjoint(forbidden)

    query_path = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_model_query.py"
    assert _import_violations(query_path, ("trader.reporting", "trader.application", "trader.runtime")) == []
    assert _module_imports(query_path, "sqlite3")


def test_production_world_model_has_no_macro_graph_bridge_handoff() -> None:
    roots = (
        REPO_ROOT / "trader" / "domain",
        REPO_ROOT / "trader" / "application" / "world_model",
        REPO_ROOT / "trader" / "infrastructure",
        REPO_ROOT / "trader" / "runtime",
    )
    forbidden = (
        "MacroGraphBridgeRunHandedOff",
        "macro_graph_bridge_run_handed_off",
        "def handoff(",
    )
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(REPO_ROOT)
            for token in forbidden:
                if token in text:
                    violations.append(f"{relative}: {token}")
    assert violations == []


def test_world_availability_and_scope_kernels_are_stdlib_domain() -> None:
    availability_path = REPO_ROOT / "trader" / "domain" / "world_availability.py"
    scope_path = REPO_ROOT / "trader" / "domain" / "world_scope.py"
    listing_path = REPO_ROOT / "trader" / "domain" / "world_scope_listing.py"
    mapping_lifecycle_path = REPO_ROOT / "trader" / "domain" / "world_scope_lifecycle.py"
    lifecycle_path = REPO_ROOT / "trader" / "domain" / "world_ontology_lifecycle.py"
    bridge_lifecycle_path = REPO_ROOT / "trader" / "domain" / "world_graph_bridge_lifecycle.py"
    assert availability_path.exists()
    assert scope_path.exists()
    assert listing_path.exists()
    assert mapping_lifecycle_path.exists()
    assert lifecycle_path.exists()
    assert bridge_lifecycle_path.exists()
    assert (
        _domain_import_violations(
            [availability_path, scope_path, listing_path, mapping_lifecycle_path, lifecycle_path, bridge_lifecycle_path],
            REPO_ROOT,
        )
        == []
    )

    from trader.domain.world_availability import (
        AvailabilityEvidence,
        PersistedWorldRef,
        PointInTimeEligibilityPolicy,
        WorldAvailabilityReceipt,
        WorldAvailabilitySubjectRef,
        world_subject_content_sha256,
    )
    from trader.domain.world_scope import (
        WorldMarketAnchorRef,
        WorldScopeMapping,
        WorldScopeResolution,
    )
    from trader.domain.world_scope_listing import propose_world_scope_mapping_entry
    from trader.domain.world_scope_lifecycle import (
        WorldScopeMappingGeneration,
        decide_cohort_anchor_admission,
        plan_world_scope_mapping_generation,
    )
    from trader.domain.world_ontology_lifecycle import (
        WorldOntologyLifecycleSpec,
        plan_world_ontology_publication,
    )
    from trader.domain.world_graph_bridge_lifecycle import (
        classify_macro_graph_bridge,
        committed_macro_graph_bridge_spec,
        require_committed_live_bridge_lineage,
    )

    assert WorldAvailabilityReceipt.__module__ == "trader.domain.world_availability"
    assert WorldAvailabilitySubjectRef.__module__ == "trader.domain.world_availability"
    assert AvailabilityEvidence.__module__ == "trader.domain.world_availability"
    assert PersistedWorldRef.__module__ == "trader.domain.world_availability"
    assert PointInTimeEligibilityPolicy.__module__ == "trader.domain.world_availability"
    assert world_subject_content_sha256.__module__ == "trader.domain.world_availability"
    assert WorldMarketAnchorRef.__module__ == "trader.domain.world_scope"
    assert WorldScopeMapping.__module__ == "trader.domain.world_scope"
    assert WorldScopeResolution.__module__ == "trader.domain.world_scope"
    assert propose_world_scope_mapping_entry.__module__ == "trader.domain.world_scope_listing"
    assert plan_world_scope_mapping_generation.__module__ == "trader.domain.world_scope_lifecycle"
    assert WorldScopeMappingGeneration.__module__ == "trader.domain.world_scope_lifecycle"
    assert decide_cohort_anchor_admission.__module__ == "trader.domain.world_scope_lifecycle"
    assert WorldOntologyLifecycleSpec.__module__ == "trader.domain.world_ontology_lifecycle"
    assert plan_world_ontology_publication.__module__ == "trader.domain.world_ontology_lifecycle"
    assert classify_macro_graph_bridge.__module__ == "trader.domain.world_graph_bridge_lifecycle"
    assert committed_macro_graph_bridge_spec.__module__ == "trader.domain.world_graph_bridge_lifecycle"
    assert require_committed_live_bridge_lineage.__module__ == "trader.domain.world_graph_bridge_lifecycle"
    assert "trader.infrastructure" not in availability_path.read_text(encoding="utf-8")
    assert "trader.infrastructure" not in scope_path.read_text(encoding="utf-8")
    assert "trader.infrastructure" not in listing_path.read_text(encoding="utf-8")
    assert "trader.infrastructure" not in mapping_lifecycle_path.read_text(encoding="utf-8")
    assert "trader.infrastructure" not in lifecycle_path.read_text(encoding="utf-8")
    assert "trader.infrastructure" not in bridge_lifecycle_path.read_text(encoding="utf-8")
    assert "trader.runtime" not in bridge_lifecycle_path.read_text(encoding="utf-8")
    assert "xtai" not in scope_path.read_text(encoding="utf-8").lower()
    assert "xnys" not in scope_path.read_text(encoding="utf-8").lower()


def test_world_feature_contract_is_stdlib_domain_single_capability_type() -> None:
    contract_path = REPO_ROOT / "trader" / "domain" / "world_feature_contract.py"
    assert contract_path.exists()
    assert _domain_import_violations([contract_path], REPO_ROOT) == []

    from trader.domain.world_feature_contract import WorldFeatureContract, WorldFeatureMask
    from trader.domain.world_scope import WorldScopeMapping, WorldScopeResolution

    assert WorldFeatureContract.__module__ == "trader.domain.world_feature_contract"
    assert WorldFeatureMask.__module__ == "trader.domain.world_feature_contract"
    assert WorldScopeMapping.__module__ == "trader.domain.world_scope"
    assert WorldScopeResolution.__module__ == "trader.domain.world_scope"
    source = contract_path.read_text(encoding="utf-8")
    assert "class WorldScopeMapping" not in source
    assert "class WorldScopeResolution" not in source
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert "numpy" not in source.lower()
    assert "networkx" not in source.lower()
    assert "class WorldFeatureContract" in source
    assert "class WorldFeatureMask" in source

    encoding_path = REPO_ROOT / "trader" / "application" / "world_model" / "encoding.py"
    assert encoding_path.exists()
    from trader.application.world_model.encoding import (
        FEATURE_CONTRACT_FINGERPRINT,
        FEATURE_CONTRACT_FINGERPRINT_CONTEXT,
    )

    assert FEATURE_CONTRACT_FINGERPRINT == "2b4023b7bab99cd39f3592c45b7b8147ad94a7de18684b7896f6daf1a454603c"
    assert FEATURE_CONTRACT_FINGERPRINT_CONTEXT == "a66a8a399f0ee6562366def6f9a231a0f0871137e9c838692f3e4c20ee6d9700"
    assert "FEATURE_CONTRACT_FINGERPRINT" not in source
    assert "trader.application.world_model.encoding" not in source


def test_world_availability_receipt_public_surface_does_not_mint_ready_at() -> None:
    import trader.infrastructure.state_db.availability_receipt as receipt_mod
    from trader.infrastructure.state_db.availability_receipt import WorldAvailabilityJsonlReceiptStore

    source = (REPO_ROOT / "trader" / "infrastructure" / "state_db" / "availability_receipt.py").read_text(
        encoding="utf-8"
    )
    assert "stamp_world_availability_receipt" not in receipt_mod.__all__
    assert "DurableWorldAvailabilityReceiptAdapter" not in receipt_mod.__all__
    assert not hasattr(receipt_mod, "stamp_world_availability_receipt")
    assert not hasattr(receipt_mod, "DurableWorldAvailabilityReceiptAdapter")
    assert "stamp_world_availability_receipt" not in source
    assert "DurableWorldAvailabilityReceiptAdapter" not in source
    append = inspect.signature(WorldAvailabilityJsonlReceiptStore.append)
    init = inspect.signature(WorldAvailabilityJsonlReceiptStore.__init__)
    assert "ready_at" not in append.parameters
    assert "clock" not in append.parameters
    assert "storage_locator" not in append.parameters
    assert "store_id" in init.parameters
    assert "_seal_world_availability_receipt" not in receipt_mod.__all__
    assert "_attest_verified_store_receipt" not in receipt_mod.__all__

    import trader.domain.world_availability as availability_mod
    import trader.domain.world_scope as scope_mod
    from trader.application.world_model.gru import GRAPH_GRU_ENCODER_IDENTITY
    from trader.domain.world_availability import WORLD_AVAILABILITY_RECEIPT_SCHEMA
    from trader.domain.world_graph import CONTEXT_IDENTITY_MAPPABLE_KINDS

    assert "_attest_verified_store_receipt" not in availability_mod.__all__
    assert "AVAILABILITY_RECEIPT_V1_SCHEMA" not in availability_mod.__all__
    assert "AVAILABILITY_RECEIPT_V2_SCHEMA" not in availability_mod.__all__
    assert "RECEIPT_SCHEMAS" not in availability_mod.__all__
    assert "AVAILABILITY_RECEIPT_SCHEMA_V2" not in receipt_mod.__all__
    assert "WORLD_AVAILABILITY_RECEIPT_SCHEMA" in availability_mod.__all__
    assert WORLD_AVAILABILITY_RECEIPT_SCHEMA == "world_availability_receipt.v1"
    assert GRAPH_GRU_ENCODER_IDENTITY == "world_gru_encoder.graph.v1"
    assert "ENCODER_VERSION_V3" not in (REPO_ROOT / "trader" / "application" / "world_model" / "gru.py").read_text(
        encoding="utf-8"
    )
    assert "IDENTITY_MAPPABLE_V2_KINDS" not in (REPO_ROOT / "trader" / "domain" / "world_graph.py").read_text(
        encoding="utf-8"
    )
    assert CONTEXT_IDENTITY_MAPPABLE_KINDS
    assert "resolve_world_market_anchor" not in scope_mod.__all__
    assert not hasattr(scope_mod, "resolve_world_market_anchor")


def test_world_resource_budget_is_stdlib_domain_with_application_port() -> None:
    domain_path = REPO_ROOT / "trader" / "domain" / "world_resource.py"
    ports_path = REPO_ROOT / "trader" / "application" / "world_model" / "resource_ports.py"
    budget_path = REPO_ROOT / "trader" / "application" / "world_model" / "resource_budget.py"
    probe_path = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_resource_probe.py"
    config_path = REPO_ROOT / "config" / "world_shadow_resource_budget.yaml"

    assert domain_path.exists()
    assert ports_path.exists()
    assert budget_path.exists()
    assert probe_path.exists()
    assert config_path.exists()
    assert _domain_import_violations([domain_path], REPO_ROOT) == []
    assert _import_violations(ports_path, _FORBIDDEN_APPLICATION_PREFIXES) == []
    assert _import_violations(budget_path, _FORBIDDEN_APPLICATION_PREFIXES) == []
    assert _import_violations(probe_path, ("trader.application", "trader.runtime", "trader.reporting")) == []

    from trader.application.world_model.resource_budget import WorldResourceBudgetGuard
    from trader.application.world_model.resource_ports import WorldResourceProbe
    from trader.domain.world_resource import WorldResourceBudget, WorldResourceDecision, decide_world_resource_budget
    from trader.infrastructure.state_db.world_resource_probe import FilesystemWorldResourceProbe

    assert WorldResourceBudget.__module__ == "trader.domain.world_resource"
    assert WorldResourceDecision.__module__ == "trader.domain.world_resource"
    assert decide_world_resource_budget.__module__ == "trader.domain.world_resource"
    assert WorldResourceProbe.__module__ == "trader.application.world_model.resource_ports"
    assert WorldResourceBudgetGuard.__module__ == "trader.application.world_model.resource_budget"
    assert FilesystemWorldResourceProbe.__module__ == "trader.infrastructure.state_db.world_resource_probe"
    domain_source = domain_path.read_text(encoding="utf-8")
    assert "VACUUM" not in domain_source
    assert "TRUNCATE" not in domain_source
    assert "os.environ" not in domain_source
    assert "sqlite3" not in domain_source


def test_world_ontology_attestation_is_application_owned_and_composed_at_runtime() -> None:
    bootstrap_path = REPO_ROOT / "trader" / "application" / "world_model" / "ontology_bootstrap.py"
    runtime_path = REPO_ROOT / "trader" / "runtime" / "world_model_runtime.py"
    daemon_path = REPO_ROOT / "trader" / "runtime" / "daemon.py"
    assert bootstrap_path.exists()
    assert "class WorldOntologyAttestation" in bootstrap_path.read_text(encoding="utf-8")
    assert _import_violations(bootstrap_path, _FORBIDDEN_APPLICATION_PREFIXES) == []
    runtime_source = runtime_path.read_text(encoding="utf-8")
    assert "def compose_world_ontology_attestation" in runtime_source
    daemon_source = daemon_path.read_text(encoding="utf-8")
    assert "compose_world_ontology_attestation(" in daemon_source
    assert "compose_world_scope_mapping_reconcile(" in daemon_source
    assert "ontology_proof=_ontology_attestation" in daemon_source
    assert daemon_source.index("compose_world_scope_mapping_reconcile(") < daemon_source.index(
        "compose_world_ontology_attestation("
    )
    assert daemon_source.index("compose_world_ontology_attestation(") < daemon_source.index(
        "activate_world_shadow_pilot("
    )


def test_world_driver_is_stdlib_domain() -> None:
    path = REPO_ROOT / "trader" / "domain" / "world_driver.py"
    assert path.exists()
    assert _domain_import_violations([path], REPO_ROOT) == []

    from trader.domain.world_driver import DriverRegimeBundle, DriverState
    from trader.domain.world_pattern import PATTERN_HYPOTHESIS_SCHEMA, PATTERN_OCCURRENCE_SCHEMA, PatternStep

    assert DriverRegimeBundle.__module__ == "trader.domain.world_driver"
    assert DriverState.__module__ == "trader.domain.world_driver"
    assert PATTERN_HYPOTHESIS_SCHEMA == "pattern_hypothesis.v1"
    assert PATTERN_OCCURRENCE_SCHEMA == "pattern_occurrence.v1"
    source = path.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert "class DriverRegimeBundle" in source
    assert "class DriverState" in source
    assert "driver_state" in inspect.signature(PatternStep).parameters


def test_pattern_discovery_driver_binding_stays_application_owned() -> None:
    ports_path = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_discovery_ports.py"
    discovery_path = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_discovery.py"
    path_mod = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_path.py"
    evaluation_path = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_evaluation.py"
    evaluation_ports = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_evaluation_ports.py"
    evaluation_request = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_evaluation_request.py"
    outcome_link = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_outcome_link.py"
    for path in (
        ports_path,
        discovery_path,
        path_mod,
        evaluation_path,
        evaluation_ports,
        evaluation_request,
        outcome_link,
    ):
        assert path.exists()
        assert _import_violations(path, _FORBIDDEN_APPLICATION_PREFIXES) == []
        source = path.read_text(encoding="utf-8")
        assert "trader.infrastructure" not in source
        assert "networkx" not in source.lower()
        assert "trader.application.world_model.gru" not in source

    from trader.application.world_model.pattern_discovery_ports import (
        PatternDriverStateBinding,
        PatternFormationRecord,
    )
    from trader.application.world_model.pattern_evaluation import PatternEvaluationService
    from trader.application.world_model.pattern_outcome_link import PatternOutcomeLinkService
    from trader.application.world_model.pattern_path import project_pattern_paths

    assert PatternDriverStateBinding.__module__ == "trader.application.world_model.pattern_discovery_ports"
    assert "driver_state_bindings" in inspect.signature(PatternFormationRecord).parameters
    assert PatternEvaluationService.__module__ == "trader.application.world_model.pattern_evaluation"
    assert PatternOutcomeLinkService.__module__ == "trader.application.world_model.pattern_outcome_link"
    assert project_pattern_paths.__module__ == "trader.application.world_model.pattern_path"
    assert "world_outcome_events" not in evaluation_path.read_text(encoding="utf-8")
    assert "WorldOutcome" not in evaluation_path.read_text(encoding="utf-8")
    assert "world_outcome_events" not in path_mod.read_text(encoding="utf-8")


def test_pattern_formation_query_is_a_readonly_state_adapter() -> None:
    path = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_formation_query.py"
    assert path.exists()
    source = path.read_text(encoding="utf-8")
    assert "class SqlitePatternFormationSource" in source
    assert "WorldGraphStore(" not in source
    assert "WorldModelStore(" not in source
    assert "StateDb(" not in source
    assert "apply_current_world_model_schema" not in source
    assert "immutable=1" not in source
    assert "networkx" not in source.lower()


def test_pattern_evaluation_and_outcome_queries_are_readonly_state_adapters() -> None:
    evaluation = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_evaluation_query.py"
    catalog = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_catalog_query.py"
    outcomes = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_outcome_query.py"
    daemon = REPO_ROOT / "trader" / "runtime" / "daemon.py"
    for path in (evaluation, catalog, outcomes):
        assert path.exists()
        source = path.read_text(encoding="utf-8")
        assert "mode=ro" in source
        assert "query_only=ON" in source
        assert "WorldGraphStore(" not in source
        assert "WorldModelStore(" not in source
        assert "StateDb(" not in source
        assert "apply_current_world_model_schema" not in source
        assert "immutable=1" not in source
        assert "networkx" not in source.lower()
    evaluation_source = evaluation.read_text(encoding="utf-8")
    catalog_source = catalog.read_text(encoding="utf-8")
    assert "world_outcome_events" not in evaluation_source
    assert "WorldOutcome" not in evaluation_source
    assert "world_outcome_events" not in catalog_source
    assert "world_outcome_events" in outcomes.read_text(encoding="utf-8")
    daemon_source = daemon.read_text(encoding="utf-8")
    assert "PatternEvaluationService" not in daemon_source
    assert "PatternOutcomeLinkService" not in daemon_source
    assert "world pattern evaluate" not in daemon_source
