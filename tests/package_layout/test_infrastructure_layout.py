import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
    _domain_import_violations,
    _has_python_sources,
)


def test_operator_surfaces_are_nested_under_interfaces() -> None:
    trader_dir = REPO_ROOT / "trader"
    interfaces_dir = trader_dir / "interfaces"

    assert interfaces_dir.exists()
    assert sorted(path.name for path in interfaces_dir.iterdir() if path.is_dir() and path.name != "__pycache__") == [
        "cli",
        "cockpit",
        "dashboards",
        "ui",
    ]

    for old_top_level_name in ("commands", "cockpit", "ui"):
        assert not _has_python_sources(trader_dir / old_top_level_name)


def test_infrastructure_backends_are_nested_under_infrastructure() -> None:
    trader_dir = REPO_ROOT / "trader"
    infrastructure_dir = trader_dir / "infrastructure"

    assert infrastructure_dir.exists()
    assert sorted(
        path.name for path in infrastructure_dir.iterdir() if path.is_dir() and path.name != "__pycache__"
    ) == [
        "brokers",
        "files",
        "graph",
        "llm",
        "market_sources",
        "queue",
        "state_db",
    ]

    for old_top_level_name in ("files", "llm", "market_sources", "queue", "state_db"):
        assert not _has_python_sources(trader_dir / old_top_level_name)


def test_llm_infrastructure_does_not_import_agent_port_facade() -> None:
    repo_root = REPO_ROOT
    llm_infra_dir = repo_root / "trader" / "infrastructure" / "llm"
    domain_contract_path = repo_root / "trader" / "domain" / "llm.py"

    assert llm_infra_dir.exists()
    assert (llm_infra_dir / "acpx_backend.py").exists()
    assert (llm_infra_dir / "openai_backend.py").exists()
    assert domain_contract_path.exists()

    violations: list[str] = []
    for path in sorted(llm_infra_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "trader.agent.llm":
                violations.append(f"{rel_path}: from trader.agent.llm import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "trader.agent.llm" or alias.name.startswith("trader.agent.llm."):
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_execution_commission_module_is_a_compatibility_facade() -> None:
    repo_root = REPO_ROOT
    facade_path = repo_root / "trader" / "execution" / "commission.py"
    facade_tree = ast.parse(
        facade_path.read_text(encoding="utf-8"),
        filename=str(facade_path),
    )

    assert not any(isinstance(node, (ast.ClassDef, ast.FunctionDef)) for node in facade_tree.body)

    from trader.application.execute.fee_estimate import round_trip_cost
    from trader.domain.execution.fill_accounting import compute_fill_effect
    from trader.execution import commission as facade
    from trader.infrastructure.brokers.commission_models import (
        IbkrCommissionModel,
        NoCommissionModel,
        commission_model_from_name,
    )

    assert facade.IbkrCommissionModel is IbkrCommissionModel
    assert facade.NoCommissionModel is NoCommissionModel
    assert facade.commission_model_from_name is commission_model_from_name
    assert facade.compute_fill_effect is compute_fill_effect
    assert facade.round_trip_cost is round_trip_cost


def test_scheduler_json_backend_is_nested_under_state_db_with_planning_facade() -> None:
    trader_dir = REPO_ROOT / "trader"
    facade_path = trader_dir / "planning" / "scheduler.py"
    backend_path = trader_dir / "infrastructure" / "state_db" / "scheduler_json.py"

    assert facade_path.exists()
    assert backend_path.exists()
    assert not _has_python_sources(trader_dir / "scheduling")

    facade_tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    backend_tree = ast.parse(backend_path.read_text(encoding="utf-8"), filename=str(backend_path))

    facade_classes = [node.name for node in ast.walk(facade_tree) if isinstance(node, ast.ClassDef)]
    backend_classes = [node.name for node in ast.walk(backend_tree) if isinstance(node, ast.ClassDef)]

    assert "Scheduler" not in facade_classes
    assert "Scheduler" in backend_classes
    assert "from trader.infrastructure.state_db.scheduler_json import Scheduler" in facade_path.read_text(
        encoding="utf-8"
    )


def test_universe_filesystem_adapters_are_canonical_with_rotation_facades() -> None:
    repo_root = REPO_ROOT
    domain_path = repo_root / "trader" / "domain" / "universe" / "user_overrides.py"
    files_dir = repo_root / "trader" / "infrastructure" / "files"
    legacy_path = repo_root / "trader" / "market" / "rotation" / "user_overrides.py"

    assert _domain_import_violations([domain_path], repo_root) == []
    for adapter_name in ("radar_cache.py", "universe_config.py", "venue_state.py"):
        adapter_source = (files_dir / adapter_name).read_text(encoding="utf-8")
        assert "trader.market.rotation" not in adapter_source

    legacy_tree = ast.parse(
        legacy_path.read_text(encoding="utf-8"),
        filename=str(legacy_path),
    )
    assert not any(isinstance(node, (ast.ClassDef, ast.FunctionDef)) for node in legacy_tree.body)

    from trader.domain.universe.user_overrides import (
        UserOverrides,
        apply_user_overrides,
    )
    from trader.infrastructure.files.radar_cache import purge_old_radar_cache
    from trader.infrastructure.files.universe_config import (
        UniverseWriteError,
        load_user_overrides,
        write_universe_atomic,
        write_universe_if_changed,
    )
    from trader.infrastructure.files.venue_state import load_venue_state
    from trader.market.rotation import UniverseWriteError as legacy_write_error
    from trader.market.rotation import write_universe_atomic as legacy_write_universe
    from trader.market.rotation.user_overrides import (
        UserOverrides as legacy_user_overrides,
    )
    from trader.market.rotation.user_overrides import (
        apply_user_overrides as legacy_apply_user_overrides,
    )
    from trader.market.rotation.user_overrides import (
        load_user_overrides as legacy_load_user_overrides,
    )
    from trader.market.rotation.venues import (
        _purge_old_radar_cache as legacy_purge_old_radar_cache,
    )
    from trader.market.rotation.venues import load_venue_state as legacy_load_venue_state
    from trader.market.rotation.venues import (
        write_universe_if_changed as legacy_write_universe_if_changed,
    )

    assert legacy_user_overrides is UserOverrides
    assert legacy_apply_user_overrides is apply_user_overrides
    assert legacy_load_user_overrides is load_user_overrides
    assert legacy_write_error is UniverseWriteError
    assert legacy_write_universe is write_universe_atomic
    assert legacy_load_venue_state is load_venue_state
    assert legacy_purge_old_radar_cache is purge_old_radar_cache
    assert legacy_write_universe_if_changed is write_universe_if_changed


def test_state_db_trade_plan_store_imports_domain_not_planning() -> None:
    repo_root = REPO_ROOT
    module_path = repo_root / "trader" / "infrastructure" / "state_db" / "trade_plan_store.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    forbidden = "trader.planning.trade_plan"
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == forbidden:
            violations.append(f"from {forbidden} import ...")
        elif isinstance(node, ast.Import):
            violations.extend(f"import {alias.name}" for alias in node.names if alias.name == forbidden)

    assert "TradePlan.model_validate(raw)" in source
    assert "trade_plan_from_dict" not in source
    assert violations == []


def test_state_db_infrastructure_does_not_import_planning_trade_plan() -> None:
    repo_root = REPO_ROOT
    state_db_dir = repo_root / "trader" / "infrastructure" / "state_db"
    forbidden = "trader.planning.trade_plan"
    violations: list[str] = []

    for module_path in state_db_dir.glob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == forbidden:
                violations.append(f"{module_path.name}: from {forbidden} import ...")
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{module_path.name}: import {alias.name}" for alias in node.names if alias.name == forbidden
                )

    assert violations == []


def test_infrastructure_imports_stale_backoff_policy_from_domain_not_scheduler_facade() -> None:
    repo_root = REPO_ROOT
    infrastructure_dir = repo_root / "trader" / "infrastructure"
    forbidden = "trader.planning.scheduler"
    violations: list[str] = []

    for module_path in infrastructure_dir.rglob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        rel_path = module_path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == forbidden:
                stale_imports = [alias.name for alias in node.names if alias.name.startswith("STALE_BACKOFF_")]
                if stale_imports:
                    violations.append(f"{rel_path}: from {forbidden} import {', '.join(stale_imports)}")

    assert violations == []


def test_execute_order_handler_lives_only_in_infrastructure() -> None:
    repo_root = REPO_ROOT
    facade_path = repo_root / "trader" / "application" / "execute" / "order_handler.py"
    adapter_path = repo_root / "trader" / "infrastructure" / "queue" / "order_handler.py"
    assert not facade_path.exists()
    assert adapter_path.exists()

    source = adapter_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(adapter_path))

    forbidden = "trader.planning.trade_plan"
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == forbidden:
            violations.append(f"from {forbidden} import ...")
        elif isinstance(node, ast.Import):
            violations.extend(f"import {alias.name}" for alias in node.names if alias.name == forbidden)

    assert "trade_plan_from_dict" not in source
    assert "from trader.domain.trade_plan import TradePlan" in source
    assert violations == []


def test_migrations_own_legacy_trade_plan_decoder() -> None:
    repo_root = REPO_ROOT
    module_path = repo_root / "trader" / "infrastructure" / "state_db" / "migrations.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    forbidden_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "trader.planning.trade_plan":
            forbidden_imports.append("from trader.planning.trade_plan import ...")

    assert "def _plan_from_legacy_dict" in source
    assert "def _tp_from_legacy_dict" in source
    assert "trade_plan_from_dict" not in source
    assert forbidden_imports == []


def test_ledger_rotation_filesystem_adapter_lives_in_infrastructure() -> None:
    repo_root = REPO_ROOT
    infrastructure_adapter = repo_root / "trader" / "infrastructure" / "files" / "ledger_rotation.py"
    runtime_adapter = repo_root / "trader" / "runtime" / "ledger_rotation.py"
    runtime_init = repo_root / "trader" / "runtime" / "__init__.py"

    assert infrastructure_adapter.exists()
    assert not runtime_adapter.exists()
    assert '"ledger_rotation"' not in runtime_init.read_text(encoding="utf-8")

    violations: list[str] = []
    for source_root in (repo_root / "trader", repo_root / "backtest", repo_root / "scripts"):
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports_runtime_module = node.module == "trader.runtime.ledger_rotation"
                    imports_runtime_export = node.module == "trader.runtime" and any(
                        alias.name == "ledger_rotation" for alias in node.names
                    )
                    if imports_runtime_module or imports_runtime_export:
                        violations.append(f"{relative_path}:{node.lineno}: legacy runtime import")
                elif isinstance(node, ast.Import):
                    if any(alias.name == "trader.runtime.ledger_rotation" for alias in node.names):
                        violations.append(f"{relative_path}:{node.lineno}: legacy runtime import")

    assert violations == []
