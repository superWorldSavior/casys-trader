"""Decision, learning, data-health, and LLM activity Rich builders."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.interfaces.ui.panels.common import _market_badge
from trader.reporting.read_models.runtime_state import (
    _format_datetime,
    _safe_float,
)


def _build_decisions_table(
    decisions: list[dict],
    *,
    open_venues: "set[str] | None" = None,
    palette: Palette = PALETTE_DARK,
) -> Table:
    dec_table = Table(title="Dernières décisions", show_lines=False, expand=True)
    dec_table.add_column("Mkt", no_wrap=True, justify="center")
    dec_table.add_column("Symbole", style="bold")
    dec_table.add_column("Action")
    dec_table.add_column("Qté", justify="right")
    dec_table.add_column("Raison", overflow="fold", ratio=4)
    dec_table.add_column("Confiance", justify="right")
    dec_table.add_column("Source", style=palette["dim"])

    for d in decisions:
        action = str(d.get("action", "HOLD"))
        action_style = (
            palette["action_buy"]
            if action == "BUY"
            else (
                palette["action_sell"] if action == "SELL" else palette["action_hold"]
            )
        )
        symbol = str(d.get("symbol", "?"))
        qty_d = _safe_float(d.get("qty"), default=0.0) or 0.0
        rationale = str(d.get("rationale") or "")
        confidence = _safe_float(d.get("confidence"), default=0.0) or 0.0
        dec_table.add_row(
            _market_badge(symbol, open_venues, palette),
            symbol,
            Text(action, style=action_style),
            f"{qty_d:,.4f}",
            rationale,
            f"{confidence:.2f}",
            str(d.get("data_source") or "—"),
        )

    if not decisions:
        dec_table.add_row("—", "—", "—", "—", "—", "—", "—")

    return dec_table


def _build_learnings_panel(
    learnings: list[dict], *, palette: Palette = PALETTE_DARK
) -> RenderableType | None:
    if not learnings:
        return None
    lines: list[Text] = []
    for index, item in enumerate(learnings[-5:]):
        if index:
            lines.append(Text(""))
        note = str(item.get("note") or "")
        symbol = str(item.get("symbol") or "—")
        ts = _format_datetime(item.get("ts"))
        lines.append(
            Text.assemble(
                ("▸ ", palette["dim"]),
                (symbol, palette["learning_symbol"]),
                ("  ·  ", palette["dim"]),
                (ts, palette["dim"]),
            )
        )
        lines.append(Text.assemble(("  ", palette["dim"]), note))
    return Panel(
        Group(*lines),
        title="[bold]Derniers apprentissages[/bold]",
        border_style=palette["border_learnings"],
        expand=True,
    )


def _build_data_health_panel(
    recent_decisions: list[dict],
    stale_streaks: dict,
    *,
    palette: Palette = PALETTE_DARK,
) -> Panel:
    """Santé data (droite, compact).

    Par symbole avec streaks > 0 ou data_source non-None dans les décisions
    récentes : une ligne {symbol} {data_source} [backoff ×N si streak > 0].
    """
    # Collecter data_source par symbole depuis les décisions récentes (dernier vu)
    ds_by_symbol: dict[str, str | None] = {}
    for dec in recent_decisions:
        sym = str(dec.get("symbol") or "")
        if not sym:
            continue
        runtime = dec.get("runtime") if isinstance(dec.get("runtime"), dict) else {}
        ds = runtime.get("data_source") if isinstance(runtime, dict) else None
        ds_by_symbol[sym] = str(ds) if ds is not None else None

    # Tous les symboles concernés = union(streaks > 0, ds non-None)
    symbols_concerned: set[str] = set()
    for sym, streak in (stale_streaks or {}).items():
        try:
            if int(streak) > 0:
                symbols_concerned.add(sym)
        except (TypeError, ValueError):
            pass
    for sym, ds in ds_by_symbol.items():
        if ds is not None:
            symbols_concerned.add(sym)

    if not symbols_concerned:
        return Panel(
            Text("—", style=palette["dim"]),
            title="[bold]Santé data[/bold]",
            border_style=palette["border_default"],
            expand=True,
        )

    table = Table(box=None, show_header=False, expand=True, pad_edge=False)
    table.add_column("sym", style="bold", no_wrap=True)
    table.add_column("source", no_wrap=True)
    table.add_column("streak", justify="right", no_wrap=True)

    for sym in sorted(symbols_concerned):
        ds = ds_by_symbol.get(sym)
        ds_str = str(ds) if ds is not None else "—"
        streak = stale_streaks.get(sym, 0)
        try:
            streak_int = int(streak)
        except (TypeError, ValueError):
            streak_int = 0
        if streak_int > 0:
            streak_cell = Text(f"backoff ×{streak_int}", style=palette["kpi_vol_warn"])
        else:
            streak_cell = Text("ok", style=palette["pnl_positive"])
        table.add_row(sym, ds_str, streak_cell)

    return Panel(
        table,
        title="[bold]Santé data[/bold]",
        border_style=palette["border_default"],
        expand=True,
    )


def _build_llm_activity_panel(
    daemon_status: dict,
    learnings_pending: int,
    consolidation_status: dict | None,
    *,
    palette: Palette = PALETTE_DARK,
) -> Panel:
    """Activité LLM (droite, compact).

    Affiche : appels modèle du cycle, learnings bruts en attente,
    dernier état de consolidation.
    """
    used = daemon_status.get("model_calls_used")
    max_calls = daemon_status.get("max_model_calls_per_cycle")
    calls_str = (
        f"{used}/{max_calls}"
        if used is not None and max_calls is not None
        else (str(used) if used is not None else "—")
    )

    # Consolidation status
    if consolidation_status is not None:
        consol_status = str(consolidation_status.get("status") or "?")
        consol_ts = _format_datetime(consolidation_status.get("ts"))
        consol_count = consolidation_status.get("count")
        consol_str = f"{consol_status}"
        if consol_count is not None:
            consol_str += f" ({consol_count} entrées)"
        consol_style = (
            palette["pnl_positive"]
            if "success" in consol_status.lower()
            else (
                palette["pnl_negative"]
                if "fail" in consol_status.lower() or "error" in consol_status.lower()
                else palette["dim"]
            )
        )
    else:
        consol_str = "—"
        consol_ts = "—"
        consol_style = palette["dim"]

    content = Text.assemble(
        ("Appels cycle: ", palette["dim"]),
        (calls_str, f"bold {palette['kpi_default']}"),
        ("  Learnings bruts: ", palette["dim"]),
        (str(learnings_pending), f"bold {palette['kpi_default']}"),
        "\n",
        ("Consolidation: ", palette["dim"]),
        (consol_str, consol_style),
        ("  ", ""),
        (consol_ts, palette["dim"]),
    )

    return Panel(
        content,
        title="[bold]LLM[/bold]",
        border_style=palette["border_llm_activity"],
        expand=True,
    )


__all__ = [
    "_build_data_health_panel",
    "_build_decisions_table",
    "_build_learnings_panel",
    "_build_llm_activity_panel",
]
