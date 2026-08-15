import ast
from pathlib import Path

from tests.package_layout._helpers import (
    REPO_ROOT,
    _domain_import_violations,
    _has_python_sources,
)


def test_market_rotation_pure_modules_have_no_high_level_dependencies() -> None:
    rotation_dir = REPO_ROOT / "trader" / "market" / "rotation"
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



def test_market_execution_eligibility_is_canonical_domain_module_with_market_facade() -> None:
    repo_root = REPO_ROOT
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



def test_market_volatility_is_canonical_low_layer_module() -> None:
    repo_root = REPO_ROOT
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



def test_top_level_packages_have_declared_architecture_roles() -> None:
    trader_dir = REPO_ROOT / "trader"

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



def test_semantic_catalog_is_nested_under_domain() -> None:
    trader_dir = REPO_ROOT / "trader"
    semantic_dir = trader_dir / "domain" / "semantic"

    assert semantic_dir.exists()
    assert (semantic_dir / "catalog.py").exists()
    assert not _has_python_sources(trader_dir / "semantic")



def test_contract_value_types_are_domain_canonical_with_public_facades() -> None:
    from dataclasses import asdict, is_dataclass, replace

    repo_root = REPO_ROOT
    trader_dir = repo_root / "trader"
    domain_paths = [
        trader_dir / "domain" / "decisions.py",
        trader_dir / "domain" / "execution" / "risk_gate.py",
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
    assert "class RiskGate" not in execution_risk_source
    assert "from trader.domain.execution.risk_gate import RiskGate" in execution_risk_source
    assert "from trader.domain.risk import RiskLimits" in execution_risk_source
    assert "from trader.domain.risk import Verdict" in execution_risk_source

    import trader.agent.protocol.strategy_language as strategy_facade
    import trader.agent.protocol.types as types_facade
    import trader.domain.decisions as domain_decisions
    import trader.domain.execution.risk_gate as domain_risk_gate
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
    assert execution_risk.RiskGate is domain_risk_gate.RiskGate
    assert domain_risk_gate.RiskGate.__module__ == "trader.domain.execution.risk_gate"

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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
    risk_source = (repo_root / "trader" / "execution" / "risk.py").read_text(encoding="utf-8")

    import trader.execution.risk as execution_risk
    from trader.interfaces.ui import tui
    from trader.reporting.read_models import runtime_state
    from trader.support.config import risk as config_risk

    assert "read_min_trade_confidence" not in risk_source
    assert not hasattr(execution_risk, "read_min_trade_confidence")
    assert config_risk.read_min_trade_confidence is runtime_state.read_min_trade_confidence

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
    repo_root = REPO_ROOT
    trader_dir = repo_root / "trader"
    domain_planning_dir = trader_dir / "domain" / "planning"

    expected_modules = {
        "__init__.py",
        "armed_order.py",
        "exit_engine.py",
        "exit_plan_spec.py",
        "indicator_watch.py",
        "protocols.py",
        "relevance_gate.py",
        "scheduling.py",
        "trade_plan.py",
        "watch_evaluator.py",
        "watches.py",
    }
    assert {path.name for path in domain_planning_dir.glob("*.py")} == expected_modules

    for module_name in (
        "armed_order",
        "exit_engine",
        "exit_plan_spec",
        "indicator_watch",
        "relevance_gate",
        "trade_plan",
        "watch_evaluator",
    ):
        facade_path = trader_dir / "planning" / f"{module_name}.py"
        assert facade_path.exists()
        source = facade_path.read_text(encoding="utf-8")
        assert f"from trader.domain.planning.{module_name} import" in source
        assert "globals().update" not in source

    import trader.domain.planning.armed_order as domain_armed_order
    import trader.domain.planning.exit_engine as domain_exit_engine
    import trader.domain.planning.exit_plan_spec as domain_exit_plan_spec
    import trader.domain.planning.indicator_watch as domain_indicator_watch
    import trader.domain.planning.protocols as domain_protocols
    import trader.domain.planning.relevance_gate as domain_relevance_gate
    import trader.domain.planning.scheduling as domain_scheduling
    import trader.domain.planning.trade_plan as domain_trade_plan
    import trader.domain.planning.watch_evaluator as domain_watch_evaluator
    import trader.domain.planning.watches as domain_watches
    import trader.application.cycle.schedule as cycle_schedule
    import trader.planning.armed_order as planning_armed_order
    import trader.planning.exit_engine as planning_exit_engine
    import trader.planning.exit_plan_spec as planning_exit_plan_spec
    import trader.planning.indicator_watch as planning_indicator_watch
    import trader.planning.protocols as planning_protocols
    import trader.planning.relevance_gate as planning_relevance_gate
    import trader.planning.scheduler as planning_scheduler
    import trader.planning.trade_plan as planning_trade_plan
    import trader.planning.watch_evaluator as planning_watch_evaluator
    from trader.planning.indicator_watch import is_armed_plan

    assert planning_protocols.SchedulerLike is domain_protocols.SchedulerLike
    assert planning_protocols.TradePlanStoreLike is domain_protocols.TradePlanStoreLike
    assert domain_protocols.SchedulerLike.__module__ == "trader.domain.planning.protocols"
    assert domain_protocols.TradePlanStoreLike.__module__ == "trader.domain.planning.protocols"
    assert planning_armed_order.normalize_armed_order is domain_armed_order.normalize_armed_order
    assert planning_exit_engine.evaluate_plan is domain_exit_engine.evaluate_plan
    assert planning_relevance_gate.symbol_needs_llm is domain_relevance_gate.symbol_needs_llm
    assert planning_exit_plan_spec.normalize_exit_plan is domain_exit_plan_spec.normalize_exit_plan
    assert planning_exit_plan_spec._positive_float is domain_exit_plan_spec._positive_float
    assert planning_indicator_watch.build_indicator_watch is domain_indicator_watch.build_indicator_watch
    assert planning_scheduler.STALE_BACKOFF_BASE_MULTIPLIER == domain_scheduling.STALE_BACKOFF_BASE_MULTIPLIER
    assert planning_scheduler.STALE_BACKOFF_MAX_MINUTES == domain_scheduling.STALE_BACKOFF_MAX_MINUTES
    assert planning_scheduler.STALE_BACKOFF_MAX_STREAK == domain_scheduling.STALE_BACKOFF_MAX_STREAK
    assert planning_watch_evaluator.evaluate_indicator_watches is domain_watch_evaluator.evaluate_indicator_watches
    assert planning_trade_plan.create_trade_plan is domain_trade_plan.create_trade_plan
    assert planning_trade_plan.resolve_exit_plan is domain_trade_plan.resolve_exit_plan
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



def test_internal_ui_and_bench_use_domain_policies_not_compatibility_facades() -> None:
    repo_root = REPO_ROOT
    expected_imports = {
        "trader/interfaces/cockpit/derive.py": "from trader.domain.planning.watches import is_armed_plan",
        "trader/interfaces/cockpit/format.py": "from trader.domain.market import fx",
        "trader/interfaces/ui/panels/plans_panels.py": "from trader.domain.planning.watches import is_armed_plan",
        "trader/interfaces/ui/panels/positions_panels.py": "from trader.domain.market import fx",
        "trader/interfaces/ui/panels/watches_panels.py": "from trader.domain.planning.watches import is_armed_plan",
        "trader/reporting/bench/decision_bench.py": "from trader.domain.market import family_regime",
    }

    for relative_path, expected_import in expected_imports.items():
        source = (repo_root / relative_path).read_text(encoding="utf-8")
        assert expected_import in source
        assert "from trader.planning.indicator_watch import is_armed_plan" not in source



def test_rotation_is_nested_under_market() -> None:
    trader_dir = REPO_ROOT / "trader"
    rotation_dir = trader_dir / "market" / "rotation"

    assert rotation_dir.exists()
    assert (rotation_dir / "core.py").exists()
    assert (rotation_dir / "venues.py").exists()
    assert (rotation_dir / "schedule.py").exists()
    assert (rotation_dir / "wiring.py").exists()
    assert not _has_python_sources(trader_dir / "rotation")



def test_universe_rotation_policies_are_canonical_domain_and_application_modules() -> None:
    repo_root = REPO_ROOT
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



def test_decision_reason_vocabulary_is_domain_canonical() -> None:
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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



def test_market_and_planning_use_domain_primitives_instead_of_tools() -> None:
    trader_dir = REPO_ROOT / "trader"
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

