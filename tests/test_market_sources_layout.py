from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path


ADAPTER_MODULES = {
    "data_source": ("YFinanceDataSource", "CompositeDataSource", "ThrottledDataSource"),
    "market_data_yf": ("get_bars", "get_quote", "Quote"),
    "fx_rates": ("load_fx_config", "rates_for_symbols"),
    "macro_series": ("collect_daily", "maybe_collect", "_extract_last_observation"),
    "radar_data": ("fetch_daily", "download_daily_batch", "CoverageError"),
    "ib_source": ("IBDataSource", "connect_ib", "INTERVAL_MAP", "LOOKBACK_MAP"),
    "news_feed": ("RawNews", "NewsItemsArchive", "set_default_news_archive", "build_snapshot", "news_snapshot"),
}

MARKET_IO_EXCEPTIONS = set(ADAPTER_MODULES) | {"news_feed"}
FORBIDDEN_IO_IMPORT_ROOTS = {"aiohttp", "httpx", "ib_async", "requests", "urllib", "yfinance"}


def test_market_source_adapters_live_under_infrastructure_with_market_facades() -> None:
    for module_name, public_names in ADAPTER_MODULES.items():
        infra = importlib.import_module(f"trader.infrastructure.market_sources.{module_name}")
        facade = importlib.import_module(f"trader.market.{module_name}")

        source_path = Path(inspect.getsourcefile(infra) or "")
        assert source_path.parts[-4:] == ("trader", "infrastructure", "market_sources", f"{module_name}.py")
        assert facade is infra
        for public_name in public_names:
            assert getattr(facade, public_name) is getattr(infra, public_name)


def test_news_feed_market_facade_reexports_complete_infrastructure_adapter() -> None:
    infra = importlib.import_module("trader.infrastructure.market_sources.news_feed")
    facade = importlib.import_module("trader.market.news_feed")

    exported_names = sorted(name for name in dir(infra) if not name.startswith("__"))
    assert facade.__all__ == exported_names
    for name in exported_names:
        assert getattr(facade, name) is getattr(infra, name)


def test_news_feed_infrastructure_adapter_does_not_import_application_or_runtime() -> None:
    path = Path(__file__).resolve().parents[1] / "trader" / "infrastructure" / "market_sources" / "news_feed.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("trader.application", "trader.runtime")):
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(("trader.application", "trader.runtime")):
                violations.append(f"from {node.module} import ...")

    assert violations == []


def test_market_domain_modules_do_not_import_external_market_io_clients() -> None:
    market_dir = Path(__file__).resolve().parents[1] / "trader" / "market"
    violations: list[str] = []

    for path in sorted(market_dir.rglob("*.py")):
        if path.parent.name == "__pycache__":
            continue
        if path.stem in MARKET_IO_EXCEPTIONS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(market_dir.parents[1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in FORBIDDEN_IO_IMPORT_ROOTS:
                        violations.append(f"{rel_path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if root in FORBIDDEN_IO_IMPORT_ROOTS:
                    violations.append(f"{rel_path}: from {node.module} import ...")

    assert violations == []
