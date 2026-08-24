from __future__ import annotations

import ast
import importlib
from pathlib import Path


MOVED_MODULES = (
    "features",
    "regime",
    "family_regime",
    "volatility",
    "fx",
    "gross_priority",
    "execution_eligibility",
)

MARKET_DATA_IMPORT_STAR_EXPORTS = (
    "annotations",
    "functools",
    "Iterable",
    "dataclass",
    "datetime",
    "timedelta",
    "timezone",
    "ZoneInfo",
    "Bar",
    "MarketError",
    "Freshness",
    "freshness_budget_minutes",
    "assess_freshness",
    "human_clock",
    "market_clocks",
    "session_context",
    "session_snapshot",
    "PRE_OPEN_LEAD_MINUTES",
    "OPEN_GRACE_MINUTES",
    "next_regular_session_open",
    "most_recent_session_open",
    "last_completed_session_date",
    "assess_daily_freshness",
    "clamp_wake_to_session_open",
    "aggregate_bars",
    "classify_symbol_context",
    "explicit_mic_assignment",
    "explicit_mic_for_symbol",
    "Quote",
    "get_bars",
    "get_quote",
)
SESSION_EXPORTS = tuple(
    name
    for name in MARKET_DATA_IMPORT_STAR_EXPORTS
    if name not in {"Quote", "get_bars", "get_quote"}
)

FORBIDDEN_DOMAIN_IMPORT_PREFIXES = (
    "trader.market",
    "trader.application",
    "trader.runtime",
    "trader.infrastructure",
    "trader.planning",
)


def _facade_attribute_names(module: object) -> set[str]:
    return {
        name for name in dir(module)
        if not (name.startswith("__") and name.endswith("__"))
    }


def test_domain_market_modules_exist_and_market_facades_reexport_attributes() -> None:
    for module_name in MOVED_MODULES:
        domain_module = importlib.import_module(f"trader.domain.market.{module_name}")
        facade_module = importlib.import_module(f"trader.market.{module_name}")

        missing = _facade_attribute_names(domain_module) - _facade_attribute_names(facade_module)

        assert not missing, f"trader.market.{module_name} misses facade names: {sorted(missing)}"


def test_domain_market_modules_do_not_import_outer_layers() -> None:
    domain_market_dir = Path("trader/domain/market")

    for module_path in domain_market_dir.glob("*.py"):
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        for node in ast.walk(tree):
            import_name: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_name = alias.name
                    assert not import_name.startswith(FORBIDDEN_DOMAIN_IMPORT_PREFIXES), (
                        f"{module_path} imports forbidden outer module {import_name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                import_name = node.module
                if import_name is not None:
                    assert not import_name.startswith(FORBIDDEN_DOMAIN_IMPORT_PREFIXES), (
                        f"{module_path} imports forbidden outer module {import_name}"
                    )


def test_market_sessions_move_preserves_exchange_calendar_and_import_star_surface() -> None:
    sessions_path = Path("trader/domain/market/sessions.py")
    facade_path = Path("trader/market/market_data.py")

    assert sessions_path.exists()
    sessions_source = sessions_path.read_text(encoding="utf-8")
    assert "import exchange_calendars as _ec" in sessions_source
    assert "trader.market.market_data_yf" not in sessions_source
    assert "from trader.domain.market_data import Bar, MarketError" in sessions_source

    facade_source = facade_path.read_text(encoding="utf-8")
    assert "from trader.domain.market.sessions import *" in facade_source
    assert "from trader.market.market_data_yf import Quote, get_bars, get_quote" in facade_source

    sessions = importlib.import_module("trader.domain.market.sessions")
    market_data = importlib.import_module("trader.market.market_data")

    assert tuple(sessions.__all__) == SESSION_EXPORTS
    assert tuple(market_data.__all__) == MARKET_DATA_IMPORT_STAR_EXPORTS
    assert {name for name in market_data.__all__} == {
        name for name in vars(market_data) if not name.startswith("_")
    }
    assert market_data.session_snapshot is sessions.session_snapshot
    assert market_data.Bar is sessions.Bar
    assert "Quote" not in sessions.__all__
