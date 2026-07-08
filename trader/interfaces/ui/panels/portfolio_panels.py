"""Portfolio, KPI, and equity Rich panels."""

from __future__ import annotations

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.interfaces.ui.panels.common import (
    _fmt_int,
    _fmt_number,
    _fmt_percent,
    sparkline,
)
from trader.reporting.read_models.runtime_state import _safe_float


def _build_kpi_band(kpis: dict, *, palette: Palette = PALETTE_DARK) -> RenderableType:
    sharpe = _safe_float(kpis.get("sharpe"), default=None)
    max_dd = _safe_float(kpis.get("max_drawdown"), default=None)
    win_rate = _safe_float(kpis.get("period_win_rate"), default=None)
    volatility = _safe_float(kpis.get("volatility"), default=None)
    trades = kpis.get("num_trades")

    def card(title: str, value: str, border: str, subtitle: str = "") -> Panel:
        body = Text.assemble((value, f"bold {border}"))
        if subtitle:
            body.append("\n")
            body.append(subtitle, style=palette["dim"])
        return Panel(
            body, title=f"[bold]{title}[/bold]", border_style=border, expand=True
        )

    sharpe_style = (
        palette["kpi_sharpe_ok"]
        if (sharpe is not None and sharpe >= 1.0)
        else (
            palette["kpi_sharpe_bad"] if (sharpe or 0.0) < 0 else palette["kpi_default"]
        )
    )
    win_style = (
        palette["kpi_sharpe_ok"]
        if (win_rate is not None and win_rate >= 0.5)
        else (
            palette["kpi_sharpe_bad"]
            if win_rate is not None
            else palette["kpi_default"]
        )
    )
    vol_style = (
        palette["kpi_default"]
        if volatility is None or volatility <= 0.25
        else palette["kpi_vol_warn"]
    )

    return Columns(
        [
            card("Sharpe", _fmt_number(sharpe), sharpe_style, "qualité risque"),
            card("Max DD", _fmt_percent(max_dd), palette["kpi_sharpe_bad"], "drawdown"),
            card("Win rate", _fmt_percent(win_rate), win_style, "période"),
            card("Volatilité", _fmt_percent(volatility), vol_style, "annualisée"),
            card(
                "Trades", _fmt_int(trades), palette["kpi_default"], "clôturés/exécutés"
            ),
        ],
        equal=True,
        expand=True,
    )


def _build_equity_panel(
    values: list[float], *, palette: Palette = PALETTE_DARK
) -> Panel:
    if len(values) < 2:
        return Panel(
            Text(
                "Courbe indisponible : moins de 2 points d'équité.",
                style=palette["equity_dim"],
            ),
            title="[bold]Équité[/bold]",
            border_style=palette["border_default"],
            expand=True,
        )

    def _rgb_from_palette(color: str) -> tuple[int, int, int] | str:
        text = color.strip()
        if text.startswith("#") and len(text) == 7:
            try:
                return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
            except ValueError:
                return text
        return text

    try:
        import plotext as plt

        plt.clear_figure()
        plt.theme("clear")
        plt.plotsize(70, 12)
        plt.plot(list(range(len(values))), values, marker="braille",
                 color=_rgb_from_palette(palette["equity_line"]))
        plt.title("Courbe d'équité")
        plt.xlabel("cycle")
        plt.ylabel("équité")
        chart: RenderableType = Text.from_ansi(plt.build())
    except Exception:
        chart = Text(sparkline(values[-70:]), style=f"bold {palette['equity_line']}")

    return Panel(
        chart,
        title="[bold]Équité[/bold]",
        border_style=palette["border_default"],
        expand=True,
    )


def _build_kpi_compact(
    kpis: dict,
    equity_curve: list[float],
    *,
    palette: Palette = PALETTE_DARK,
) -> RenderableType:
    """Version compacte de _build_kpi_band : 2 lignes inline + sparkline (PURE).

    Ligne 1 : Sharpe / MaxDD / WinRate / Vol / Trades
    Ligne 2 : sparkline des 32 derniers points d'équité
    """
    sharpe = _safe_float(kpis.get("sharpe"), default=None)
    max_dd = _safe_float(kpis.get("max_drawdown"), default=None)
    win_rate = _safe_float(kpis.get("period_win_rate"), default=None)
    volatility = _safe_float(kpis.get("volatility"), default=None)
    trades = kpis.get("num_trades")

    sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "—"
    dd_str = f"{max_dd * 100:.1f}%" if max_dd is not None else "—"
    wr_str = f"{win_rate * 100:.1f}%" if win_rate is not None else "—"
    vol_str = f"{volatility * 100:.1f}%" if volatility is not None else "—"
    trades_str = str(int(trades)) if trades is not None else "0"

    line1 = Text.assemble(
        ("Sharpe: ", palette["dim"]),
        (sharpe_str, f"bold {palette['kpi_default']}"),
        ("  MaxDD: ", palette["dim"]),
        (dd_str, f"bold {palette['kpi_sharpe_bad']}"),
        ("  WinRate: ", palette["dim"]),
        (wr_str, f"bold {palette['kpi_default']}"),
        ("  Vol: ", palette["dim"]),
        (vol_str, f"bold {palette['kpi_default']}"),
        ("  Trades: ", palette["dim"]),
        (trades_str, f"bold {palette['kpi_default']}"),
    )

    spark = sparkline(equity_curve[-32:]) if equity_curve else ""
    line2 = Text(spark or "—", style=palette["dim"])

    return Panel(
        Group(line1, line2),
        title="[bold]KPI[/bold]",
        border_style=palette["border_default"],
        expand=True,
    )


__all__ = [
    "_build_equity_panel",
    "_build_kpi_band",
    "_build_kpi_compact",
]
