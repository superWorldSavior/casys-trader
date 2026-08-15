import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
)


def test_internal_code_uses_canonical_interface_imports() -> None:
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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
    repo_root = REPO_ROOT
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



def test_internal_backtest_scripts_use_canonical_imports_instead_of_legacy_facades() -> None:
    repo_root = REPO_ROOT
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



def test_core_packages_do_not_depend_on_legacy_news_feed_tool() -> None:
    trader_dir = REPO_ROOT / "trader"
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
    trader_dir = REPO_ROOT / "trader"
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
    from trader.domain.contracts import Commission as CanonicalCommission
    from trader.domain.contracts import CommissionModelName as CanonicalCommissionModelName
    from trader.domain.contracts import Fill as CanonicalFill
    from trader.domain.contracts import Order as CanonicalOrder
    from trader.domain.contracts import Position as CanonicalPosition
    from trader.application.execute.protocols import Broker as CanonicalBroker
    from trader.application.execute.protocols import CommissionModel as CanonicalCommissionModel
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



def test_internal_code_does_not_import_execution_compatibility_facades() -> None:
    repo_root = REPO_ROOT
    trader_dir = repo_root / "trader"
    compatibility_dir = trader_dir / "execution"
    forbidden_prefix = "trader.execution"
    violations: list[str] = []

    for root in (trader_dir, repo_root / "backtest", repo_root / "scripts"):
        for path in sorted(root.rglob("*.py")):
            if path == trader_dir / "__init__.py" or compatibility_dir in path.parents:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module == forbidden_prefix or node.module.startswith(f"{forbidden_prefix}.")
                ):
                    violations.append(f"{relative_path}:{node.lineno}: from {node.module} import ...")
                elif isinstance(node, ast.Import):
                    violations.extend(
                        f"{relative_path}:{node.lineno}: import {alias.name}"
                        for alias in node.names
                        if alias.name == forbidden_prefix or alias.name.startswith(f"{forbidden_prefix}.")
                    )

    assert violations == []



def test_portfolio_imports_are_canonical_with_tools_compatibility() -> None:
    repo_root = REPO_ROOT
    facade_path = repo_root / "trader" / "execution" / "portfolio.py"
    facade_tree = ast.parse(
        facade_path.read_text(encoding="utf-8"),
        filename=str(facade_path),
    )

    assert not any(isinstance(node, (ast.ClassDef, ast.FunctionDef)) for node in facade_tree.body)

    from trader.application.portfolio.snapshot import snapshot
    from trader.domain.portfolio.snapshot import Holding, Snapshot
    from trader.execution.portfolio import Holding as FacadeHolding
    from trader.execution.portfolio import Snapshot as FacadeSnapshot
    from trader.execution.portfolio import snapshot as facade_snapshot
    from trader.tools.portfolio import Holding as LegacyHolding
    from trader.tools.portfolio import Snapshot as LegacySnapshot
    from trader.tools.portfolio import snapshot as legacy_snapshot

    assert FacadeHolding is Holding
    assert FacadeSnapshot is Snapshot
    assert facade_snapshot is snapshot
    assert LegacyHolding is Holding
    assert LegacySnapshot is Snapshot
    assert legacy_snapshot is snapshot



def test_runtime_and_execution_do_not_depend_on_legacy_portfolio_tool() -> None:
    trader_dir = REPO_ROOT / "trader"
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
    trader_dir = REPO_ROOT / "trader"
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
    trader_dir = REPO_ROOT / "trader"
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

