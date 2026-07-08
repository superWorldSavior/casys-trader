"""Top-level Rich dashboard composition."""

from __future__ import annotations

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.interfaces.ui.panels.common import (
    _fmt_fee_cost,
    _holding_notional_usd,
    _holding_round_trip_fee_for_display,
    _holding_unrealized_pnl_for_display,
    sparkline,
)
from trader.interfaces.ui.panels.decisions_panels import (
    _build_decisions_table,
    _build_learnings_panel,
)
from trader.interfaces.ui.panels.portfolio_panels import (
    _build_equity_panel,
    _build_kpi_band,
)
from trader.interfaces.ui.panels.positions_panels import (
    _build_attribution_panel,
    _build_positions_panel,
)
from trader.reporting.read_models.runtime_state import (
    _format_datetime,
    _safe_float,
    _safe_list_of_dicts,
)


def build_view(
    state: dict | None, *, palette: Palette = PALETTE_DARK
) -> RenderableType:
    """Construit l'affichage Rich à partir d'un dict d'état.

    Paramètres
    ----------
    state:
        Dict issu de `load_runtime_state`. Accepte None ou un dict partiel sans
        lever d'exception. Le champ optionnel ``kill_switch`` (bool) doit être
        injecté par l'appelant (non lu depuis le disque ici).
    palette:
        Design tokens Rich. Défaut PALETTE_DARK → comportement identique à l'existant.
    """
    # Normalisation défensive
    if not isinstance(state, dict):
        state = {}

    portfolio = (
        state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    )
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    attribution = (
        state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
    )
    holdings = _safe_list_of_dicts(portfolio.get("holdings"))
    decisions = _safe_list_of_dicts(state.get("decisions"))
    daemon_status = (
        state.get("daemon_status")
        if isinstance(state.get("daemon_status"), dict)
        else {}
    )
    equity_curve = [
        _safe_float(value, default=None) for value in (state.get("equity_curve") or [])
    ]
    equity_curve = [value for value in equity_curve if value is not None]
    learnings = _safe_list_of_dicts(state.get("learnings"))
    dry_run: bool = state.get("dry_run", True)
    ts = _format_datetime(state.get("ts"))
    source: str = str(state.get("source", "—"))
    kill_active: bool = state.get("kill_switch", False)
    halted: str | None = state.get("halted")

    # ------------------------------------------------------------------
    # Panel header — équité, cash, rendement, mode
    # ------------------------------------------------------------------
    cash_ledger = _safe_float(portfolio.get("cash_ledger") or portfolio.get("cash"), default=None)
    if cash_ledger is None:
        cash_ledger = _safe_float(kpis.get("cash"), default=0.0) or 0.0
    cash = _safe_float(portfolio.get("cash_available"), default=None)
    if cash is None:
        short_exposure = sum(
            _holding_notional_usd(h)
            for h in holdings
            if (_safe_float(h.get("quantity"), default=0.0) or 0.0) < 0.0
        )
        cash = cash_ledger - short_exposure
    equity = _safe_float(portfolio.get("equity"), default=None)
    if equity is None:
        equity = _safe_float(kpis.get("equity"), default=0.0) or 0.0
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        total_return = _safe_float(kpis.get("total_return"), default=0.0) or 0.0
        ret_pct = total_return * 100.0
    unrealized_total = sum(_holding_unrealized_pnl_for_display(h) for h in holdings)
    unrealized_fees = [
        fee
        for h in holdings
        if (fee := _holding_round_trip_fee_for_display(h)) is not None
    ]
    unrealized_fee_total = sum(unrealized_fees)
    phase = str(daemon_status.get("phase", "—"))
    current_symbol = str(daemon_status.get("current_symbol") or "—")
    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    progress = f"{done}/{total}" if done is not None and total is not None else "—"
    used = daemon_status.get("model_calls_used")
    limit = daemon_status.get("max_model_calls_per_cycle")
    calls = (
        f"{used}/{limit}"
        if used is not None and limit is not None
        else (str(used) if used is not None else "—")
    )

    mode_label = (
        Text("LIVE", style="bold red")
        if not dry_run
        else Text("DRY-RUN", style="bold yellow")
    )

    kill_label: Text
    if kill_active:
        kill_label = Text("KILL ACTIF", style="bold red on white")
    else:
        kill_label = Text("nominal", style=palette["pnl_positive"])

    if halted:
        halted_label = Text(f"  !! HALTED: {halted} !!", style=palette["pnl_negative"])
    else:
        halted_label = Text("")

    ret_style = palette["pnl_positive"] if ret_pct >= 0 else palette["pnl_negative"]
    unrealized_style = (
        palette["pnl_positive"] if unrealized_total >= 0 else palette["pnl_negative"]
    )
    inline_curve = sparkline(equity_curve[-32:]) if equity_curve else ""
    header_lines = Text.assemble(
        ("Équité $ : ", "bold"),
        (f"${equity:,.2f}", f"bold {palette['kpi_default']}"),
        (f"  {inline_curve}   " if inline_curve else "   ", palette["kpi_default"]),
        ("Cash libre $ : ", "bold"),
        (f"${cash:,.2f}   ", palette["kpi_default"]),
        ("Rendement : ", "bold"),
        (f"{ret_pct:+.2f}%   ", ret_style),
        ("PnL latent USD : ", "bold"),
        (f"${unrealized_total:+,.2f}", unrealized_style),
        (
            f" (dont frais {_fmt_fee_cost(unrealized_fee_total)})   "
            if unrealized_fees
            else "   ",
            palette["dim"],
        ),
        ("Mode : ", "bold"),
        mode_label,
        ("   Kill-switch : ", "bold"),
        kill_label,
        ("   Dernier cycle : ", "bold"),
        (ts, palette["dim"]),
        halted_label,
        "\n",
        ("Daemon : ", "bold"),
        (phase, palette["status_phase"]),
        ("   Symbole : ", "bold"),
        (current_symbol, palette["kpi_default"]),
        ("   Progrès : ", "bold"),
        (progress, palette["kpi_default"]),
        ("   Appels : ", "bold"),
        (calls, palette["kpi_default"]),
        ("   Source : ", "bold"),
        (source, palette["dim"]),
    )

    header_panel = Panel(
        header_lines, title="[bold]casys-trader — cockpit trading[/bold]", expand=True
    )
    body = Columns(
        [
            _build_positions_panel(holdings, palette=palette),
            _build_attribution_panel(attribution, palette=palette),
        ],
        equal=True,
        expand=True,
    )
    learnings_panel = _build_learnings_panel(learnings, palette=palette)
    footer = (
        Group(_build_decisions_table(decisions, palette=palette), learnings_panel)
        if learnings_panel is not None
        else _build_decisions_table(decisions, palette=palette)
    )

    return Group(
        header_panel,
        _build_kpi_band(kpis, palette=palette),
        _build_equity_panel(equity_curve, palette=palette),
        body,
        footer,
    )


__all__ = ["build_view"]
