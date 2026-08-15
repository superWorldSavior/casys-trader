import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
    _has_python_sources,
)


def test_only_legacy_compat_modules_are_flat_files() -> None:
    trader_dir = REPO_ROOT / "trader"

    flat_files = sorted(path.name for path in trader_dir.glob("*.py"))

    assert flat_files == ["__init__.py"]



def test_internal_sources_do_not_import_virtual_flat_compat_modules() -> None:
    repo_root = REPO_ROOT
    trader_init = repo_root / "trader" / "__init__.py"
    init_tree = ast.parse(
        trader_init.read_text(encoding="utf-8"),
        filename=str(trader_init),
    )
    compat_assignment = next(
        node
        for node in init_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_COMPAT_MODULES"
            for target in node.targets
        )
    )
    compat_modules = set(ast.literal_eval(compat_assignment.value))
    assert "ledger_rotation" not in compat_modules

    violations: list[str] = []
    checked_roots = (
        repo_root / "trader",
        repo_root / "backtest",
        repo_root / "scripts",
    )
    for checked_root in checked_roots:
        for path in sorted(checked_root.rglob("*.py")):
            # This file owns the virtual aliases. Tests are intentionally outside
            # checked_roots because they also prove the remaining public shims.
            if path == trader_init or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if len(parts) >= 2 and parts[0] == "trader" and parts[1] in compat_modules:
                            violations.append(
                                f"{relative_path}:{node.lineno}: import {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    if node.module == "trader":
                        for alias in node.names:
                            if alias.name in compat_modules:
                                violations.append(
                                    f"{relative_path}:{node.lineno}: "
                                    f"from trader import {alias.name}"
                                )
                        continue
                    parts = node.module.split(".")
                    if len(parts) >= 2 and parts[0] == "trader" and parts[1] in compat_modules:
                        violations.append(
                            f"{relative_path}:{node.lineno}: from {node.module} import ..."
                        )

    # Command strings such as ``python -m trader.daemon`` are deliberate public
    # entrypoints and are not imports, so this AST-only guard leaves them intact.
    assert violations == []



def test_legacy_tools_package_is_virtual_compatibility_layer(monkeypatch) -> None:
    trader_dir = REPO_ROOT / "trader"

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



def test_support_and_read_model_legacy_packages_are_virtual() -> None:
    trader_dir = REPO_ROOT / "trader"

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



def test_operator_interface_legacy_packages_are_virtual() -> None:
    trader_dir = REPO_ROOT / "trader"

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
    trader_dir = REPO_ROOT / "trader"

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
    trader_dir = REPO_ROOT / "trader"

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
    trader_dir = REPO_ROOT / "trader"

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
    trader_dir = REPO_ROOT / "trader"

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
    trader_dir = REPO_ROOT / "trader"

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
    from trader.domain.execution.risk_gate import RiskGate as DomainRiskGate
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
    assert RiskGate is DomainRiskGate
    assert RiskGate.__module__ == "trader.domain.execution.risk_gate"
    assert build_view.__module__ == "trader.interfaces.ui.panels.dashboard"



def test_legacy_flat_modules_are_virtual_compatibility_layers() -> None:
    trader_dir = REPO_ROOT / "trader"

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



def test_legacy_news_feed_tool_module_proxies_mutations_to_canonical_module(monkeypatch) -> None:
    from trader.market import news_feed as canonical_news_feed
    from trader.tools import news_feed as legacy_news_feed

    sentinel = object()
    monkeypatch.setattr(legacy_news_feed, "_compat_probe", sentinel, raising=False)

    assert canonical_news_feed._compat_probe is sentinel



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

