import ast
import subprocess
import sys
from pathlib import Path


def _has_python_sources(path: Path) -> bool:
    if not path.exists():
        return False
    return any("__pycache__" not in candidate.parts for candidate in path.rglob("*.py"))


def _assert_application_submodule_layout(application_dir: Path, submodule: str, modules: list[str]) -> None:
    submodule_dir = application_dir / submodule

    assert (submodule_dir / "__init__.py").exists()
    for module in modules:
        new_path = submodule_dir / f"{module}.py"
        shim_path = application_dir / f"{module}.py"

        assert new_path.exists()
        assert not shim_path.exists()


def _domain_import_violations(paths: list[Path], repo_root: Path) -> list[str]:
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level > 0 or node.module is None:
                    continue
                module = node.module
                root = module.split(".", 1)[0]
                if module.startswith("trader.") and not module.startswith("trader.domain"):
                    violations.append(f"{rel_path}: from {module} import ...")
                elif root != "trader" and root not in sys.stdlib_module_names:
                    violations.append(f"{rel_path}: from {module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    root = module.split(".", 1)[0]
                    if module.startswith("trader.") and not module.startswith("trader.domain"):
                        violations.append(f"{rel_path}: import {module}")
                    elif root != "trader" and root not in sys.stdlib_module_names:
                        violations.append(f"{rel_path}: import {module}")
    return violations


def test_market_rotation_pure_modules_have_no_high_level_dependencies() -> None:
    rotation_dir = Path(__file__).resolve().parents[1] / "trader" / "market" / "rotation"
    adapter_facades = {"core.py", "user_overrides.py", "venues.py"}

    violations: list[str] = []
    for source_path in sorted(rotation_dir.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        source = source_path.read_text(encoding="utf-8")
        forbidden_fragments = ["trader.execution", "trader.agent"]
        if source_path.name in adapter_facades:
            source = source.replace("trader.infrastructure.files", "")
            forbidden_fragments.append("trader.infrastructure")
        else:
            forbidden_fragments.append("trader.infrastructure")
        for fragment in forbidden_fragments:
            if fragment in source:
                violations.append(f"{source_path.relative_to(rotation_dir)}: {fragment}")

    assert violations == []


def test_application_package_has_only_canonical_subpackages() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    entries = {
        path.name
        for path in application_dir.iterdir()
        if path.name != "__pycache__"
    }

    assert entries == {
        "analyst",
        "__init__.py",
        "cycle",
        "decide",
        "execute",
        "exit",
        "migration",
        "portfolio",
        "queue",
        "record",
        "universe",
    }


def test_application_does_not_import_infrastructure_outside_compatibility_facades() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    application_dir = repo_root / "trader" / "application"
    compatibility_facades = {
        Path("execute/order_handler.py"),
    }
    violations: list[str] = []

    for source_path in sorted(application_dir.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        relative_path = source_path.relative_to(application_dir)
        if relative_path in compatibility_facades:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "trader.infrastructure"
            ):
                violations.append(f"{relative_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("trader.infrastructure"):
                        violations.append(f"{relative_path}: import {alias.name}")

    assert violations == []


def test_application_analyst_modules_are_nested_without_legacy_shims() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "analyst",
        [
            "news_macro",
        ],
    )


def test_application_migration_modules_are_nested_without_legacy_shims() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "migration",
        [
            "strategy_language",
        ],
    )


def test_application_record_modules_are_nested_without_legacy_shims() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "record",
        [
            "decision_recorder",
            "decision_entries",
            "decision_watches",
            "tool_outcomes",
            "plan_review",
            "confidence_feedback",
            "gross_feedback",
        ],
    )


def test_application_cycle_modules_are_nested_without_legacy_shims() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "cycle",
        [
            "decision_scope",
            "schedule",
            "infra_holds",
            "market_snapshot",
            "watch_scanner",
        ],
    )


def test_cycle_decision_scope_is_application_canonical_and_daemon_delegates() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    service_path = repo_root / "trader" / "application" / "cycle" / "decision_scope.py"
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"

    service_source = service_path.read_text(encoding="utf-8")
    assert "trader.runtime" not in service_source
    assert "trader.infrastructure" not in service_source
    assert "trader.market" not in service_source

    daemon_source = daemon_path.read_text(encoding="utf-8")
    assert "decision_scope.prepare_decision_scope" in daemon_source
    assert "armed_plans.resolve_armed_plan_triggers" not in daemon_source
    assert "infra_holds.quiet_gate_decisions" not in daemon_source

    daemon_tree = ast.parse(daemon_source, filename=str(daemon_path))
    daemon_definitions = {
        node.name
        for node in ast.walk(daemon_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_analysis_eligible" not in daemon_definitions


def test_market_execution_eligibility_is_canonical_domain_module_with_market_facade() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    module_path = trader_dir / "domain" / "market" / "execution_eligibility.py"
    facade_path = trader_dir / "market" / "execution_eligibility.py"

    assert module_path.exists()
    assert facade_path.exists()
    assert not (trader_dir / "application" / "cycle" / "execution_eligibility.py").exists()
    assert "from trader.domain.market.execution_eligibility import *" in facade_path.read_text(encoding="utf-8")

    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(("trader.application", "trader.runtime", "trader.infrastructure", "trader.market", "trader.planning")):
                violations.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("trader.application", "trader.runtime", "trader.infrastructure", "trader.market", "trader.planning")):
                    violations.append(f"import {alias.name}")

    assert violations == []


def test_application_decide_modules_are_nested_without_legacy_shims() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "decide",
        [
            "planner_batch",
            "one",
            "handler",
            "tool_round",
            "queue_dispatch",
            "recent_decisions",
        ],
    )


def test_application_exit_modules_are_nested_without_legacy_shims() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "exit",
        [
            "planned_exits",
            "fill_plan_effects",
            "exit_bars",
            "exit_update",
            "armed_plans",
        ],
    )


def test_market_volatility_is_canonical_low_layer_module() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    module_path = trader_dir / "market" / "volatility.py"

    assert module_path.exists()
    assert not (trader_dir / "application" / "exit" / "reference_volatility.py").exists()

    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(("trader.application", "trader.runtime")):
                violations.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("trader.application", "trader.runtime")):
                    violations.append(f"import {alias.name}")

    assert violations == []


def test_application_execute_modules_are_nested_without_legacy_shims() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "execute",
        [
            "cycle_decision",
            "order_admission",
            "risk_admission",
            "risk_capacity",
            "fill_outcome",
            "entry_context",
            "queue_dispatch",
            "queue_plan",
            "order_handler",
        ],
    )


def test_only_legacy_compat_modules_are_flat_files() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    flat_files = sorted(path.name for path in trader_dir.glob("*.py"))

    assert flat_files == ["__init__.py"]


def test_top_level_packages_have_declared_architecture_roles() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    actual = {
        path.name
        for path in trader_dir.iterdir()
        if path.is_dir() and path.name != "__pycache__" and _has_python_sources(path)
    }
    canonical_packages = {
        "agent",
        "application",
        "domain",
        "execution",
        "infrastructure",
        "interfaces",
        "market",
        "planning",
        "reporting",
        "runtime",
        "support",
    }
    compatibility_facades = set()

    assert actual == canonical_packages | compatibility_facades


def test_operator_surfaces_are_nested_under_interfaces() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
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
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    infrastructure_dir = trader_dir / "infrastructure"

    assert infrastructure_dir.exists()
    assert sorted(path.name for path in infrastructure_dir.iterdir() if path.is_dir() and path.name != "__pycache__") == [
        "files",
        "llm",
        "market_sources",
        "queue",
        "state_db",
    ]

    for old_top_level_name in ("files", "llm", "market_sources", "queue", "state_db"):
        assert not _has_python_sources(trader_dir / old_top_level_name)


def test_llm_infrastructure_does_not_import_agent_port_facade() -> None:
    repo_root = Path(__file__).resolve().parents[1]
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


def test_semantic_catalog_is_nested_under_domain() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    semantic_dir = trader_dir / "domain" / "semantic"

    assert semantic_dir.exists()
    assert (semantic_dir / "catalog.py").exists()
    assert not _has_python_sources(trader_dir / "semantic")


def test_contract_value_types_are_domain_canonical_with_public_facades() -> None:
    from dataclasses import asdict, is_dataclass, replace

    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    domain_paths = [
        trader_dir / "domain" / "decisions.py",
        trader_dir / "domain" / "strategy_language.py",
        trader_dir / "domain" / "risk.py",
    ]

    for path in domain_paths:
        assert path.exists()

    assert "from trader.domain.decisions import *" in (
        trader_dir / "agent" / "protocol" / "types.py"
    ).read_text(encoding="utf-8")
    assert "from trader.domain.strategy_language import *" in (
        trader_dir / "agent" / "protocol" / "strategy_language.py"
    ).read_text(encoding="utf-8")
    execution_risk_source = (trader_dir / "execution" / "risk.py").read_text(encoding="utf-8")
    assert "from trader.domain.risk import RiskLimits" in execution_risk_source
    assert "from trader.domain.risk import Verdict" in execution_risk_source

    import trader.agent.protocol.strategy_language as strategy_facade
    import trader.agent.protocol.types as types_facade
    import trader.domain.decisions as domain_decisions
    import trader.domain.risk as domain_risk
    import trader.domain.strategy_language as domain_strategy
    import trader.execution.risk as execution_risk

    assert types_facade.Decision is domain_decisions.Decision
    assert types_facade.IndicatorRequest is domain_decisions.IndicatorRequest
    assert types_facade.ContextResearchRequest is domain_decisions.ContextResearchRequest
    assert types_facade.BatchToolCallRequest is domain_decisions.BatchToolCallRequest
    assert strategy_facade.CompiledStrategyCall is domain_strategy.CompiledStrategyCall
    assert strategy_facade.compile_strategy_call is domain_strategy.compile_strategy_call
    assert execution_risk.RiskLimits is domain_risk.RiskLimits
    assert execution_risk.Verdict is domain_risk.Verdict
    assert execution_risk.RiskGate.__module__ == "trader.execution.risk"

    decision = domain_decisions.Decision.hold("SPY", "wait")
    assert is_dataclass(decision)
    assert domain_decisions.Decision.__dataclass_params__.frozen is True
    assert domain_decisions.IndicatorRequest.__dataclass_params__.frozen is True
    assert domain_decisions.ContextResearchRequest.__dataclass_params__.frozen is True
    assert domain_decisions.BatchToolCallRequest.__dataclass_params__.frozen is True
    assert domain_strategy.CompiledStrategyCall.__dataclass_params__.frozen is True
    assert domain_risk.RiskLimits.__dataclass_params__.frozen is True
    assert domain_risk.Verdict.__dataclass_params__.frozen is True
    assert asdict(decision)["intent"] == "HOLD"
    assert replace(decision, confidence=0.5).confidence == 0.5

    assert _domain_import_violations(domain_paths, repo_root) == []


def test_llm_router_and_backend_port_are_domain_canonical() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    domain_path = repo_root / "trader" / "domain" / "llm.py"
    facade_path = repo_root / "trader" / "agent" / "llm.py"

    assert _domain_import_violations([domain_path], repo_root) == []
    facade_tree = ast.parse(
        facade_path.read_text(encoding="utf-8"),
        filename=str(facade_path),
    )
    facade_classes = {
        node.name
        for node in facade_tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert {"LlmBackend", "LlmRouter"}.isdisjoint(facade_classes)

    from trader.agent.llm import LlmBackend as facade_backend
    from trader.agent.llm import LlmRouter as facade_router
    from trader.domain.llm import LlmBackend, LlmRouter

    assert facade_backend is LlmBackend
    assert facade_router is LlmRouter


def test_min_trade_confidence_reader_has_single_config_source(monkeypatch, tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    risk_source = (repo_root / "trader" / "execution" / "risk.py").read_text(encoding="utf-8")

    import trader.execution.risk as execution_risk
    from trader.interfaces.ui import tui
    from trader.reporting.read_models import runtime_state
    from trader.support.config import risk as config_risk

    assert "def read_min_trade_confidence" not in risk_source
    assert execution_risk.read_min_trade_confidence is config_risk.read_min_trade_confidence

    seen_paths: list[Path] = []

    def fake_read_min_trade_confidence(path: Path) -> float:
        seen_paths.append(path)
        return 0.61

    monkeypatch.setattr(runtime_state, "read_min_trade_confidence", fake_read_min_trade_confidence)
    monkeypatch.setattr(runtime_state, "_ROOT", tmp_path)

    assert runtime_state._read_min_trade_confidence_safe() == 0.61
    assert seen_paths == [tmp_path / "config" / "risk.yaml"]
    assert tui._read_min_trade_confidence_safe is runtime_state._read_min_trade_confidence_safe


def test_pure_planning_calculations_are_nested_under_domain_with_facades() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    domain_planning_dir = trader_dir / "domain" / "planning"

    expected_modules = {
        "__init__.py",
        "exit_engine.py",
        "exit_plan_spec.py",
        "relevance_gate.py",
        "scheduling.py",
        "watches.py",
    }
    assert {path.name for path in domain_planning_dir.glob("*.py")} == expected_modules

    for module_name in ("exit_engine", "exit_plan_spec", "relevance_gate"):
        facade_path = trader_dir / "planning" / f"{module_name}.py"
        assert facade_path.exists()
        source = facade_path.read_text(encoding="utf-8")
        assert f"from trader.domain.planning.{module_name} import *" in source

    indicator_watch_source = (trader_dir / "planning" / "indicator_watch.py").read_text(encoding="utf-8")
    assert "from trader.domain.planning.watches import is_armed_plan" in indicator_watch_source

    import trader.domain.planning.exit_engine as domain_exit_engine
    import trader.domain.planning.exit_plan_spec as domain_exit_plan_spec
    import trader.domain.planning.relevance_gate as domain_relevance_gate
    import trader.domain.planning.scheduling as domain_scheduling
    import trader.domain.planning.watches as domain_watches
    import trader.application.cycle.schedule as cycle_schedule
    import trader.planning.exit_engine as planning_exit_engine
    import trader.planning.exit_plan_spec as planning_exit_plan_spec
    import trader.planning.relevance_gate as planning_relevance_gate
    import trader.planning.scheduler as planning_scheduler
    from trader.planning.indicator_watch import is_armed_plan

    assert planning_exit_engine.evaluate_plan is domain_exit_engine.evaluate_plan
    assert planning_relevance_gate.symbol_needs_llm is domain_relevance_gate.symbol_needs_llm
    assert planning_exit_plan_spec.normalize_exit_plan is domain_exit_plan_spec.normalize_exit_plan
    assert planning_exit_plan_spec._positive_float is domain_exit_plan_spec._positive_float
    assert planning_scheduler.STALE_BACKOFF_BASE_MULTIPLIER == domain_scheduling.STALE_BACKOFF_BASE_MULTIPLIER
    assert planning_scheduler.STALE_BACKOFF_MAX_MINUTES == domain_scheduling.STALE_BACKOFF_MAX_MINUTES
    assert planning_scheduler.STALE_BACKOFF_MAX_STREAK == domain_scheduling.STALE_BACKOFF_MAX_STREAK
    assert cycle_schedule.stale_backoff_wake_minutes is domain_scheduling.stale_backoff_wake_minutes
    assert is_armed_plan is domain_watches.is_armed_plan

    forbidden_prefixes = (
        "trader.agent",
        "trader.application",
        "trader.execution",
        "trader.infrastructure",
        "trader.interfaces",
        "trader.market",
        "trader.planning",
        "trader.reporting",
        "trader.runtime",
        "trader.tools",
    )
    violations: list[str] = []
    for path in sorted(domain_planning_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(forbidden_prefixes):
                    violations.append(f"{rel_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_scheduler_json_backend_is_nested_under_state_db_with_planning_facade() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
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


def test_scheduler_consumers_depend_on_schedulerlike_not_planning_scheduler_class() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    checked_paths = [
        repo_root / "trader" / "runtime" / "cycle_scheduling.py",
        repo_root / "trader" / "runtime" / "daemon.py",
        repo_root / "trader" / "application" / "cycle" / "schedule.py",
        repo_root / "trader" / "application" / "cycle" / "watch_scanner.py",
        repo_root / "trader" / "application" / "decide" / "planner_batch.py",
        repo_root / "trader" / "application" / "execute" / "cycle_decision.py",
    ]

    violations: list[str] = []
    for module_path in checked_paths:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        rel_path = module_path.relative_to(repo_root)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "trader.planning.scheduler" and any(
                    alias.name == "Scheduler" for alias in node.names
                ):
                    violations.append(f"{rel_path}: from trader.planning.scheduler import Scheduler")
                if node.module == "trader.infrastructure.state_db.scheduler_json" and any(
                    alias.name == "Scheduler" for alias in node.names
                ):
                    violations.append(
                        f"{rel_path}: from trader.infrastructure.state_db.scheduler_json import Scheduler"
                    )
                if node.module == "trader.planning" and any(
                    alias.name == "scheduler" for alias in node.names
                ):
                    violations.append(f"{rel_path}: from trader.planning import scheduler")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {
                        "trader.infrastructure.state_db.scheduler_json",
                        "trader.planning.scheduler",
                    }:
                        violations.append(f"{rel_path}: import {alias.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations = [
                    *(arg.annotation for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]),
                    node.args.vararg.annotation if node.args.vararg else None,
                    node.args.kwarg.annotation if node.args.kwarg else None,
                    node.returns,
                ]
                for annotation in annotations:
                    if _annotation_mentions_scheduler_class(annotation):
                        violations.append(f"{rel_path}: {node.name} annotates Scheduler")
            elif isinstance(node, ast.AnnAssign):
                if _annotation_mentions_scheduler_class(node.annotation):
                    violations.append(f"{rel_path}: variable annotates Scheduler")

    assert violations == []


def test_schedulerlike_protocol_is_satisfied_by_json_and_sqlite_schedulers() -> None:
    from trader.infrastructure.state_db.scheduler_json import Scheduler
    from trader.infrastructure.state_db.scheduler_store import SqliteScheduler
    from trader.planning.protocols import SchedulerLike

    protocol_methods = {
        name
        for name, value in SchedulerLike.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert protocol_methods == {
        "active_indicator_watches",
        "clear_symbol_next_wake",
        "due_symbols",
        "get_stale_streak",
        "next_wake",
        "pop_expired_indicator_watches",
        "reconcile_universe",
        "remove_indicator_watch",
        "reset_stale_streak",
        "seconds_until_wake",
        "set_next_wake_in",
        "set_stale_streak",
        "set_symbol_indicator_watch",
        "set_symbol_next_wake",
        "set_symbol_next_wake_in",
    }
    assert protocol_methods <= {
        name for name, value in Scheduler.__dict__.items() if callable(value) and not name.startswith("_")
    }
    assert protocol_methods <= {
        name for name, value in SqliteScheduler.__dict__.items() if callable(value) and not name.startswith("_")
    }


def _annotation_mentions_scheduler_class(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "Scheduler":
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "Scheduler"
            and isinstance(node.value, ast.Name)
            and node.value.id == "scheduler"
        ):
            return True
    return False


def test_agent_learnings_are_nested_under_agent() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    learnings_dir = trader_dir / "agent" / "learnings"

    assert learnings_dir.exists()
    assert (learnings_dir / "raw_store.py").exists()
    assert (learnings_dir / "store.py").exists()
    assert (learnings_dir / "embeddings.py").exists()
    assert (learnings_dir / "consolidator.py").exists()
    assert (learnings_dir / "recall_provider.py").exists()
    assert not (trader_dir / "application" / "decide" / "learnings_recall.py").exists()
    assert not _has_python_sources(trader_dir / "learnings")


def test_rotation_is_nested_under_market() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    rotation_dir = trader_dir / "market" / "rotation"

    assert rotation_dir.exists()
    assert (rotation_dir / "core.py").exists()
    assert (rotation_dir / "venues.py").exists()
    assert (rotation_dir / "schedule.py").exists()
    assert (rotation_dir / "wiring.py").exists()
    assert not _has_python_sources(trader_dir / "rotation")


def test_universe_rotation_policies_are_canonical_domain_and_application_modules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    selection_path = repo_root / "trader" / "domain" / "universe" / "selection.py"
    candidate_scope_path = repo_root / "trader" / "domain" / "universe" / "candidate_scope.py"
    activation_path = repo_root / "trader" / "application" / "universe" / "activation.py"
    scope_rotation_path = repo_root / "trader" / "application" / "universe" / "scope_rotation.py"

    assert (
        _domain_import_violations(
            [selection_path, candidate_scope_path],
            repo_root,
        )
        == []
    )

    for application_path in (activation_path, scope_rotation_path):
        application_source = application_path.read_text(encoding="utf-8")
        assert "trader.runtime" not in application_source
        assert "trader.infrastructure" not in application_source
        assert "trader.market" not in application_source

    from trader.application.universe.activation import validate_prepared_hotlist
    from trader.application.universe.scope_rotation import (
        refresh_preopen_candidate_scope,
        update_venue_ranking,
    )
    from trader.domain.universe.selection import (
        apply_hysteresis,
        compose_active_universe,
    )
    from trader.market.rotation import apply_hysteresis as legacy_apply_hysteresis
    from trader.market.rotation.venues import (
        compose_active_universe as legacy_compose_active_universe,
    )
    from trader.market.rotation.venues import (
        validate_prepared_hotlist as legacy_validate_prepared_hotlist,
    )
    from trader.market.rotation.venues import (
        refresh_preopen_candidate_scope as legacy_refresh_preopen_candidate_scope,
    )
    from trader.market.rotation.venues import (
        update_venue_ranking as legacy_update_venue_ranking,
    )

    assert legacy_apply_hysteresis is apply_hysteresis
    assert legacy_compose_active_universe is compose_active_universe
    assert legacy_validate_prepared_hotlist is validate_prepared_hotlist
    assert legacy_refresh_preopen_candidate_scope is refresh_preopen_candidate_scope
    assert legacy_update_venue_ranking is update_venue_ranking


def test_universe_filesystem_adapters_are_canonical_with_rotation_facades() -> None:
    repo_root = Path(__file__).resolve().parents[1]
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


def test_daemon_agent_context_and_process_state_are_runtime_canonical() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    runtime_dir = repo_root / "trader" / "runtime"
    daemon_path = runtime_dir / "daemon.py"

    assert (runtime_dir / "agent_cycle_context.py").exists()
    assert (runtime_dir / "cycle_process_state.py").exists()

    daemon_tree = ast.parse(
        daemon_path.read_text(encoding="utf-8"), filename=str(daemon_path)
    )
    daemon_definitions = {
        node.name
        for node in ast.walk(daemon_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert daemon_definitions.isdisjoint(
        {
            "CycleProcessState",
            "_global_plans_summary",
            "_plan_to_context_dict",
            "_build_base_context",
        }
    )

    from trader.runtime import daemon
    from trader.runtime.agent_cycle_context import (
        build_base_context,
        global_plans_summary,
        plan_to_context_dict,
    )
    from trader.runtime.cycle_process_state import CycleProcessState

    assert daemon._build_base_context is build_base_context
    assert daemon._global_plans_summary is global_plans_summary
    assert daemon._plan_to_context_dict is plan_to_context_dict
    assert daemon.CycleProcessState is CycleProcessState


def test_daemon_delegates_watch_schedule_glue_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "cycle_scheduling.py"

    assert adapter_path.exists()

    tree = ast.parse(daemon_path.read_text(encoding="utf-8"), filename=str(daemon_path))
    forbidden = {"cycle_schedule", "watch_scanner"}
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "trader.application":
            continue
        for alias in node.names:
            if alias.name in forbidden:
                violations.append(alias.name)

    assert violations == []


def test_daemon_delegates_planned_exit_logic_to_application_service() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    planned_exits_path = repo_root / "trader" / "application" / "exit" / "planned_exits.py"

    assert planned_exits_path.exists()

    tree = ast.parse(daemon_path.read_text(encoding="utf-8"), filename=str(daemon_path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "trader.planning.exit_engine":
            continue
        for alias in node.names:
            if alias.name == "evaluate_plan":
                violations.append(alias.name)

    assert violations == []


def test_daemon_delegates_execute_queue_dispatch_to_application_service() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"

    tree = ast.parse(daemon_path.read_text(encoding="utf-8"), filename=str(daemon_path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "enqueue"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "execute_ledger"
        ):
            violations.append("daemon.py: execute_ledger.enqueue(...)")

    assert violations == []


def test_daemon_delegates_decision_dispatch_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "decision_dispatch_runtime.py"

    assert adapter_path.exists()
    adapter_source = adapter_path.read_text(encoding="utf-8")
    assert "trader.infrastructure" not in adapter_source

    daemon_source = daemon_path.read_text(encoding="utf-8")
    assert "decision_dispatch_runtime.dispatch_decisions" in daemon_source
    assert "iter_decide_results_via_queue" not in daemon_source
    assert "planner_batch.batch_decide" not in daemon_source
    assert "build_symbol_facts" not in daemon_source


def test_daemon_delegates_queue_pool_bootstrap_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "queue_runtime.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "queue_runtime.start_queue_runtimes" in source

    tree = ast.parse(source, filename=str(daemon_path))
    forbidden_modules = {
        "trader.application.decide.handler",
        "trader.application.execute.order_handler",
        "trader.infrastructure.queue.decide_pool",
        "trader.infrastructure.queue.ledger",
        "trader.infrastructure.queue.pools",
        "trader.infrastructure.state_db.broker_store",
        "trader.infrastructure.state_db.connection",
        "trader.infrastructure.state_db.trade_plan_store",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
            violations.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    violations.append(f"import {alias.name}")

    assert violations == []


def test_state_db_trade_plan_store_imports_domain_not_planning() -> None:
    repo_root = Path(__file__).resolve().parents[1]
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
    repo_root = Path(__file__).resolve().parents[1]
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
                    f"{module_path.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name == forbidden
                )

    assert violations == []


def test_infrastructure_imports_stale_backoff_policy_from_domain_not_scheduler_facade() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    infrastructure_dir = repo_root / "trader" / "infrastructure"
    forbidden = "trader.planning.scheduler"
    violations: list[str] = []

    for module_path in infrastructure_dir.rglob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        rel_path = module_path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == forbidden:
                stale_imports = [
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("STALE_BACKOFF_")
                ]
                if stale_imports:
                    violations.append(
                        f"{rel_path}: from {forbidden} import {', '.join(stale_imports)}"
                    )

    assert violations == []


def test_execute_order_handler_is_infrastructure_canonical_with_application_facade() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    facade_path = repo_root / "trader" / "application" / "execute" / "order_handler.py"
    adapter_path = repo_root / "trader" / "infrastructure" / "queue" / "order_handler.py"
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

    facade_tree = ast.parse(
        facade_path.read_text(encoding="utf-8"),
        filename=str(facade_path),
    )
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef))
        for node in facade_tree.body
    )

    from trader.application.execute.order_handler import (
        make_execute_order_handler as legacy_make_execute_order_handler,
    )
    from trader.infrastructure.queue.order_handler import make_execute_order_handler

    assert legacy_make_execute_order_handler is make_execute_order_handler


def test_retryable_error_is_application_contract_with_worker_facade() -> None:
    from trader.application.queue.contracts import RetryableError
    from trader.infrastructure.queue.worker import RetryableError as worker_retryable_error
    from trader.queue.worker import RetryableError as legacy_retryable_error

    assert worker_retryable_error is RetryableError
    assert legacy_retryable_error is RetryableError


def test_migrations_own_legacy_trade_plan_decoder() -> None:
    repo_root = Path(__file__).resolve().parents[1]
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


def test_daemon_delegates_data_source_bootstrap_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "data_source_runtime.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "data_source_runtime.load_data_source_config" in source
    assert "data_source_runtime.build_data_source" in source
    assert "data_source_runtime.maybe_attach_ib" in source
    assert "data_source_runtime.detach_failed_ib" in source

    tree = ast.parse(source, filename=str(daemon_path))
    forbidden_calls = {
        "CompositeDataSource",
        "IBAttachBackoff",
        "IBDataSource",
        "YFinanceDataSource",
        "connect_ib",
        "parse_data_sources_config",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
            violations.append(f"{node.func.id}(...)")

    assert violations == []


def test_daemon_delegates_market_rotation_tick_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "market_rotation_runtime.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "market_rotation_runtime.tick_market_rotation" in source

    tree = ast.parse(source, filename=str(daemon_path))
    forbidden_modules = {
        "trader.market.radar_config",
        "trader.market.rotation",
        "trader.market.rotation.venues",
        "trader.market.rotation.wiring",
    }

    def _is_forbidden_module(module: str) -> bool:
        return module in forbidden_modules or any(
            module.startswith(f"{forbidden}.") for forbidden in forbidden_modules
        )

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
            violations.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name):
                    violations.append(f"import {alias.name}")

    assert violations == []


def test_daemon_delegates_state_bootstrap_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "daemon_bootstrap.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "daemon_bootstrap.bootstrap_runtime_state" in source

    tree = ast.parse(source, filename=str(daemon_path))
    main_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"]
    assert len(main_nodes) == 1
    main_tree = main_nodes[0]
    forbidden_calls = {
        "bootstrap_state_backend",
        "load_starting_cash",
        "make_scheduler",
        "rotate_monthly",
    }
    violations: list[str] = []
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                violations.append(f"{node.func.id}(...)")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_calls:
                violations.append(f"{node.func.attr}(...)")
        elif isinstance(node, ast.ImportFrom) and node.module == "trader.runtime":
            for alias in node.names:
                if alias.name == "ledger_rotation":
                    violations.append("from trader.runtime import ledger_rotation")

    assert violations == []


def test_daemon_delegates_run_cycle_call_to_runtime_dispatcher() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "cycle_dispatch.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "cycle_dispatch.dispatch_run_cycle" in source

    tree = ast.parse(source, filename=str(daemon_path))
    main_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"]
    assert len(main_nodes) == 1
    main_tree = main_nodes[0]
    violations: list[str] = []
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_cycle":
            violations.append("run_cycle(...)")

    assert violations == []


def test_daemon_delegates_cycle_report_persistence_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "cycle_reporting.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "cycle_reporting.persist_cycle_report" in source

    tree = ast.parse(source, filename=str(daemon_path))
    main_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"]
    assert len(main_nodes) == 1
    main_tree = main_nodes[0]
    violations: list[str] = []
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Constant) and node.value == "last_report.json":
            violations.append("last_report.json")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_append_cycle_history":
            violations.append("_append_cycle_history(...)")

    assert violations == []


def test_daemon_delegates_shutdown_to_runtime_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "runtime_shutdown.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "runtime_shutdown.shutdown_runtime_resources" in source

    tree = ast.parse(source, filename=str(daemon_path))
    main_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"]
    assert len(main_nodes) == 1
    main_tree = main_nodes[0]
    violations: list[str] = []
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "stop":
            violations.append(".stop()")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "release_pid_file":
            violations.append("release_pid_file(...)")

    assert violations == []


def test_cycle_decision_delegates_execute_queue_plan_payload_to_application_service() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cycle_decision_path = repo_root / "trader" / "application" / "execute" / "cycle_decision.py"
    service_path = repo_root / "trader" / "application" / "execute" / "queue_plan.py"

    assert cycle_decision_path.exists()
    assert service_path.exists()

    source = cycle_decision_path.read_text(encoding="utf-8")
    assert "queue_plan.build_execute_queue_plan_payload" in source

    queue_block = source.split("if ctx.queue_execute_enabled and ctx.execute_ledger is not None:", 1)[1]
    queue_block = queue_block.split("fill = _exec_outcome.fill", 1)[0]
    forbidden = (
        "create_trade_plan_from_order(",
        "_projected_scale_in_risk_basis(",
        "_flip_open_quantity(",
    )
    violations = [call for call in forbidden if call in queue_block]

    assert violations == []


def test_cycle_decision_delegates_fill_accounting_to_application_service() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cycle_decision_path = repo_root / "trader" / "application" / "execute" / "cycle_decision.py"
    service_path = repo_root / "trader" / "application" / "execute" / "fill_outcome.py"

    assert cycle_decision_path.exists()
    assert service_path.exists()

    source = cycle_decision_path.read_text(encoding="utf-8")
    assert "fill_outcome.build_fill_accounting" in source

    post_fill_block = source.split("fill_accounting = fill_outcome.build_fill_accounting", 1)[1]
    post_fill_block = post_fill_block.split("if fill is not None and decision.intent in", 1)[0]
    forbidden = (
        'entry["model_performance_logged"]',
        'entry["commission"]',
        'entry["commission_currency"]',
        'entry["commission_model"]',
        'entry["fx_rate"]',
        "commission_currency=fill.commission_currency",
    )
    violations = [snippet for snippet in forbidden if snippet in post_fill_block]

    assert violations == []
    append_index = post_fill_block.index("ctx.append_model_performance(**fill_accounting.model_performance)")
    update_index = post_fill_block.index("entry.update(fill_accounting.entry_updates)")
    assert append_index < update_index


def test_cycle_decision_delegates_fill_plan_effects_to_application_service() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cycle_decision_path = repo_root / "trader" / "application" / "execute" / "cycle_decision.py"
    service_path = repo_root / "trader" / "application" / "exit" / "fill_plan_effects.py"

    assert cycle_decision_path.exists()
    assert service_path.exists()

    source = cycle_decision_path.read_text(encoding="utf-8")
    assert "fill_plan_effects.apply_filled_plan_effects" in source
    assert "def _create_plan_for_final_position" not in source

    post_fill_block = source.split("ctx.append_model_performance(**fill_accounting.model_performance)", 1)[1]
    post_fill_block = post_fill_block.split("if fill is not None and decision.intent in _OPENING_INTENTS:", 1)[0]
    forbidden = (
        "plan_store.close_symbol(sym)",
        "plan_store.sync_symbol_quantity(",
        "create_trade_plan_from_order(",
        'entry["trade_plan_created"]',
        'entry["trade_plan"]',
    )
    violations = [snippet for snippet in forbidden if snippet in post_fill_block]

    assert violations == []


def test_decision_reason_vocabulary_is_domain_canonical() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    canonical_path = repo_root / "trader" / "domain" / "decision_reason.py"
    reporting_root = repo_root / "trader" / "reporting"
    reporting_facade = repo_root / "trader" / "reporting" / "decision_reason.py"

    assert canonical_path.exists()
    assert reporting_facade.exists()

    violations: list[str] = []
    for path in sorted((repo_root / "trader").rglob("*.py")):
        if "__pycache__" in path.parts or path == reporting_facade:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "trader.reporting":
                for alias in node.names:
                    if alias.name == "decision_reason":
                        violations.append(f"{rel_path}: from trader.reporting import decision_reason")
            elif isinstance(node, ast.ImportFrom) and node.module == "trader.reporting.decision_reason":
                violations.append(f"{rel_path}: from trader.reporting.decision_reason import ...")
            elif isinstance(node, ast.ImportFrom) and node.level and reporting_root in path.parents:
                if node.module == "decision_reason":
                    violations.append(f"{rel_path}: from .decision_reason import ...")
                elif node.module is None:
                    for alias in node.names:
                        if alias.name == "decision_reason":
                            violations.append(f"{rel_path}: from . import decision_reason")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "trader.reporting.decision_reason":
                        violations.append(f"{rel_path}: import trader.reporting.decision_reason")

    assert violations == []


def test_foundation_packages_do_not_depend_on_higher_layers() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    foundation_roots = (trader_dir / "domain", trader_dir / "support")

    violations: list[str] = []
    for root in foundation_roots:
        root_package = root.name
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trader."):
                    imported_package = node.module.split(".")[1]
                    if imported_package != root_package:
                        violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if not alias.name.startswith("trader."):
                            continue
                        imported_package = alias.name.split(".")[1]
                        if imported_package != root_package:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_legacy_tools_package_is_virtual_compatibility_layer(monkeypatch) -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    assert not (trader_dir / "tools").exists()

    import trader.tools as legacy_tools
    from trader.market import market_data as canonical_market
    from trader.tools import market as legacy_market
    from trader.tools.market import Bar as LegacyBar

    assert getattr(legacy_tools, "__file__", None) is None
    assert getattr(legacy_tools, "__path__", None) == []
    assert legacy_tools.market is legacy_market
    assert LegacyBar is canonical_market.Bar

    sentinel = object()
    monkeypatch.setattr(legacy_market, "_compat_probe", sentinel, raising=False)

    assert canonical_market._compat_probe is sentinel


def test_legacy_tools_virtual_modules_preserve_explicit_star_exports() -> None:
    import trader.tools.execution as legacy_execution
    import trader.tools.scheduler as legacy_scheduler

    assert legacy_execution.__all__ == [
        "Broker",
        "Commission",
        "CommissionModel",
        "CommissionModelName",
        "Fill",
        "IbkrCommissionModel",
        "NoCommissionModel",
        "Order",
        "Position",
        "Side",
        "SimBroker",
        "commission_model_from_name",
        "compute_fill_effect",
        "round_trip_cost",
    ]
    assert legacy_scheduler.__all__ == [
        "STALE_BACKOFF_BASE_MULTIPLIER",
        "STALE_BACKOFF_MAX_MINUTES",
        "STALE_BACKOFF_MAX_STREAK",
        "Scheduler",
    ]


def test_legacy_agent_packages_are_virtual_compatibility_layers(monkeypatch) -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    for legacy_dir in ("agent_protocol", "agent_tools"):
        assert not (trader_dir / legacy_dir).exists()

    import trader.agent_protocol as legacy_protocol
    import trader.agent_tools as legacy_tools
    from trader.domain.decisions import IndicatorRequest as DomainIndicatorRequest
    from trader.agent.protocol.parsing import parse_batch
    from trader.agent.protocol import IndicatorRequest
    from trader.agent.tools import core, registry
    from trader.agent_protocol.parsing import parse_batch as legacy_parse_batch
    from trader.agent_tools.core import ToolContext as LegacyToolContext

    assert getattr(legacy_protocol, "__file__", None) is None
    assert getattr(legacy_protocol, "__path__", None) == []
    assert getattr(legacy_tools, "__file__", None) is None
    assert getattr(legacy_tools, "__path__", None) == []
    assert IndicatorRequest is DomainIndicatorRequest
    assert legacy_parse_batch is parse_batch
    assert legacy_tools.TOOL_REGISTRY is registry.TOOL_REGISTRY
    assert LegacyToolContext is core.ToolContext

    sentinel = object()
    monkeypatch.setattr(legacy_tools.core, "_compat_probe", sentinel, raising=False)

    assert core._compat_probe is sentinel


def test_universe_pipeline_read_model_is_canonical_runtime_state_dependency() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    read_models_dir = repo_root / "trader" / "reporting" / "read_models"
    canonical_path = read_models_dir / "universe_pipeline.py"
    assembler_path = read_models_dir / "runtime_state.py"

    assert canonical_path.exists()

    assembler_tree = ast.parse(
        assembler_path.read_text(encoding="utf-8"), filename=str(assembler_path)
    )
    assembler_definitions = {
        node.name
        for node in ast.walk(assembler_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert assembler_definitions.isdisjoint(
        {
            "_read_projection_safe",
            "_latest_activations_by_venue_safe",
            "_scope_pipeline_entry",
            "_scout_pipeline_entry",
            "_brief_pipeline_entry",
            "_agent_pipeline_entry",
            "_activation_pipeline_entry",
        }
    )

    from trader.reporting.read_models import runtime_state
    from trader.reporting.read_models.universe_pipeline import load_universe_pipeline

    assert runtime_state._load_universe_pipeline_safe is load_universe_pipeline


def test_support_and_read_model_legacy_packages_are_virtual() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    for legacy_dir in ("config", "metadata", "read_models", "system"):
        assert not (trader_dir / legacy_dir).exists()

    from trader.config import pool as legacy_pool
    from trader.config.portfolio import load_starting_cash as legacy_load_starting_cash
    from trader.metadata import code_version as legacy_code_version
    from trader.read_models import runtime_state as legacy_runtime_state
    from trader.read_models.live_kpis import compute_live_kpis as legacy_compute_live_kpis
    from trader.support.config import pool as support_pool
    from trader.support.config.portfolio import load_starting_cash
    from trader.support.metadata import code_version
    from trader.support.system.process_env import sanitized_runtime_env
    from trader.system.process_env import sanitized_runtime_env as legacy_sanitized_runtime_env
    from trader.reporting.read_models import runtime_state
    from trader.reporting.read_models.live_kpis import compute_live_kpis

    assert getattr(__import__("trader.config").config, "__path__", None) == []
    assert legacy_pool.load_pool is support_pool.load_pool
    assert legacy_load_starting_cash is load_starting_cash
    assert legacy_code_version.current_code_version is code_version.current_code_version
    assert legacy_sanitized_runtime_env is sanitized_runtime_env
    assert legacy_runtime_state.load_runtime_state is runtime_state.load_runtime_state
    assert legacy_compute_live_kpis is compute_live_kpis


def test_cockpit_universe_projection_is_canonical_and_textual_free() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    projection_path = cockpit_dir / "projections" / "universe.py"
    page_path = cockpit_dir / "pages" / "universe.py"

    assert projection_path.exists()
    projection_source = projection_path.read_text(encoding="utf-8")
    assert "textual" not in projection_source
    assert "rich." not in projection_source

    page_source = page_path.read_text(encoding="utf-8")
    page_tree = ast.parse(page_source, filename=str(page_path))
    page_definitions = {
        node.name
        for node in ast.walk(page_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert page_definitions.isdisjoint(
        {"SymbolRowData", "build_symbol_rows", "_venue_of_safe"}
    )

    populate = next(
        node
        for node in page_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_populate_universe_table"
    )
    populate_calls = {
        node.func.id
        for node in ast.walk(populate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_symbol_rows" in populate_calls
    assert "_safe_float" not in populate_calls

    from trader.interfaces.cockpit.pages import universe as page
    from trader.interfaces.cockpit.projections.universe import (
        SymbolRowData,
        build_symbol_rows,
        venue_of_safe,
    )

    assert page.SymbolRowData is SymbolRowData
    assert page.build_symbol_rows is build_symbol_rows
    assert page._venue_of_safe is venue_of_safe


def test_cockpit_decision_projection_is_canonical_and_textual_free() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    projection_path = cockpit_dir / "projections" / "decisions.py"
    page_path = cockpit_dir / "pages" / "decisions.py"

    assert projection_path.exists()
    projection_source = projection_path.read_text(encoding="utf-8")
    assert "textual" not in projection_source
    assert "rich." not in projection_source

    page_source = page_path.read_text(encoding="utf-8")
    page_tree = ast.parse(page_source, filename=str(page_path))
    page_definitions = {
        node.name
        for node in ast.walk(page_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert page_definitions.isdisjoint({"_fmt_conf", "_has_detail"})
    assert "_count_filters" not in page_source
    assert "_filter_rows" not in page_source
    assert "_group_into_ledger_rows" not in page_source

    populate = next(
        node
        for node in page_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "populate_ledger_table"
    )
    populate_calls = {
        node.func.id
        for node in ast.walk(populate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_ledger_rows" in populate_calls

    refresh = next(
        node
        for node in ast.walk(page_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_refresh_ledger"
    )
    refresh_calls = {
        node.func.id
        for node in ast.walk(refresh)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "project_decision_ledger" in refresh_calls

    from trader.interfaces.cockpit.pages import decisions as page
    from trader.interfaces.cockpit.projections.decisions import (
        format_confidence,
        has_decision_detail,
    )
    from trader.reporting.read_models import decision_filters

    assert page._fmt_conf is format_confidence
    assert page._has_detail is has_decision_detail
    assert decision_filters._count_filters is decision_filters.count_filters
    assert decision_filters._filter_rows is decision_filters.filter_rows
    assert (
        decision_filters._group_into_ledger_rows
        is decision_filters.group_into_ledger_rows
    )


def test_cockpit_uses_support_coercion_instead_of_private_reporting_helpers() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    coercion_path = repo_root / "trader" / "support" / "coercion.py"
    runtime_state_path = (
        repo_root / "trader" / "reporting" / "read_models" / "runtime_state.py"
    )

    assert coercion_path.exists()
    violations: list[str] = []
    for path in sorted(cockpit_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "trader.reporting.read_models.runtime_state":
                continue
            private_names = [
                alias.name for alias in node.names if alias.name.startswith("_safe_")
            ]
            if private_names:
                violations.append(
                    f"{path.relative_to(repo_root)}: {', '.join(private_names)}"
                )
    assert violations == []

    runtime_tree = ast.parse(
        runtime_state_path.read_text(encoding="utf-8"),
        filename=str(runtime_state_path),
    )
    runtime_definitions = {
        node.name
        for node in runtime_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_safe_float" not in runtime_definitions
    assert "_safe_list_of_dicts" not in runtime_definitions

    from trader.reporting.read_models import runtime_state
    from trader.support.coercion import dict_list, finite_float

    assert runtime_state._safe_float is finite_float
    assert runtime_state._safe_list_of_dicts is dict_list


def test_operator_interface_legacy_packages_are_virtual() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    for legacy_dir in ("commands", "cockpit", "ui"):
        assert not _has_python_sources(trader_dir / legacy_dir)

    import trader.cockpit as legacy_cockpit
    import trader.commands as legacy_commands
    import trader.ui as legacy_ui
    from trader.commands import stats as legacy_stats
    from trader.interfaces.cli import stats
    from trader.interfaces.cockpit.app import CockpitApp
    from trader.interfaces.ui import palette
    from trader.ui import palette as legacy_palette

    assert getattr(legacy_cockpit, "__file__", None) is None
    assert getattr(legacy_cockpit, "__path__", None) == []
    assert getattr(legacy_commands, "__file__", None) is None
    assert getattr(legacy_commands, "__path__", None) == []
    assert getattr(legacy_ui, "__file__", None) is None
    assert getattr(legacy_ui, "__path__", None) == []
    assert legacy_cockpit.CockpitApp is CockpitApp
    assert legacy_stats.main is stats.main
    assert legacy_palette.PALETTE_LIGHT is palette.PALETTE_LIGHT


def test_infrastructure_legacy_packages_are_virtual() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    for legacy_dir in ("queue", "state_db"):
        assert not _has_python_sources(trader_dir / legacy_dir)

    import trader.queue as legacy_queue
    import trader.state_db as legacy_state_db
    from trader.infrastructure.queue.ledger import TaskLedger
    from trader.infrastructure.state_db.connection import StateDb
    from trader.queue.ledger import TaskLedger as LegacyTaskLedger
    from trader.state_db.connection import StateDb as LegacyStateDb

    assert getattr(legacy_queue, "__file__", None) is None
    assert getattr(legacy_queue, "__path__", None) == []
    assert getattr(legacy_state_db, "__file__", None) is None
    assert getattr(legacy_state_db, "__path__", None) == []
    assert LegacyTaskLedger is TaskLedger
    assert LegacyStateDb is StateDb


def test_semantic_legacy_package_is_virtual() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    assert not _has_python_sources(trader_dir / "semantic")

    import trader.semantic as legacy_semantic
    import trader.semantic.catalog as legacy_catalog
    from trader.domain.semantic import catalog
    from trader.semantic.catalog import family_for_symbol as legacy_family_for_symbol

    assert getattr(legacy_semantic, "__file__", None) is None
    assert getattr(legacy_semantic, "__path__", None) == []
    assert legacy_catalog.family_for_symbol is catalog.family_for_symbol
    assert legacy_family_for_symbol is catalog.family_for_symbol


def test_scheduling_legacy_package_is_virtual() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    assert not _has_python_sources(trader_dir / "scheduling")

    import trader.scheduling as legacy_scheduling
    import trader.scheduling.scheduler as legacy_scheduler
    from trader.planning.scheduler import Scheduler
    from trader.scheduling.scheduler import Scheduler as LegacyScheduler

    assert getattr(legacy_scheduling, "__file__", None) is None
    assert getattr(legacy_scheduling, "__path__", None) == []
    assert legacy_scheduler.Scheduler is Scheduler
    assert LegacyScheduler is Scheduler


def test_learnings_legacy_package_is_virtual() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    assert not _has_python_sources(trader_dir / "learnings")

    import trader.learnings as legacy_learnings
    import trader.learnings.raw_store as legacy_raw_store
    from trader.agent.learnings.raw_store import RawLearningsStore
    from trader.learnings.raw_store import RawLearningsStore as LegacyRawLearningsStore

    assert getattr(legacy_learnings, "__file__", None) is None
    assert getattr(legacy_learnings, "__path__", None) == []
    assert legacy_raw_store.RawLearningsStore is RawLearningsStore
    assert LegacyRawLearningsStore is RawLearningsStore


def test_rotation_legacy_package_is_virtual() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    assert not _has_python_sources(trader_dir / "rotation")

    import trader.rotation as legacy_rotation
    import trader.rotation.schedule as legacy_schedule
    from trader.market.rotation import run
    from trader.market.rotation.schedule import rotation_due
    from trader.rotation import run as legacy_run
    from trader.rotation.schedule import rotation_due as legacy_rotation_due

    assert getattr(legacy_rotation, "__file__", None) is None
    assert getattr(legacy_rotation, "__path__", None) == []
    assert legacy_run is run
    assert legacy_schedule.rotation_due is rotation_due
    assert legacy_rotation_due is rotation_due

    namespace: dict[str, object] = {}
    exec("from trader.rotation import *", namespace)
    exported = {name for name in namespace if not name.startswith("__")}
    assert exported == set(legacy_rotation.__all__)
    assert {"run", "apply_hysteresis", "CoverageError"} <= exported
    assert {"Any", "import_module", "_CORE_EXPORTS"}.isdisjoint(exported)


def test_rotation_python_m_core_smoke_has_no_preimport_warning() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    for module_name in ("trader.market.rotation.core", "trader.rotation.core"):
        result = subprocess.run(
            [sys.executable, "-m", module_name, "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, f"{module_name}: {result.stderr}"
        assert "RuntimeWarning" not in result.stderr


def test_internal_code_uses_canonical_interface_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    checked_roots = (trader_dir, repo_root / "scripts")
    ignored_files = {trader_dir / "__init__.py"}
    forbidden_direct_modules = {
        "trader.cockpit",
        "trader.commands",
        "trader.ui",
    }
    forbidden_from_trader = {"cockpit", "commands", "ui"}

    def _is_forbidden_module(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_direct_modules
        )

    violations: list[str] = []
    for root in checked_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path in ignored_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_trader)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_forbidden_module(alias.name):
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_internal_code_uses_canonical_infrastructure_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    checked_roots = (trader_dir, repo_root / "scripts")
    ignored_files = {trader_dir / "__init__.py"}
    forbidden_direct_modules = {
        "trader.queue",
        "trader.state_db",
    }
    forbidden_from_trader = {"queue", "state_db"}

    def _is_forbidden_module(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_direct_modules
        )

    violations: list[str] = []
    for root in checked_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path in ignored_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_trader)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_forbidden_module(alias.name):
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_internal_code_uses_canonical_semantic_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    checked_roots = (trader_dir, repo_root / "scripts")
    ignored_files = {trader_dir / "__init__.py"}
    forbidden_direct_modules = {"trader.semantic"}
    forbidden_from_trader = {"semantic"}

    def _is_forbidden_module(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_direct_modules
        )

    violations: list[str] = []
    for root in checked_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path in ignored_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_trader)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_forbidden_module(alias.name):
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_internal_code_uses_canonical_scheduling_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    checked_roots = (trader_dir, repo_root / "scripts")
    ignored_files = {trader_dir / "__init__.py"}
    forbidden_direct_modules = {"trader.scheduling"}
    forbidden_from_trader = {"scheduling"}

    def _is_forbidden_module(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_direct_modules
        )

    violations: list[str] = []
    for root in checked_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path in ignored_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_trader)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_forbidden_module(alias.name):
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_internal_code_uses_canonical_learnings_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    checked_roots = (trader_dir, repo_root / "scripts")
    ignored_files = {trader_dir / "__init__.py"}
    forbidden_direct_modules = {"trader.learnings"}
    forbidden_from_trader = {"learnings"}

    def _is_forbidden_module(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_direct_modules
        )

    violations: list[str] = []
    for root in checked_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path in ignored_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_trader)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_forbidden_module(alias.name):
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_internal_code_uses_canonical_rotation_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    checked_roots = (trader_dir, repo_root / "scripts")
    ignored_files = {trader_dir / "__init__.py"}
    forbidden_direct_modules = {"trader.rotation"}
    forbidden_from_trader = {"rotation"}

    def _is_forbidden_module(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_direct_modules
        )

    violations: list[str] = []
    for root in checked_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path in ignored_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_trader)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_forbidden_module(alias.name):
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_legacy_virtual_packages_support_from_trader_and_python_m() -> None:
    import trader
    from trader import config as legacy_config

    assert trader.config is legacy_config

    repo_root = Path(__file__).resolve().parents[1]
    for module_name in (
        "trader.agent_protocol.parsing",
        "trader.agent_protocol.prompts",
        "trader.agent_tools.core",
        "trader.agent_tools.registry",
        "trader.tools.memory",
        "trader.config.pool",
        "trader.config.portfolio",
        "trader.metadata.code_version",
        "trader.system.process_env",
        "trader.read_models.live_kpis",
        "trader.read_models.runtime_state",
        "trader.semantic.catalog",
        "trader.scheduling.scheduler",
        "trader.tools.scheduler",
        "trader.learnings.raw_store",
        "trader.learnings.store",
        "trader.learnings.embeddings",
        "trader.learnings.consolidator",
        "trader.rotation",
        "trader.rotation.bench",
        "trader.rotation.collectors",
        "trader.rotation.core",
        "trader.rotation.daemon",
        "trader.rotation.ledger",
        "trader.rotation.override",
        "trader.rotation.schedule",
        "trader.rotation.state",
        "trader.rotation.venues",
        "trader.rotation.wiring",
    ):
        result = subprocess.run(
            [sys.executable, "-m", module_name],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, f"{module_name}: {result.stderr}"


def test_legacy_flat_module_imports_remain_compatible() -> None:
    import trader.cockpit_events as legacy_cockpit_events
    import trader.codex_client as legacy_codex_client
    import trader.consolidator as legacy_consolidator
    import trader.daemon as legacy_daemon
    import trader.embeddings as legacy_embeddings
    import trader.fx as legacy_fx
    import trader.learnings_store as legacy_learnings_store
    import trader.llm as legacy_llm
    import trader.palette as legacy_palette
    import trader.rotation_schedule as legacy_schedule
    import trader.stats as legacy_stats
    from trader.cockpit import CockpitApp
    from trader import decision_ledger
    from trader.domain.decisions import Decision as DomainDecision
    from trader.indicator_watch import WATCH_VALID_OPERATORS
    from trader.risk import RiskGate
    from trader.tui import build_view

    assert legacy_cockpit_events.__name__ == "trader.interfaces.cockpit.events"
    assert legacy_codex_client.Decision is DomainDecision
    assert legacy_consolidator.__name__ == "trader.agent.learnings.consolidator"
    assert legacy_daemon.run_cycle.__module__ == "trader.runtime.daemon"
    assert legacy_embeddings.__name__ == "trader.agent.learnings.embeddings"
    assert legacy_fx.__name__ == "trader.market.fx"
    assert legacy_learnings_store.__name__ == "trader.agent.learnings.store"
    assert legacy_llm.LlmRouter.__module__ == "trader.domain.llm"
    assert legacy_palette.__name__ == "trader.interfaces.ui.palette"
    assert legacy_schedule.__name__ == "trader.market.rotation.schedule"
    assert legacy_stats.compute_live_kpis.__module__ == "trader.reporting.stats"
    assert decision_ledger.__name__ == "trader.reporting.decision_ledger"
    assert CockpitApp.__module__ == "trader.interfaces.cockpit.app"
    assert ">" in WATCH_VALID_OPERATORS
    assert RiskGate.__module__ == "trader.execution.risk"
    assert build_view.__module__ == "trader.interfaces.ui.panels.dashboard"


def test_legacy_flat_modules_are_virtual_compatibility_layers() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    for legacy_module in ("attribution", "cli", "daemon", "stats", "tool_usage", "tui"):
        assert not (trader_dir / f"{legacy_module}.py").exists()

    import trader.attribution as legacy_attribution
    import trader.cli as legacy_cli
    import trader.daemon as legacy_daemon
    import trader.stats as legacy_stats
    import trader.tool_usage as legacy_tool_usage
    import trader.tui as legacy_tui
    from trader.interfaces.cli import attribution as attribution_cli
    from trader.interfaces.cli import stats as stats_cli
    from trader.interfaces.cli import tool_usage as tool_usage_cli
    from trader.interfaces.ui import tui
    from trader.reporting import attribution, stats, tool_usage
    from trader.runtime import cli, daemon

    assert legacy_attribution.compute_attribution is attribution.compute_attribution
    assert legacy_attribution.main is attribution_cli.main
    assert legacy_cli.main is cli.main
    assert legacy_daemon.run_cycle is daemon.run_cycle
    assert legacy_stats.compute_live_kpis is stats.compute_live_kpis
    assert legacy_stats.main is stats_cli.main
    assert legacy_tool_usage.build_report is tool_usage.build_report
    assert legacy_tool_usage.main is tool_usage_cli.main
    assert legacy_tui.build_view is tui.build_view
    assert legacy_tui.main is tui.main


def test_legacy_daemon_and_cli_packages_proxy_mutations(monkeypatch, tmp_path) -> None:
    import trader.cli as legacy_cli
    import trader.daemon as legacy_daemon
    from trader.runtime import cli as runtime_cli
    from trader.runtime import daemon as runtime_daemon

    monkeypatch.setattr(legacy_daemon, "STATE_DIR", tmp_path)

    assert runtime_daemon.STATE_DIR == tmp_path
    assert legacy_cli.daemon is runtime_cli.daemon


def test_legacy_daemon_and_cli_python_m_entrypoints() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for module_name in ("trader.daemon", "trader.cli"):
        result = subprocess.run(
            [sys.executable, "-m", module_name, "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_internal_backtest_scripts_use_canonical_imports_instead_of_legacy_facades() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    checked_roots = (repo_root / "backtest", repo_root / "scripts")
    forbidden_direct_modules = {
        "trader.codex_client",
        "trader.exit_engine",
        "trader.indicator_watch",
        "trader.risk",
        "trader.tools",
        "trader.trade_plan",
        "trader.tui",
    }
    forbidden_from_trader = {
        "codex_client",
        "exit_engine",
        "indicator_watch",
        "risk",
        "tools",
        "trade_plan",
        "tui",
    }

    def _is_forbidden_module(module_name: str) -> bool:
        return any(
            module_name == forbidden or module_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_direct_modules
        )

    violations: list[str] = []
    for root in checked_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and _is_forbidden_module(node.module):
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_trader)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if _is_forbidden_module(alias.name):
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def _module_imports(path: Path, module_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == module_name for alias in node.names):
                return True
    return False


def test_runnable_compatibility_facades_delegate_to_interface_command_modules() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    command_modules = {
        "attribution": ("trader.reporting.attribution",),
        "stats": ("trader.reporting.read_models.live_kpis", "trader.reporting.renderers.live_kpis"),
        "tool_usage": ("trader.reporting.read_models.tool_usage", "trader.reporting.renderers.tool_usage"),
        "tui": ("trader.interfaces.ui.tui",),
    }

    for command_name, canonical_modules in command_modules.items():
        command_path = trader_dir / "interfaces" / "cli" / f"{command_name}.py"
        legacy_package_main_path = trader_dir / command_name / "__main__.py"
        legacy_module_path = trader_dir / f"{command_name}.py"

        assert command_path.exists(), f"missing canonical command module for {command_name}"
        for canonical_module in canonical_modules:
            assert _module_imports(command_path, canonical_module)
        if legacy_package_main_path.exists():
            assert _module_imports(legacy_package_main_path, f"trader.commands.{command_name}")
        assert not legacy_module_path.exists()


def test_reporting_command_python_m_entrypoints() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for module_name in (
        "trader.interfaces.cli.attribution",
        "trader.interfaces.cli.stats",
        "trader.interfaces.cli.tool_usage",
        "trader.commands.attribution",
        "trader.commands.stats",
        "trader.commands.tool_usage",
        "trader.reporting.attribution",
        "trader.reporting.stats",
        "trader.attribution",
        "trader.stats",
        "trader.tool_usage",
    ):
        result = subprocess.run(
            [sys.executable, "-m", module_name, "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_tool_usage_cli_owner_is_command_module() -> None:
    from trader.commands import tool_usage as command_tool_usage
    from trader.reporting import tool_usage as reporting_tool_usage

    assert command_tool_usage.main.__module__ == "trader.interfaces.cli.tool_usage"
    assert not hasattr(reporting_tool_usage, "main")


def test_tool_usage_report_projection_is_read_model_canonical() -> None:
    import trader.tool_usage as legacy_tool_usage
    from trader.reporting import tool_usage as reporting_tool_usage
    from trader.reporting.read_models import tool_usage as read_model_tool_usage

    assert reporting_tool_usage.build_report is read_model_tool_usage.build_report
    assert reporting_tool_usage.domain_tool_usage is read_model_tool_usage.domain_tool_usage
    assert reporting_tool_usage.risk_observability is read_model_tool_usage.risk_observability
    assert legacy_tool_usage.build_report is read_model_tool_usage.build_report
    assert legacy_tool_usage.render_cli is reporting_tool_usage.render_cli


def test_reporting_renderers_are_canonical() -> None:
    from trader.reporting import stats as reporting_stats
    from trader.reporting import tool_usage as reporting_tool_usage
    from trader.reporting.renderers import live_kpis as live_kpis_renderer
    from trader.reporting.renderers import tool_usage as tool_usage_renderer

    assert reporting_stats.render_text is live_kpis_renderer.render_text
    assert reporting_tool_usage.render_cli is tool_usage_renderer.render_cli


def test_stats_cli_owner_is_command_module() -> None:
    from trader.commands import stats as command_stats
    from trader.reporting import stats as reporting_stats

    assert command_stats.main.__module__ == "trader.interfaces.cli.stats"
    assert not hasattr(reporting_stats, "main")


def test_attribution_cli_owner_is_command_module() -> None:
    from trader.commands import attribution as command_attribution
    from trader.reporting import attribution as reporting_attribution

    assert command_attribution.main.__module__ == "trader.interfaces.cli.attribution"
    assert not hasattr(reporting_attribution, "main")


def test_attribution_projection_is_read_model_canonical() -> None:
    import trader.attribution as legacy_attribution
    from trader.reporting import attribution as reporting_attribution
    from trader.reporting.read_models import attribution as read_model_attribution

    assert reporting_attribution.compute_round_trips is read_model_attribution.compute_round_trips
    assert reporting_attribution.compute_attribution is read_model_attribution.compute_attribution
    assert reporting_attribution.compute_hard_stop_diagnostics is read_model_attribution.compute_hard_stop_diagnostics
    assert legacy_attribution.compute_attribution is read_model_attribution.compute_attribution
    assert legacy_attribution.render_text is reporting_attribution.render_text


def test_meta_performance_projection_is_read_model_canonical() -> None:
    from trader.reporting import meta_performance as reporting_meta_performance
    from trader.reporting.read_models import meta_performance as read_model_meta_performance

    assert reporting_meta_performance.compute_meta_performance is read_model_meta_performance.compute_meta_performance
    assert reporting_meta_performance.DEFAULT_HORIZONS is read_model_meta_performance.DEFAULT_HORIZONS
    assert reporting_meta_performance._AUDIT_CACHE is read_model_meta_performance._AUDIT_CACHE


def test_decision_audit_engine_is_audit_package_canonical() -> None:
    from trader.reporting import decision_audit as reporting_decision_audit
    from trader.reporting.audit import decision_quality

    assert reporting_decision_audit.audit_rows is decision_quality.audit_rows
    assert reporting_decision_audit.refresh_audit_payload is decision_quality.refresh_audit_payload
    assert reporting_decision_audit.summarize_audited_rows is decision_quality.summarize_audited_rows
    assert reporting_decision_audit.load_prices_yfinance is decision_quality.load_prices_yfinance
    assert reporting_decision_audit.parse_ts is decision_quality.parse_ts
    assert reporting_decision_audit.parse_horizon is decision_quality.parse_horizon


def test_decision_bench_engine_is_bench_package_canonical() -> None:
    from trader.reporting import decision_bench as reporting_decision_bench
    from trader.reporting.bench import decision_bench as bench_decision_bench

    assert reporting_decision_bench.ModelSpec is bench_decision_bench.ModelSpec
    assert reporting_decision_bench.ModelCompletion is bench_decision_bench.ModelCompletion
    assert reporting_decision_bench.parse_model_specs is bench_decision_bench.parse_model_specs
    assert reporting_decision_bench.select_cases is bench_decision_bench.select_cases
    assert reporting_decision_bench.run_bench is bench_decision_bench.run_bench
    assert reporting_decision_bench.dry_run_payload is bench_decision_bench.dry_run_payload
    assert reporting_decision_bench.render_summary is bench_decision_bench.render_summary


def test_decision_ledger_store_is_ledger_package_canonical() -> None:
    from trader.reporting import decision_ledger as reporting_decision_ledger
    from trader.reporting.ledger import decision_ledger as ledger_decision_ledger

    assert reporting_decision_ledger.DecisionLedgerStore is ledger_decision_ledger.DecisionLedgerStore
    assert reporting_decision_ledger.build_decision_row is ledger_decision_ledger.build_decision_row
    assert reporting_decision_ledger.build_legacy_event_row is ledger_decision_ledger.build_legacy_event_row
    assert reporting_decision_ledger.seed_existing_reports is ledger_decision_ledger.seed_existing_reports
    assert reporting_decision_ledger.seed_existing_events is ledger_decision_ledger.seed_existing_events
    assert reporting_decision_ledger.backfill_code_versions is ledger_decision_ledger.backfill_code_versions
    assert reporting_decision_ledger.DEFAULT_LEDGER_FILENAME is ledger_decision_ledger.DEFAULT_LEDGER_FILENAME
    assert reporting_decision_ledger._decision_id is ledger_decision_ledger._decision_id
    assert reporting_decision_ledger.code_version is ledger_decision_ledger.code_version


def test_reporting_protocols_are_colocated_under_reporting() -> None:
    from trader.reporting.audit.protocols import PriceHistoryLoader
    from trader.reporting.bench.protocols import BenchHistory, ModelBenchCompleter
    from trader.reporting.ledger.protocols import DecisionLedgerAppender, DecisionLedgerReader
    from trader.reporting.read_models.protocols import DecisionQualityScorer

    assert PriceHistoryLoader.__module__ == "trader.reporting.audit.protocols"
    assert BenchHistory.__module__ == "trader.reporting.bench.protocols"
    assert ModelBenchCompleter.__module__ == "trader.reporting.bench.protocols"
    assert DecisionLedgerAppender.__module__ == "trader.reporting.ledger.protocols"
    assert DecisionLedgerReader.__module__ == "trader.reporting.ledger.protocols"
    assert DecisionQualityScorer.__module__ == "trader.reporting.read_models.protocols"


def test_market_and_planning_use_domain_primitives_instead_of_tools() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    checked_roots = (trader_dir / "market", trader_dir / "planning")
    forbidden_modules = {"trader.tools.market", "trader.tools.execution"}

    violations: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(trader_dir.parent)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader.tools":
                    forbidden_names = {
                        alias.name for alias in node.names if f"trader.tools.{alias.name}" in forbidden_modules
                    }
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader.tools import {sorted(forbidden_names)}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_agent_package_does_not_depend_on_runtime_package() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    agent_dir = trader_dir / "agent"

    violations: list[str] = []
    for path in sorted(agent_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(trader_dir.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trader.runtime"):
                violations.append(f"{rel_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("trader.runtime"):
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_tool_primitive_imports_are_compatibility_aliases() -> None:
    from trader.domain.market_data import Bar, MarketError
    from trader.domain.orders import Side
    from trader.tools.execution import Side as LegacySide
    from trader.tools.market import Bar as LegacyBar
    from trader.tools.market import MarketError as LegacyMarketError

    assert LegacyBar is Bar
    assert LegacyMarketError is MarketError
    assert LegacySide == Side


def test_market_data_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.market.data_source import CompositeDataSource, DataSource as AdapterDataSource, YFinanceDataSource
    from trader.market.ib_source import IBDataSource, INTERVAL_MAP, LOOKBACK_MAP, connect_ib
    from trader.market.market_data import Bar, Freshness, MarketError, assess_freshness
    from trader.market.protocols import DataSource
    from trader.tools.data_source import CompositeDataSource as LegacyCompositeDataSource
    from trader.tools.data_source import DataSource as LegacyDataSource
    from trader.tools.data_source import YFinanceDataSource as LegacyYFinanceDataSource
    from trader.tools.ib_source import IBDataSource as LegacyIBDataSource
    from trader.tools.ib_source import INTERVAL_MAP as LegacyIntervalMap
    from trader.tools.ib_source import LOOKBACK_MAP as LegacyLookbackMap
    from trader.tools.ib_source import connect_ib as legacy_connect_ib
    from trader.tools.market import Bar as LegacyBar
    from trader.tools.market import Freshness as LegacyFreshness
    from trader.tools.market import MarketError as LegacyMarketError
    from trader.tools.market import assess_freshness as legacy_assess_freshness

    assert LegacyBar is Bar
    assert LegacyMarketError is MarketError
    assert LegacyFreshness is Freshness
    assert legacy_assess_freshness is assess_freshness
    assert AdapterDataSource is DataSource
    assert LegacyDataSource is DataSource
    assert LegacyCompositeDataSource is CompositeDataSource
    assert LegacyYFinanceDataSource is YFinanceDataSource
    assert LegacyIBDataSource is IBDataSource
    assert LegacyIntervalMap is INTERVAL_MAP
    assert LegacyLookbackMap is LOOKBACK_MAP
    assert legacy_connect_ib is connect_ib


def test_shared_protocols_use_protocols_modules_instead_of_ports_modules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    checked_roots = (
        repo_root / "trader",
        repo_root / "tests",
        repo_root / "backtest",
    )
    forbidden_modules = {
        f"trader.{package}.ports"
        for package in ("execution", "market")
    }

    violations: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_shared_protocols_have_no_legacy_ports_modules() -> None:
    from trader.execution.protocols import Broker, CommissionModel
    from trader.market.protocols import DataSource

    assert Broker.__module__ == "trader.execution.protocols"
    assert CommissionModel.__module__ == "trader.execution.protocols"
    assert DataSource.__module__ == "trader.market.protocols"

    repo_root = Path(__file__).resolve().parents[1]
    ports_paths = (
        repo_root / "trader" / "execution" / "ports.py",
        repo_root / "trader" / "market" / "ports.py",
    )

    assert [path.relative_to(repo_root) for path in ports_paths if path.exists()] == []


def test_legacy_market_tool_modules_proxy_mutations_to_canonical_modules(monkeypatch) -> None:
    from trader.market import data_source as canonical_data_source
    from trader.market import ib_source as canonical_ib_source
    from trader.market import market_data as canonical_market
    from trader.tools import data_source as legacy_data_source
    from trader.tools import ib_source as legacy_ib_source
    from trader.tools import market as legacy_market

    market_sentinel = object()
    data_source_sentinel = object()
    ib_source_sentinel = object()

    monkeypatch.setattr(legacy_market, "_compat_probe", market_sentinel, raising=False)
    monkeypatch.setattr(legacy_data_source, "_compat_probe", data_source_sentinel, raising=False)
    monkeypatch.setattr(legacy_ib_source, "_compat_probe", ib_source_sentinel, raising=False)

    assert canonical_market._compat_probe is market_sentinel
    assert canonical_data_source._compat_probe is data_source_sentinel
    assert canonical_ib_source._compat_probe is ib_source_sentinel


def test_application_uses_market_ports_instead_of_data_source_adapters() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    application_dir = trader_dir / "application"
    forbidden_modules = {"trader.market.data_source"}
    forbidden_from_market = {"data_source"}

    violations: list[str] = []
    for path in sorted(application_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(trader_dir.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                violations.append(f"{rel_path}: from {node.module} import ...")
            elif isinstance(node, ast.ImportFrom) and node.module == "trader.market":
                forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_market)
                if forbidden_names:
                    violations.append(f"{rel_path}: from trader.market import {forbidden_names}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules:
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_application_layer_does_not_depend_on_runtime_composition() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    application_dir = trader_dir / "application"

    violations: list[str] = []
    for path in sorted(application_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(trader_dir.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trader.runtime"):
                violations.append(f"{rel_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("trader.runtime"):
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_news_feed_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.market.news_feed import NewsItemsArchive, RawNews, news_snapshot, reset_cache
    from trader.tools.news_feed import NewsItemsArchive as LegacyNewsItemsArchive
    from trader.tools.news_feed import RawNews as LegacyRawNews
    from trader.tools.news_feed import news_snapshot as legacy_news_snapshot
    from trader.tools.news_feed import reset_cache as legacy_reset_cache

    assert LegacyNewsItemsArchive is NewsItemsArchive
    assert LegacyRawNews is RawNews
    assert legacy_news_snapshot is news_snapshot
    assert legacy_reset_cache is reset_cache


def test_legacy_news_feed_tool_module_proxies_mutations_to_canonical_module(monkeypatch) -> None:
    from trader.market import news_feed as canonical_news_feed
    from trader.tools import news_feed as legacy_news_feed

    sentinel = object()
    monkeypatch.setattr(legacy_news_feed, "_compat_probe", sentinel, raising=False)

    assert canonical_news_feed._compat_probe is sentinel


def test_core_packages_do_not_depend_on_legacy_news_feed_tool() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    checked_roots = (
        trader_dir / "application",
        trader_dir / "market",
        trader_dir / "reporting",
        trader_dir / "runtime",
    )
    forbidden_modules = {"trader.tools.news_feed"}
    forbidden_from_tools = {"news_feed"}

    violations: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(trader_dir.parent)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader.tools":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_tools)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader.tools import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_core_packages_do_not_depend_on_legacy_market_tools() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    checked_roots = (
        trader_dir / "agent",
        trader_dir / "application",
        trader_dir / "market",
        trader_dir / "reporting",
        trader_dir / "runtime",
    )
    forbidden_modules = {
        "trader.tools.data_source",
        "trader.tools.ib_source",
        "trader.tools.market",
    }
    forbidden_from_tools = {"data_source", "ib_source", "market"}

    violations: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(trader_dir.parent)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader.tools":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_tools)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader.tools import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_execution_broker_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.execution.broker import (
        Broker,
        Commission,
        CommissionModel,
        CommissionModelName,
        Fill,
        IbkrCommissionModel,
        NoCommissionModel,
        Order,
        Position,
        Side,
        SimBroker,
    )
    from trader.domain.orders import Side as CanonicalSide
    from trader.execution.contracts import Commission as CanonicalCommission
    from trader.execution.contracts import CommissionModelName as CanonicalCommissionModelName
    from trader.execution.contracts import Fill as CanonicalFill
    from trader.execution.contracts import Order as CanonicalOrder
    from trader.execution.contracts import Position as CanonicalPosition
    from trader.execution.protocols import Broker as CanonicalBroker
    from trader.execution.protocols import CommissionModel as CanonicalCommissionModel
    from trader.tools.execution import Broker as LegacyBroker
    from trader.tools.execution import Commission as LegacyCommission
    from trader.tools.execution import CommissionModel as LegacyCommissionModel
    from trader.tools.execution import CommissionModelName as LegacyCommissionModelName
    from trader.tools.execution import Fill as LegacyFill
    from trader.tools.execution import IbkrCommissionModel as LegacyIbkrCommissionModel
    from trader.tools.execution import NoCommissionModel as LegacyNoCommissionModel
    from trader.tools.execution import Order as LegacyOrder
    from trader.tools.execution import Position as LegacyPosition
    from trader.tools.execution import Side as LegacySide
    from trader.tools.execution import SimBroker as LegacySimBroker

    assert Broker is CanonicalBroker
    assert Commission is CanonicalCommission
    assert CommissionModel is CanonicalCommissionModel
    assert CommissionModelName is CanonicalCommissionModelName
    assert Fill is CanonicalFill
    assert Order is CanonicalOrder
    assert Position is CanonicalPosition
    assert Side is CanonicalSide
    assert LegacyBroker is Broker
    assert LegacyCommission is Commission
    assert LegacyCommissionModel is CommissionModel
    assert LegacyCommissionModelName is CommissionModelName
    assert LegacyFill is Fill
    assert LegacyIbkrCommissionModel is IbkrCommissionModel
    assert LegacyNoCommissionModel is NoCommissionModel
    assert LegacyOrder is Order
    assert LegacyPosition is Position
    assert LegacySide is Side
    assert LegacySimBroker is SimBroker


def test_planned_exits_uses_canonical_trade_plan_store_protocol() -> None:
    from trader.application.exit import planned_exits
    from trader.planning.protocols import TradePlanStoreLike

    assert planned_exits.TradePlanStoreLike is TradePlanStoreLike

    path = Path(__file__).resolve().parents[1] / "trader" / "application" / "exit" / "planned_exits.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    local_protocols = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "TradePlanStoreLike"
    ]

    assert local_protocols == []


def test_runtime_protocols_are_canonical_and_not_redeclared() -> None:
    from trader.runtime.protocols import (
        CycleReportWriter,
        LoggerLike,
        RecoverableLedger,
        StartablePool,
        Stoppable,
    )

    assert {"debug", "info", "warning", "exception"} <= set(LoggerLike.__dict__)
    assert "stop" in Stoppable.__dict__
    assert {"write_last_report", "append_cycle_history"} <= set(CycleReportWriter.__dict__)
    assert "recover_on_boot" in RecoverableLedger.__dict__
    assert {"start", "stop"} <= set(StartablePool.__dict__)

    repo_root = Path(__file__).resolve().parents[1]
    runtime_dir = repo_root / "trader" / "runtime"
    canonical_path = runtime_dir / "protocols.py"
    shared_protocols = {
        "LoggerLike",
        "Stoppable",
        "CycleReportWriter",
        "RecoverableLedger",
        "StartablePool",
    }

    violations: list[str] = []
    for path in sorted(runtime_dir.rglob("*.py")):
        if "__pycache__" in path.parts or path == canonical_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in shared_protocols:
                violations.append(f"{rel_path}: class {node.name}")

    assert violations == []


def test_application_uses_execution_contracts_and_ports_instead_of_broker_adapter() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    checked_paths = (
        trader_dir / "application",
        trader_dir / "execution" / "portfolio.py",
        trader_dir / "execution" / "risk.py",
    )
    forbidden_module = "trader.execution.broker"
    forbidden_names = {"Broker", "CommissionModel", "Fill", "Order", "Position"}

    violations: list[str] = []
    for checked_path in checked_paths:
        paths = sorted(checked_path.rglob("*.py")) if checked_path.is_dir() else [checked_path]
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(trader_dir.parent)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == forbidden_module:
                    imported = sorted(alias.name for alias in node.names if alias.name in forbidden_names)
                    if imported:
                        violations.append(f"{rel_path}: from {forbidden_module} import {imported}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == forbidden_module:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_portfolio_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.execution.portfolio import Holding, Snapshot, snapshot
    from trader.tools.portfolio import Holding as LegacyHolding
    from trader.tools.portfolio import Snapshot as LegacySnapshot
    from trader.tools.portfolio import snapshot as legacy_snapshot

    assert LegacyHolding is Holding
    assert LegacySnapshot is Snapshot
    assert legacy_snapshot is snapshot


def test_agent_memory_and_raw_learnings_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.agent.memory import Memory
    from trader.agent.learnings.raw_store import LearningsStore
    from trader.agent.learnings.raw_store import RawLearningsStore
    from trader.tools.memory import LearningsStore as LegacyLearningsStore
    from trader.tools.memory import Memory as LegacyMemory
    from trader.tools.memory import RawLearningsStore as LegacyRawLearningsStore

    assert LegacyMemory is Memory
    assert LearningsStore is RawLearningsStore
    assert LegacyLearningsStore is RawLearningsStore
    assert LegacyRawLearningsStore is RawLearningsStore


def test_legacy_memory_tool_module_proxies_mutations_to_canonical_modules(monkeypatch) -> None:
    from trader.agent import memory as canonical_agent_memory
    from trader.agent.learnings import raw_store as canonical_raw_store
    from trader.tools import memory as legacy_memory

    memory_sentinel = object()
    learnings_sentinel = object()
    raw_sentinel = object()

    monkeypatch.setattr(legacy_memory, "Memory", memory_sentinel)
    monkeypatch.setattr(legacy_memory, "LearningsStore", learnings_sentinel)

    assert canonical_agent_memory.Memory is memory_sentinel
    assert canonical_raw_store.RawLearningsStore is learnings_sentinel
    assert canonical_raw_store.LearningsStore is learnings_sentinel

    monkeypatch.setattr(legacy_memory, "RawLearningsStore", raw_sentinel)

    assert canonical_raw_store.RawLearningsStore is raw_sentinel
    assert canonical_raw_store.LearningsStore is raw_sentinel


def test_runtime_agent_and_learnings_do_not_depend_on_legacy_memory_tool() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    trader_dir = repo_root / "trader"
    checked_roots = (
        repo_root / "backtest",
        trader_dir / "agent",
        trader_dir / "reporting" / "read_models",
        trader_dir / "runtime",
    )
    forbidden_modules = {"trader.tools.memory"}
    forbidden_from_tools = {"memory"}

    violations: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader.tools":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_tools)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader.tools import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_legacy_portfolio_tool_module_proxies_mutations_to_canonical_module(monkeypatch) -> None:
    from trader.execution import portfolio as canonical_portfolio
    from trader.tools import portfolio as legacy_portfolio

    sentinel = object()
    monkeypatch.setattr(legacy_portfolio, "_compat_probe", sentinel, raising=False)

    assert canonical_portfolio._compat_probe is sentinel

    def fake_snapshot(*_args, **_kwargs):
        return sentinel

    monkeypatch.setattr(legacy_portfolio, "snapshot", fake_snapshot)

    assert canonical_portfolio.snapshot is fake_snapshot


def test_runtime_and_execution_do_not_depend_on_legacy_portfolio_tool() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    checked_roots = (
        trader_dir / "execution",
        trader_dir / "runtime",
    )
    forbidden_modules = {"trader.tools.portfolio"}
    forbidden_from_tools = {"portfolio"}

    violations: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(trader_dir.parent)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader.tools":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_tools)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader.tools import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_execution_package_does_not_depend_on_tools_package() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    execution_dir = trader_dir / "execution"

    violations: list[str] = []
    for path in sorted(execution_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(trader_dir.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trader.tools"):
                violations.append(f"{rel_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("trader.tools"):
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_scheduler_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.planning.scheduler import (
        STALE_BACKOFF_MAX_MINUTES,
        STALE_BACKOFF_MAX_STREAK,
        Scheduler,
    )
    from trader.scheduling.scheduler import Scheduler as LegacyPackageScheduler
    from trader.tools.scheduler import STALE_BACKOFF_MAX_MINUTES as LegacyMaxMinutes
    from trader.tools.scheduler import STALE_BACKOFF_MAX_STREAK as LegacyMaxStreak
    from trader.tools.scheduler import Scheduler as LegacyScheduler

    assert LegacyPackageScheduler is Scheduler
    assert LegacyScheduler is Scheduler
    assert LegacyMaxMinutes == STALE_BACKOFF_MAX_MINUTES
    assert LegacyMaxStreak == STALE_BACKOFF_MAX_STREAK


def test_planning_scheduler_does_not_depend_on_tools_package() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    scheduler_path = trader_dir / "planning" / "scheduler.py"

    violations: list[str] = []
    tree = ast.parse(scheduler_path.read_text(encoding="utf-8"), filename=str(scheduler_path))
    rel_path = scheduler_path.relative_to(trader_dir.parent)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trader.tools"):
            violations.append(f"{rel_path}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("trader.tools"):
                    violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []


def test_agent_protocol_prompts_do_not_import_agent_context() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        "import trader.agent.protocol.prompts; "
        "assert 'trader.agent.context' not in sys.modules, "
        "sorted(name for name in sys.modules if name.startswith('trader.agent'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_agent_protocol_type_import_stays_light() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    code = """
import sys

from trader.agent.protocol import IndicatorRequest
from trader.domain.decisions import IndicatorRequest as DomainIndicatorRequest

assert IndicatorRequest is DomainIndicatorRequest
loaded = set(sys.modules)
for name in (
    "trader.market",
    "trader.planning",
    "trader.reporting",
    "trader.semantic",
    "trader.agent.context",
    "trader.agent.client",
    "trader.agent.llm",
):
    assert name not in loaded, sorted(m for m in loaded if m.startswith("trader."))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
