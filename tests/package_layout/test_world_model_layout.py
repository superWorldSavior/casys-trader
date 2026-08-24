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


def test_world_availability_and_scope_kernels_are_stdlib_domain() -> None:
    availability_path = REPO_ROOT / "trader" / "domain" / "world_availability.py"
    scope_path = REPO_ROOT / "trader" / "domain" / "world_scope.py"
    assert availability_path.exists()
    assert scope_path.exists()
    assert _domain_import_violations([availability_path, scope_path], REPO_ROOT) == []

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

    assert WorldAvailabilityReceipt.__module__ == "trader.domain.world_availability"
    assert WorldAvailabilitySubjectRef.__module__ == "trader.domain.world_availability"
    assert AvailabilityEvidence.__module__ == "trader.domain.world_availability"
    assert PersistedWorldRef.__module__ == "trader.domain.world_availability"
    assert PointInTimeEligibilityPolicy.__module__ == "trader.domain.world_availability"
    assert world_subject_content_sha256.__module__ == "trader.domain.world_availability"
    assert WorldMarketAnchorRef.__module__ == "trader.domain.world_scope"
    assert WorldScopeMapping.__module__ == "trader.domain.world_scope"
    assert WorldScopeResolution.__module__ == "trader.domain.world_scope"
    assert "trader.infrastructure" not in availability_path.read_text(encoding="utf-8")
    assert "trader.infrastructure" not in scope_path.read_text(encoding="utf-8")
    assert "xtai" not in scope_path.read_text(encoding="utf-8").lower()
    assert "xnys" not in scope_path.read_text(encoding="utf-8").lower()


def test_world_feature_contract_is_stdlib_domain_single_v1_v2_v3_type() -> None:
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
        FEATURE_CONTRACT_FINGERPRINT_V2,
    )

    assert FEATURE_CONTRACT_FINGERPRINT == "2b4023b7bab99cd39f3592c45b7b8147ad94a7de18684b7896f6daf1a454603c"
    assert FEATURE_CONTRACT_FINGERPRINT_V2 == "a039216d5b53dab0c1aea134faabeac7b6f94c656d5880ec84882ebab05dbde5"
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

    assert "_attest_verified_store_receipt" not in availability_mod.__all__
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
    assert "ontology_proof=_ontology_attestation" in daemon_source
    assert daemon_source.index("compose_world_ontology_attestation(") < daemon_source.index(
        "activate_world_shadow_pilot("
    )
