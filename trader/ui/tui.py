"""tui — CLI live pour le rendu Rich casys-trader.

Les lectures d'état vivent dans :mod:`trader.read_models.runtime_state`.
Les builders Rich purs vivent dans :mod:`trader.ui.rich_panels`.

Ce module reste la façade historique pour les tests et les imports existants.
"""

from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live

from trader.read_models.runtime_state import (  # noqa: F401
    UTC,
    _COMPANY_NAMES_CACHE,
    _COMPANY_NAMES_PATH,
    _CURRENT_REPORT_FILE,
    _LAST_REPORT_FILE,
    _ROOT,
    _STATE_DIR,
    _STATUS_FILE,
    _compute_attribution_safe,
    _compute_live_kpis_safe,
    _count_pending_learnings_safe,
    _enrich_decisions_with_data_source,
    _format_datetime,
    _load_company_names,
    _load_consolidation_status_safe,
    _load_equity_curve,
    _load_fills_safe,
    _load_indicator_watches_safe,
    _load_learnings_safe,
    _load_scheduler_data_safe,
    _load_starting_cash_safe,
    _load_trade_plans_safe,
    _load_universe_symbols_safe,
    _load_venue_open_state_safe,
    _read_min_trade_confidence_safe,
    _safe_float,
    _safe_list_of_dicts,
    _tail_decisions_safe,
    _watch_is_expired,
    load_runtime_state,
    load_state,
)
from trader.ui.rich_panels import (  # noqa: F401
    _ACTION_VENUES,
    _MAX_HOTLIST_DISPLAY,
    _SPARK_BLOCKS,
    _build_armed_plans_panel,
    _build_attribution_panel,
    _build_data_health_panel,
    _build_decisions_table,
    _build_equity_panel,
    _build_exit_plans_enriched,
    _build_exit_plans_panel,
    _build_kpi_band,
    _build_kpi_compact,
    _build_learnings_panel,
    _build_llm_activity_panel,
    _build_positions_panel,
    _build_watches_panel,
    _confidence_bar,
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
    _holding_round_trip_fee_for_display,
    _holding_unrealized_pnl_for_display,
    _style_for_pnl,
    _truncate,
    build_closed_trades_table,
    build_selection_panel,
    build_trades_table,
    build_universe_panel,
    build_view,
    compute_realized_pnl_by_fill,
    sparkline,
)

_KILL_FILE = _ROOT / "KILL"


def main() -> None:
    """Lance le TUI en mode live. Ctrl+C pour quitter proprement."""
    console = Console()

    try:
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while True:
                raw = load_runtime_state()
                kill_active = _KILL_FILE.exists()
                raw = {**raw, "kill_switch": kill_active}
                live.update(build_view(raw))
                time.sleep(2.0)
    except KeyboardInterrupt:
        console.print("[yellow]TUI arrêté.[/yellow]")


if __name__ == "__main__":
    main()
