import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
    _annotation_mentions_scheduler_class,
)


def test_scheduler_consumers_depend_on_schedulerlike_not_planning_scheduler_class() -> None:
    repo_root = REPO_ROOT
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
    from trader.domain.planning.protocols import SchedulerLike
    from trader.infrastructure.state_db.scheduler_json import Scheduler
    from trader.infrastructure.state_db.scheduler_store import SqliteScheduler
    from trader.planning.protocols import SchedulerLike as FacadeSchedulerLike

    assert FacadeSchedulerLike is SchedulerLike
    assert SchedulerLike.__module__ == "trader.domain.planning.protocols"

    protocol_methods = {
        name
        for name, value in SchedulerLike.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert protocol_methods == {
        "ack_indicator_triggers",
        "active_indicator_watches",
        "claim_indicator_watch_trigger",
        "clear_symbol_next_wake",
        "due_symbols",
        "get_stale_streak",
        "next_wake",
        "pending_indicator_trigger_symbols",
        "pending_indicator_triggers",
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



def test_daemon_agent_context_and_process_state_are_runtime_canonical() -> None:
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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



def test_daemon_delegates_decision_dispatch_to_runtime_adapter() -> None:
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "queue_runtime.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "queue_runtime.start_queue_runtimes" in source

    tree = ast.parse(source, filename=str(daemon_path))
    forbidden_modules = {
        "trader.application.decide.handler",
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



def test_daemon_delegates_data_source_bootstrap_to_runtime_adapter() -> None:
    repo_root = REPO_ROOT
    daemon_path = repo_root / "trader" / "runtime" / "daemon.py"
    adapter_path = repo_root / "trader" / "runtime" / "data_source_runtime.py"

    assert adapter_path.exists()

    source = daemon_path.read_text(encoding="utf-8")
    assert "data_source_runtime.load_data_source_config" in source
    assert "data_source_runtime.build_data_source" in source
    assert "data_source_runtime.maybe_attach_ib" in source
    assert "data_source_runtime.detach_failed_ib" in source
    assert "from trader.market.data_source import" not in source
    assert "from trader.market.ib_source import" not in source
    assert "from trader.infrastructure.market_sources.data_source import" in source
    assert "from trader.infrastructure.market_sources.ib_source import" in source

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



def test_data_source_runtime_uses_infrastructure_market_source_adapters() -> None:
    repo_root = REPO_ROOT
    source = (repo_root / "trader" / "runtime" / "data_source_runtime.py").read_text(
        encoding="utf-8"
    )

    assert "from trader.market.data_source import" not in source
    assert "from trader.market.ib_source import" not in source
    assert "from trader.infrastructure.market_sources.data_source import" in source
    assert "from trader.infrastructure.market_sources.ib_source import" in source



def test_daemon_delegates_market_rotation_tick_to_runtime_adapter() -> None:
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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



def test_protocols_are_colocated_with_their_consumers() -> None:
    from trader.application.decide.recent_decisions import DecisionLedgerReader
    from trader.application.record.decision_recorder import DecisionLedgerAppender
    from trader.reporting.audit.protocols import PriceHistoryLoader
    from trader.reporting.bench.protocols import BenchHistory, ModelBenchCompleter
    from trader.reporting.ledger.protocols import (
        DecisionLedgerAppender as LegacyDecisionLedgerAppender,
    )
    from trader.reporting.ledger.protocols import (
        DecisionLedgerReader as LegacyDecisionLedgerReader,
    )
    from trader.reporting.read_models.protocols import DecisionQualityScorer

    assert PriceHistoryLoader.__module__ == "trader.reporting.audit.protocols"
    assert BenchHistory.__module__ == "trader.reporting.bench.protocols"
    assert ModelBenchCompleter.__module__ == "trader.reporting.bench.protocols"
    assert DecisionLedgerAppender.__module__ == "trader.application.record.decision_recorder"
    assert DecisionLedgerReader.__module__ == "trader.application.decide.recent_decisions"
    assert LegacyDecisionLedgerAppender is DecisionLedgerAppender
    assert LegacyDecisionLedgerReader is DecisionLedgerReader
    assert DecisionQualityScorer.__module__ == "trader.reporting.read_models.protocols"



def test_shared_protocols_use_protocols_modules_instead_of_ports_modules() -> None:
    repo_root = REPO_ROOT
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
    from trader.application.execute.protocols import Broker as CanonicalBroker
    from trader.application.execute.protocols import CommissionModel as CanonicalCommissionModel
    from trader.execution.protocols import Broker, CommissionModel
    from trader.market.protocols import DataSource

    assert Broker is CanonicalBroker
    assert CommissionModel is CanonicalCommissionModel
    assert Broker.__module__ == "trader.application.execute.protocols"
    assert CommissionModel.__module__ == "trader.application.execute.protocols"
    assert DataSource.__module__ == "trader.market.protocols"

    repo_root = REPO_ROOT
    ports_paths = (
        repo_root / "trader" / "execution" / "ports.py",
        repo_root / "trader" / "market" / "ports.py",
    )

    assert [path.relative_to(repo_root) for path in ports_paths if path.exists()] == []



def test_planned_exits_uses_canonical_trade_plan_store_protocol() -> None:
    from trader.application.exit import planned_exits
    from trader.domain.planning.protocols import TradePlanStoreLike
    from trader.planning.protocols import TradePlanStoreLike as FacadeTradePlanStoreLike

    assert planned_exits.TradePlanStoreLike is TradePlanStoreLike
    assert FacadeTradePlanStoreLike is TradePlanStoreLike
    assert TradePlanStoreLike.__module__ == "trader.domain.planning.protocols"

    path = REPO_ROOT / "trader" / "application" / "exit" / "planned_exits.py"
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

    repo_root = REPO_ROOT
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
