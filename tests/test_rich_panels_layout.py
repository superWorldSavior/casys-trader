"""Layout checks for the Rich panel builders facade."""

from __future__ import annotations

import importlib


EXPECTED_RICH_PANEL_EXPORTS = {
    "_ACTION_VENUES",
    "_MAX_HOTLIST_DISPLAY",
    "_SPARK_BLOCKS",
    "_build_armed_plans_panel",
    "_build_attribution_panel",
    "_build_data_health_panel",
    "_build_decisions_table",
    "_build_equity_panel",
    "_build_exit_plans_enriched",
    "_build_exit_plans_panel",
    "_build_kpi_band",
    "_build_kpi_compact",
    "_build_learnings_panel",
    "_build_llm_activity_panel",
    "_build_positions_panel",
    "_build_watches_panel",
    "_confidence_bar",
    "_expire_relative",
    "_fmt_fee_cost",
    "_fmt_holding_duration",
    "_fmt_int",
    "_fmt_money",
    "_fmt_number",
    "_fmt_percent",
    "_fmt_price_4",
    "_fmt_price_pair",
    "_fmt_signed_money",
    "_fmt_symbol_short",
    "_fmt_time_hms",
    "_holding_notional_usd",
    "_holding_round_trip_fee_for_display",
    "_holding_unrealized_pnl_for_display",
    "_market_badge",
    "_style_for_pnl",
    "_truncate",
    "build_closed_trades_table",
    "build_selection_panel",
    "build_trades_table",
    "build_universe_panel",
    "build_view",
    "compute_realized_pnl_by_fill",
    "sparkline",
}


EXPECTED_THEME_MODULES = {
    "common",
    "dashboard",
    "decisions_panels",
    "plans_panels",
    "portfolio_panels",
    "positions_panels",
    "universe_panels",
    "watches_panels",
}


def test_rich_panels_is_complete_facade_over_theme_modules() -> None:
    facade = importlib.import_module("trader.interfaces.ui.rich_panels")
    panels_pkg = importlib.import_module("trader.interfaces.ui.panels")

    assert set(facade.__all__) == EXPECTED_RICH_PANEL_EXPORTS
    assert set(panels_pkg.__all__) == EXPECTED_RICH_PANEL_EXPORTS

    for name in EXPECTED_RICH_PANEL_EXPORTS:
        assert getattr(facade, name) is getattr(panels_pkg, name)

    for module_name in EXPECTED_THEME_MODULES:
        importlib.import_module(f"trader.interfaces.ui.panels.{module_name}")
