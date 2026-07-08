"""Themed Rich panel builders for casys-trader."""

from __future__ import annotations

from trader.interfaces.ui.panels.common import (
    _SPARK_BLOCKS,
    _expire_relative,
    _fmt_fee_cost,
    _fmt_holding_duration,
    _fmt_int,
    _fmt_money,
    _fmt_number,
    _fmt_percent,
    _fmt_price_4,
    _fmt_price_pair,
    _fmt_signed_money,
    _fmt_symbol_short,
    _fmt_time_hms,
    _holding_notional_usd,
    _holding_round_trip_fee_for_display,
    _holding_unrealized_pnl_for_display,
    _market_badge,
    _style_for_pnl,
    _truncate,
    sparkline,
)
from trader.interfaces.ui.panels.dashboard import build_view
from trader.interfaces.ui.panels.decisions_panels import (
    _build_data_health_panel,
    _build_decisions_table,
    _build_learnings_panel,
    _build_llm_activity_panel,
)
from trader.interfaces.ui.panels.plans_panels import (
    _build_armed_plans_panel,
    _build_exit_plans_enriched,
    _build_exit_plans_panel,
)
from trader.interfaces.ui.panels.portfolio_panels import (
    _build_equity_panel,
    _build_kpi_band,
    _build_kpi_compact,
)
from trader.interfaces.ui.panels.positions_panels import (
    _build_attribution_panel,
    _build_positions_panel,
    _confidence_bar,
    build_closed_trades_table,
    build_trades_table,
    compute_realized_pnl_by_fill,
)
from trader.interfaces.ui.panels.universe_panels import (
    _ACTION_VENUES,
    _MAX_HOTLIST_DISPLAY,
    build_selection_panel,
    build_universe_panel,
)
from trader.interfaces.ui.panels.watches_panels import _build_watches_panel

__all__ = [
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
]
