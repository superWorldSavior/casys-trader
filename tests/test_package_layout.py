import ast
import subprocess
import sys
from pathlib import Path


def test_only_legacy_compat_modules_are_flat_files() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    flat_files = sorted(path.name for path in trader_dir.glob("*.py"))

    assert flat_files == [
        "__init__.py",
        "attribution.py",
        "cli.py",
        "daemon.py",
        "stats.py",
        "tool_usage.py",
        "tui.py",
    ]


def test_top_level_packages_have_declared_architecture_roles() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    def _has_python_sources(path: Path) -> bool:
        return any("__pycache__" not in candidate.parts for candidate in path.rglob("*.py"))

    actual = {
        path.name
        for path in trader_dir.iterdir()
        if path.is_dir() and path.name != "__pycache__" and _has_python_sources(path)
    }
    canonical_packages = {
        "agent",
        "application",
        "cockpit",
        "commands",
        "domain",
        "execution",
        "learnings",
        "market",
        "planning",
        "queue",
        "reporting",
        "rotation",
        "runtime",
        "scheduling",
        "semantic",
        "state_db",
        "support",
        "ui",
    }
    compatibility_facades = set()

    assert actual == canonical_packages | compatibility_facades


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
    from trader.agent.protocol.parsing import parse_batch
    from trader.agent.protocol import IndicatorRequest
    from trader.agent.tools import core, registry
    from trader.agent_protocol.parsing import parse_batch as legacy_parse_batch
    from trader.agent_tools.core import ToolContext as LegacyToolContext

    assert getattr(legacy_protocol, "__file__", None) is None
    assert getattr(legacy_protocol, "__path__", None) == []
    assert getattr(legacy_tools, "__file__", None) is None
    assert getattr(legacy_tools, "__path__", None) == []
    assert IndicatorRequest.__module__ == "trader.agent.protocol.types"
    assert legacy_parse_batch is parse_batch
    assert legacy_tools.TOOL_REGISTRY is registry.TOOL_REGISTRY
    assert LegacyToolContext is core.ToolContext

    sentinel = object()
    monkeypatch.setattr(legacy_tools.core, "_compat_probe", sentinel, raising=False)

    assert core._compat_probe is sentinel


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
    import trader.daemon as legacy_daemon
    import trader.fx as legacy_fx
    import trader.llm as legacy_llm
    import trader.palette as legacy_palette
    import trader.rotation_schedule as legacy_schedule
    import trader.stats as legacy_stats
    from trader.cockpit import CockpitApp
    from trader import decision_ledger
    from trader.indicator_watch import WATCH_VALID_OPERATORS
    from trader.risk import RiskGate
    from trader.tui import build_view

    assert legacy_cockpit_events.__name__ == "trader.cockpit.events"
    assert legacy_codex_client.Decision.__module__ == "trader.agent.protocol.types"
    assert legacy_daemon.run_cycle.__module__ == "trader.runtime.daemon"
    assert legacy_fx.__name__ == "trader.market.fx"
    assert legacy_llm.LlmRouter.__module__ == "trader.agent.llm"
    assert legacy_palette.__name__ == "trader.ui.palette"
    assert legacy_schedule.__name__ == "trader.rotation.schedule"
    assert legacy_stats.compute_live_kpis.__module__ == "trader.reporting.stats"
    assert decision_ledger.__name__ == "trader.reporting.decision_ledger"
    assert CockpitApp.__module__ == "trader.cockpit.app"
    assert ">" in WATCH_VALID_OPERATORS
    assert RiskGate.__module__ == "trader.execution.risk"
    assert build_view.__module__ == "trader.ui.rich_panels"


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


def test_runnable_compatibility_facades_delegate_to_command_modules() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    command_modules = {
        "attribution": "trader.reporting.attribution",
        "stats": "trader.reporting.stats",
        "tool_usage": "trader.reporting.tool_usage",
        "tui": "trader.ui.tui",
    }

    for command_name, canonical_module in command_modules.items():
        command_path = trader_dir / "commands" / f"{command_name}.py"
        legacy_package_main_path = trader_dir / command_name / "__main__.py"
        legacy_module_path = trader_dir / f"{command_name}.py"

        assert command_path.exists(), f"missing canonical command module for {command_name}"
        assert _module_imports(command_path, canonical_module)
        if legacy_package_main_path.exists():
            assert _module_imports(legacy_package_main_path, f"trader.commands.{command_name}")
        else:
            assert legacy_module_path.exists(), f"missing legacy module shim for {command_name}"
            assert _module_imports(legacy_module_path, f"trader.commands.{command_name}")


def test_reporting_command_python_m_entrypoints() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for module_name in (
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

    assert command_tool_usage.main.__module__ == "trader.commands.tool_usage"
    assert not hasattr(reporting_tool_usage, "main")


def test_stats_cli_owner_is_command_module() -> None:
    from trader.commands import stats as command_stats
    from trader.reporting import stats as reporting_stats

    assert command_stats.main.__module__ == "trader.commands.stats"
    assert not hasattr(reporting_stats, "main")


def test_attribution_cli_owner_is_command_module() -> None:
    from trader.commands import attribution as command_attribution
    from trader.reporting import attribution as reporting_attribution

    assert command_attribution.main.__module__ == "trader.commands.attribution"
    assert not hasattr(reporting_attribution, "main")


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
    from trader.market.ports import DataSource
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
    from trader.execution.ports import Broker as CanonicalBroker
    from trader.execution.ports import CommissionModel as CanonicalCommissionModel
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
    from trader.learnings.raw_store import LearningsStore
    from trader.learnings.raw_store import RawLearningsStore
    from trader.tools.memory import LearningsStore as LegacyLearningsStore
    from trader.tools.memory import Memory as LegacyMemory
    from trader.tools.memory import RawLearningsStore as LegacyRawLearningsStore

    assert LegacyMemory is Memory
    assert LearningsStore is RawLearningsStore
    assert LegacyLearningsStore is RawLearningsStore
    assert LegacyRawLearningsStore is RawLearningsStore


def test_legacy_memory_tool_module_proxies_mutations_to_canonical_modules(monkeypatch) -> None:
    from trader.agent import memory as canonical_agent_memory
    from trader.learnings import raw_store as canonical_raw_store
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
        trader_dir / "learnings",
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
    from trader.scheduling.scheduler import (
        STALE_BACKOFF_MAX_MINUTES,
        STALE_BACKOFF_MAX_STREAK,
        Scheduler,
    )
    from trader.tools.scheduler import STALE_BACKOFF_MAX_MINUTES as LegacyMaxMinutes
    from trader.tools.scheduler import STALE_BACKOFF_MAX_STREAK as LegacyMaxStreak
    from trader.tools.scheduler import Scheduler as LegacyScheduler

    assert LegacyScheduler is Scheduler
    assert LegacyMaxMinutes == STALE_BACKOFF_MAX_MINUTES
    assert LegacyMaxStreak == STALE_BACKOFF_MAX_STREAK


def test_scheduling_package_does_not_depend_on_tools_package() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    scheduling_dir = trader_dir / "scheduling"

    violations: list[str] = []
    for path in sorted(scheduling_dir.rglob("*.py")):
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

assert IndicatorRequest.__module__ == "trader.agent.protocol.types"
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
