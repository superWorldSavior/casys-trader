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
    world_dynamics = (
        state.get("world_dynamics") if isinstance(state.get("world_dynamics"), dict) else {}
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
    equity = _safe_float(portfolio.get("equity"), default=None)
    if equity is None:
        equity = _safe_float(kpis.get("equity"), default=None)
    gross_exposure = _safe_float(portfolio.get("gross_exposure_usd"), default=None)
    if gross_exposure is None and isinstance(portfolio.get("holdings"), list):
        gross_exposure = sum(_holding_notional_usd(holding) for holding in holdings)
    capital_uncommitted = (
        max(0.0, equity - gross_exposure)
        if equity is not None and gross_exposure is not None
        else None
    )
    if capital_uncommitted is None:
        for raw_cash in (
            portfolio.get("cash_available"),
            portfolio.get("cash"),
            kpis.get("cash"),
        ):
            capital_uncommitted = _safe_float(raw_cash, default=None)
            if capital_uncommitted is not None:
                break
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        total_return = _safe_float(kpis.get("total_return"), default=None)
        ret_pct = total_return * 100.0 if total_return is not None else None
    gross_unrealized = [
        _safe_float(holding.get("unrealized_pnl"), default=None)
        for holding in holdings
    ]
    net_unrealized = [
        _safe_float(
            holding.get("unrealized_pnl_after_broker_fees"),
            default=None,
        )
        if holding.get("unrealized_pnl_after_broker_fees") is not None
        else _safe_float(holding.get("unrealized_pnl_net"), default=None)
        for holding in holdings
    ]
    net_coverage = sum(value is not None for value in net_unrealized)
    net_complete = bool(holdings) and net_coverage == len(holdings)
    gross_complete = all(value is not None for value in gross_unrealized)
    if not holdings:
        unrealized_total: float | None = 0.0
        unrealized_is_gross = False
    elif net_complete:
        unrealized_total = sum(value for value in net_unrealized if value is not None)
        unrealized_is_gross = False
    elif gross_complete:
        unrealized_total = sum(value for value in gross_unrealized if value is not None)
        unrealized_is_gross = True
    else:
        unrealized_total = None
        unrealized_is_gross = True
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

    ret_style = (
        palette["dim"]
        if ret_pct is None
        else (
            palette["pnl_positive"]
            if ret_pct >= 0
            else palette["pnl_negative"]
        )
    )
    unrealized_style = (
        palette["dim"]
        if unrealized_total is None
        else (
            palette["pnl_positive"]
            if unrealized_total >= 0
            else palette["pnl_negative"]
        )
    )
    if unrealized_is_gross and holdings:
        unrealized_detail = (
            f" (net — · frais {net_coverage}/{len(holdings)})   "
        )
    elif unrealized_fees and len(unrealized_fees) == len(holdings):
        unrealized_detail = (
            f" (dont frais {_fmt_fee_cost(unrealized_fee_total)}, "
            "entrée + sortie estimées; hors taxes/place)   "
        )
    elif holdings:
        unrealized_detail = " (net après courtage; détail des frais incomplet)   "
    else:
        unrealized_detail = "   "
    inline_curve = sparkline(equity_curve[-32:]) if equity_curve else ""
    header_lines = Text.assemble(
        ("Équité $ : ", "bold"),
        (
            f"${equity:,.2f}" if equity is not None else "—",
            f"bold {palette['kpi_default']}",
        ),
        (f"  {inline_curve}   " if inline_curve else "   ", palette["kpi_default"]),
        ("Capital non engagé $ : ", "bold"),
        (
            f"${capital_uncommitted:,.2f}   "
            if capital_uncommitted is not None
            else "—   ",
            palette["kpi_default"],
        ),
        ("Rendement : ", "bold"),
        (f"{ret_pct:+.2f}%   " if ret_pct is not None else "—   ", ret_style),
        (
            "PnL latent USD brut : " if unrealized_is_gross else "PnL latent USD : ",
            "bold",
        ),
        (
            f"${unrealized_total:+,.2f}" if unrealized_total is not None else "—",
            unrealized_style,
        ),
        (unrealized_detail, palette["dim"]),
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
        "\n",
        ("World dynamics shadow : ", "bold"),
        (str(world_dynamics.get("status", "not_started")), palette["status_phase"]),
        ("   Dernière exécution : ", "bold"),
        (_format_datetime(world_dynamics.get("last_run_at")), palette["dim"]),
        ("   casys-trader world dynamics status", palette["dim"]),
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
