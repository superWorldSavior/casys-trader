import ast
import subprocess
import sys
from pathlib import Path


def test_capability_modules_are_not_flat_files() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    flat_files = sorted(path.name for path in trader_dir.glob("*.py"))

    assert flat_files == ["__init__.py"]


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
    assert legacy_codex_client.Decision.__module__ == "trader.agent_protocol.types"
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
                        alias.name
                        for alias in node.names
                        if f"trader.tools.{alias.name}" in forbidden_modules
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


def test_execution_broker_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.execution.broker import (
        Broker,
        Fill,
        IbkrCommissionModel,
        NoCommissionModel,
        Order,
        SimBroker,
    )
    from trader.tools.execution import Broker as LegacyBroker
    from trader.tools.execution import Fill as LegacyFill
    from trader.tools.execution import IbkrCommissionModel as LegacyIbkrCommissionModel
    from trader.tools.execution import NoCommissionModel as LegacyNoCommissionModel
    from trader.tools.execution import Order as LegacyOrder
    from trader.tools.execution import SimBroker as LegacySimBroker

    assert LegacyBroker is Broker
    assert LegacyFill is Fill
    assert LegacyIbkrCommissionModel is IbkrCommissionModel
    assert LegacyNoCommissionModel is NoCommissionModel
    assert LegacyOrder is Order
    assert LegacySimBroker is SimBroker


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
        "import trader.agent_protocol.prompts; "
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

from trader.agent_protocol import IndicatorRequest

assert IndicatorRequest.__module__ == "trader.agent_protocol.types"
loaded = set(sys.modules)
for name in (
    "trader.market",
    "trader.planning",
    "trader.reporting",
    "trader.semantic",
    "trader.agent",
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
