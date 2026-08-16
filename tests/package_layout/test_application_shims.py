import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
    _assert_application_submodule_layout,
)


def test_application_package_has_only_canonical_subpackages() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

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
        "learnings",
        "migration",
        "portfolio",
        "queue",
        "record",
        "universe",
    }



def test_application_does_not_import_infrastructure() -> None:
    repo_root = REPO_ROOT
    application_dir = repo_root / "trader" / "application"
    violations: list[str] = []

    for source_path in sorted(application_dir.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        relative_path = source_path.relative_to(application_dir)
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



def test_application_does_not_import_reporting() -> None:
    repo_root = REPO_ROOT
    application_dir = repo_root / "trader" / "application"
    violations: list[str] = []

    for source_path in sorted(application_dir.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        relative_path = source_path.relative_to(application_dir)
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "trader.reporting"
            ):
                violations.append(f"{relative_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("trader.reporting"):
                        violations.append(f"{relative_path}: import {alias.name}")

    assert violations == []



def test_application_imports_decision_contracts_from_domain() -> None:
    repo_root = REPO_ROOT
    application_dir = repo_root / "trader" / "application"
    violations: list[str] = []

    for source_path in sorted(application_dir.rglob("*.py")):
        if "__pycache__" in source_path.parts:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "trader.agent.protocol.types":
                violations.append(
                    f"{source_path.relative_to(application_dir)}:{node.lineno}: "
                    "import decision contracts from trader.domain.decisions"
                )

    assert violations == []



def test_decide_application_uses_local_planner_protocol_not_agent_client() -> None:
    repo_root = REPO_ROOT
    decide_dir = repo_root / "trader" / "application" / "decide"
    protocol_path = decide_dir / "protocols.py"

    assert protocol_path.exists()
    protocol_source = protocol_path.read_text(encoding="utf-8")
    assert "class DecisionBatchPlanner(Protocol):" in protocol_source
    assert "trader.agent" not in protocol_source

    violations: list[str] = []
    for source_path in sorted((repo_root / "trader" / "application").rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {
                "trader.agent.client",
                "trader.agent.protocol.types",
            }:
                violations.append(
                    f"{source_path.relative_to(repo_root)}:{node.lineno}: from {node.module} import ..."
                )
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{source_path.relative_to(repo_root)}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name in {"trader.agent.client", "trader.agent.protocol.types"}
                )

    assert violations == []



def test_watch_scanner_depends_on_market_port_and_domain_error() -> None:
    repo_root = REPO_ROOT
    source_path = repo_root / "trader" / "application" / "cycle" / "watch_scanner.py"
    source = source_path.read_text(encoding="utf-8")

    assert "from trader.domain.market_data import MarketError" in source
    assert "from trader.market.protocols import DataSource" in source
    assert "from trader.market import market_data" not in source
    assert "data_source: object" not in source



def test_market_snapshot_uses_local_fx_provider_not_runtime_configuration() -> None:
    repo_root = REPO_ROOT
    source_path = repo_root / "trader" / "application" / "cycle" / "market_snapshot.py"
    source = source_path.read_text(encoding="utf-8")

    assert "class FxRateProvider(Protocol):" in source
    assert "from pathlib import Path" not in source
    assert "trader.market.fx_rates" not in source
    assert "config_dir:" not in source



def test_decision_recorder_injects_agent_trace_appender() -> None:
    repo_root = REPO_ROOT
    recorder_path = repo_root / "trader" / "application" / "record" / "decision_recorder.py"
    runtime_path = repo_root / "trader" / "runtime" / "agent_trace_runtime.py"
    source = recorder_path.read_text(encoding="utf-8")

    assert runtime_path.exists()
    assert "AgentTraceAppender" in source
    assert "agent_trace_appender" in source
    assert "agent_trace_path" not in source
    assert "from pathlib import Path" not in source



def test_application_learnings_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "learnings",
        [
            "consolidate",
            "context",
            "protocols",
        ],
    )
    protocols = (application_dir / "learnings" / "protocols.py").read_text(encoding="utf-8")
    assert "class RawLearningsPort(Protocol):" in protocols
    assert "class ConsolidatedLearningsPort(Protocol):" in protocols
    assert "class ConsolidationStatusPort(Protocol):" in protocols
    assert "class CurationPort(Protocol):" in protocols
    assert "class LearningComposer(Protocol):" in protocols
    assert "def compose(" in protocols
    assert "trader.agent" not in protocols
    assert "trader.infrastructure" not in protocols



def test_application_analyst_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "analyst",
        [
            "company_micro",
            "news_macro",
            "situation_attribution",
        ],
    )



def test_application_migration_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "migration",
        [
            "strategy_language",
        ],
    )



def test_application_record_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "record",
        [
            "decision_recorder",
            "decision_entries",
            "decision_ledger_rows",
            "decision_watches",
            "tool_outcomes",
            "plan_review",
            "confidence_feedback",
            "gross_feedback",
            "learning_outcomes",
        ],
    )



def test_application_cycle_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

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
    repo_root = REPO_ROOT
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



def test_application_decide_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

    _assert_application_submodule_layout(
        application_dir,
        "decide",
        [
            "planner_batch",
            "protocols",
            "one",
            "handler",
            "tool_round",
            "queue_dispatch",
            "recent_decisions",
        ],
    )



def test_application_exit_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

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



def test_application_execute_modules_are_nested_without_legacy_shims() -> None:
    application_dir = REPO_ROOT / "trader" / "application"

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
        ],
    )



def test_application_and_backtest_use_canonical_domain_watch_policy() -> None:
    repo_root = REPO_ROOT
    forbidden_modules = {
        "trader.planning.armed_order",
        "trader.planning.indicator_watch",
        "trader.planning.trade_plan",
        "trader.planning.watch_evaluator",
    }
    violations: list[str] = []

    for source_root in (
        repo_root / "trader" / "application",
        repo_root / "trader" / "runtime",
        repo_root / "backtest",
    ):
        for source_path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(
                        f"{source_path.relative_to(repo_root)}:{node.lineno}: from {node.module} import ..."
                    )
                elif isinstance(node, ast.Import):
                    violations.extend(
                        f"{source_path.relative_to(repo_root)}:{node.lineno}: import {alias.name}"
                        for alias in node.names
                        if alias.name in forbidden_modules
                    )

    assert violations == []



def test_application_runtime_and_backtest_use_canonical_pure_market_modules() -> None:
    repo_root = REPO_ROOT
    forbidden_modules = {
        "trader.market.execution_eligibility",
        "trader.market.family_regime",
        "trader.market.features",
        "trader.market.fx",
        "trader.market.gross_priority",
        "trader.market.volatility",
    }
    violations: list[str] = []

    for source_root in (
        repo_root / "trader" / "application",
        repo_root / "trader" / "runtime",
        repo_root / "backtest",
    ):
        for source_path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(
                        f"{source_path.relative_to(repo_root)}:{node.lineno}: from {node.module} import ..."
                    )
                elif isinstance(node, ast.Import):
                    violations.extend(
                        f"{source_path.relative_to(repo_root)}:{node.lineno}: import {alias.name}"
                        for alias in node.names
                        if alias.name in forbidden_modules
                    )

    assert violations == []



def test_application_uses_canonical_domain_relevance_and_session_policy() -> None:
    repo_root = REPO_ROOT
    infra_holds = (repo_root / "trader" / "application" / "cycle" / "infra_holds.py").read_text(
        encoding="utf-8"
    )
    planner_batch = (repo_root / "trader" / "application" / "decide" / "planner_batch.py").read_text(
        encoding="utf-8"
    )

    assert "from trader.domain.planning import relevance_gate" in infra_holds
    assert "trader.planning import relevance_gate" not in infra_holds
    assert "from trader.domain.market import sessions as market" in planner_batch
    assert "trader.market import market_data as market" not in planner_batch



def test_daemon_delegates_planned_exit_logic_to_application_service() -> None:
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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



def test_retryable_error_is_application_contract_with_worker_facade() -> None:
    from trader.application.queue.contracts import RetryableError
    from trader.infrastructure.queue.worker import RetryableError as worker_retryable_error
    from trader.queue.worker import RetryableError as legacy_retryable_error

    assert worker_retryable_error is RetryableError
    assert legacy_retryable_error is RetryableError



def test_cycle_decision_delegates_execute_queue_plan_payload_to_application_service() -> None:
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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



def test_application_uses_market_ports_instead_of_data_source_adapters() -> None:
    trader_dir = REPO_ROOT / "trader"
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
    trader_dir = REPO_ROOT / "trader"
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



def test_application_uses_execution_contracts_and_ports_instead_of_broker_adapter() -> None:
    trader_dir = REPO_ROOT / "trader"
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

