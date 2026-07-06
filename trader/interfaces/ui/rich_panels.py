"""Pure Rich render builders for casys-trader runtime state."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trader.market import fx
from trader.planning.indicator_watch import is_armed_plan as _is_armed_plan
from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.reporting.read_models.runtime_state import (
    UTC,
    _format_datetime,
    _safe_float,
    _safe_list_of_dicts,
)

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# Construction de la vue (PURE — ne lit aucun fichier)
# ---------------------------------------------------------------------------


def sparkline(values: list[float]) -> str:
    """Mini-courbe unicode à 8 niveaux. Retourne "" si aucune valeur valide."""
    clean = [_safe_float(value, default=None) for value in values]
    clean_values = [value for value in clean if value is not None]
    if not clean_values:
        return ""

    low = min(clean_values)
    high = max(clean_values)
    if high == low:
        return "▄" * len(clean_values)

    span = high - low
    last_index = len(_SPARK_BLOCKS) - 1
    blocks: list[str] = []
    for value in clean_values:
        index = int(round((value - low) / span * last_index))
        index = max(0, min(last_index, index))
        blocks.append(_SPARK_BLOCKS[index])
    return "".join(blocks)


def _fmt_money(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:,.2f}"


def _fmt_signed_money(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:+,.2f}"


def _fmt_fee_cost(value: Any, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{-abs(number):+,.2f}"


def _holding_unrealized_pnl_for_display(holding: dict) -> float:
    net_pnl = _safe_float(holding.get("unrealized_pnl_net"), default=None)
    if net_pnl is not None:
        return net_pnl
    return _safe_float(holding.get("unrealized_pnl"), default=0.0) or 0.0


def _holding_round_trip_fee_for_display(holding: dict) -> float | None:
    if _safe_float(holding.get("unrealized_pnl_net"), default=None) is None:
        return None
    return _safe_float(holding.get("round_trip_fee"), default=None)


def _holding_notional_usd(holding: dict) -> float:
    qty = _safe_float(holding.get("quantity"), default=0.0) or 0.0
    price = _safe_float(holding.get("last_price"), default=None)
    if price is None:
        price = _safe_float(holding.get("current_price"), default=None)
    if price is None:
        price = _safe_float(holding.get("avg_price"), default=0.0) or 0.0
    fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0
    return abs(qty) * price * fx_rate


def _fmt_number(value: Any, decimals: int = 2, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number:.{decimals}f}"


def _fmt_percent(value: Any, decimals: int = 1, *, default: str = "n/a") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{number * 100.0:.{decimals}f}%"


def _fmt_int(value: Any, *, default: str = "0") -> str:
    number = _safe_float(value, default=None)
    return default if number is None else f"{int(number)}"


def _style_for_pnl(value: Any) -> str:
    number = _safe_float(value, default=0.0) or 0.0
    return "green" if number >= 0 else "red"


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


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


def _market_badge(
    symbol: str, open_venues: "set[str] | None", palette: Palette
) -> Text:
    """Badge marché partagé : ● ouvert / ○ fermé / · inconnu (open_venues absent).

    ``open_venues`` = codes venue ouverts (EU/US/TW/FX). None → badge neutre,
    pour distinguer « fermé » d'« information de session indisponible ».
    """
    from trader.market.rotation.wiring import venue_of

    if open_venues is None:
        return Text("·", style=palette["dim"])
    if venue_of(symbol) in open_venues:
        return Text("●", style=palette["status_nominal"])
    return Text("○", style=palette["dim"])


def _build_positions_panel(
    holdings: list[dict],
    *,
    trade_plans: list[dict] | None = None,
    open_venues: "set[str] | None" = None,
    palette: Palette = PALETTE_DARK,
) -> Panel:
    """Table Positions débruitée : marché ouvert/fermé + stop en un coup d'œil.

    Colonnes bruyantes retirées (taux FX, prix natifs — dispo dans le drill-down
    et les plans de sortie). ``open_venues`` = codes venue ouverts (EU/US/TW/FX) ;
    ``trade_plans`` alimente la colonne Stop. Les deux sont optionnels : sans eux
    la table reste valide (badge neutre « · », stop « — »).
    """

    plans_by_symbol: dict[str, dict] = {}
    for plan in _safe_list_of_dicts(trade_plans or []):
        sym = str(plan.get("symbol") or "")
        if sym and sym not in plans_by_symbol:
            plans_by_symbol[sym] = plan

    pos_table = Table(show_lines=False, expand=True)
    pos_table.add_column("Mkt", no_wrap=True, justify="center")
    pos_table.add_column("Symbole", style="bold")
    pos_table.add_column("Dev.", no_wrap=True)
    pos_table.add_column("Valeur USD", justify="right")
    pos_table.add_column("PnL latent USD", justify="right")
    pos_table.add_column("PnL %", justify="right")
    pos_table.add_column("Stop", justify="right", no_wrap=True)

    for h in holdings:
        symbol = str(h.get("symbol", "?"))
        ccy = fx.currency_for(symbol)
        qty = _safe_float(h.get("quantity"), default=0.0) or 0.0
        avg = _safe_float(h.get("avg_price"), default=0.0) or 0.0
        last = _safe_float(h.get("last_price"), default=0.0) or 0.0
        gross_pnl = _safe_float(h.get("unrealized_pnl"), default=0.0) or 0.0
        net_pnl = _safe_float(h.get("unrealized_pnl_net"), default=None)
        round_trip_fee = _safe_float(h.get("round_trip_fee"), default=None)
        pnl = net_pnl if net_pnl is not None else gross_pnl
        fx_rate = _safe_float(h.get("fx_rate"), default=1.0) or 1.0
        notional = abs(avg * qty * fx_rate)
        pnl_pct = (pnl / notional * 100.0) if notional else 0.0
        pnl_style = palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]
        pnl_text = Text(f"{pnl:+,.2f}", style=pnl_style)
        if net_pnl is not None and round_trip_fee is not None:
            pnl_text.append("\n")
            pnl_text.append(
                f"brut {gross_pnl:+,.2f} · frais {_fmt_fee_cost(round_trip_fee)}",
                style=palette["dim"],
            )

        mkt_cell = _market_badge(symbol, open_venues, palette)

        plan = plans_by_symbol.get(symbol)

        # Valeur de marché en USD (base USD) — plus parlant que la qté native.
        # Repli sur le prix moyen si le dernier prix manque.
        ref_price = last if last > 0 else avg
        value_usd = abs(qty * ref_price * fx_rate)
        value_cell = Text(f"{value_usd:,.0f}")
        if plan is not None:
            planned = _safe_float(plan.get("quantity"), default=None)
            remaining = _safe_float(plan.get("remaining_quantity"), default=None)
            filled_tps = plan.get("filled_take_profits") or []
            reduced = (
                planned is not None
                and remaining is not None
                and planned > 0
                and remaining < planned - 1e-9
            )
            if reduced or filled_tps:
                frac = (1.0 - remaining / planned) if reduced else 0.0
                label = f"◑ soldé {frac:.0%}" if frac > 0 else "◑ solde en cours"
                if filled_tps:
                    label += " (" + "·".join(str(t) for t in filled_tps) + ")"
                value_cell.append("\n")
                value_cell.append(label, style=palette["status_accent"])

        # Stop du plan de sortie + distance signée vs dernier prix natif
        stop = _safe_float(plan.get("hard_stop_price"), default=None) if plan else None
        if stop is not None and last > 0:
            dist_pct = (stop - last) / last * 100.0
            stop_cell = Text(
                f"{stop:,.2f} ({dist_pct:+.1f}%)", style=palette["pnl_negative"]
            )
        elif stop is not None:
            stop_cell = Text(f"{stop:,.2f}", style=palette["pnl_negative"])
        else:
            stop_cell = Text("—", style=palette["dim"])

        pos_table.add_row(
            mkt_cell,
            symbol,
            ccy,
            value_cell,
            pnl_text,
            Text(f"{pnl_pct:+.2f}%", style=pnl_style),
            stop_cell,
        )

    if not holdings:
        pos_table.add_row("—", "—", "—", "—", "—", "—", "—")

    return Panel(
        pos_table,
        title="[bold]Positions[/bold]",
        border_style=palette["border_default"],
        expand=True,
    )


def _confidence_bar(
    win_rate: Any, pnl: Any, *, width: int = 12, palette: Palette = PALETTE_DARK
) -> Text:
    win = _safe_float(win_rate, default=0.0) or 0.0
    win = max(0.0, min(1.0, win))
    filled = int(round(win * width))
    bar = "█" * filled + "░" * (width - filled)
    pnl_val = _safe_float(pnl, default=0.0) or 0.0
    return Text(
        bar, style=palette["pnl_positive"] if pnl_val >= 0 else palette["pnl_negative"]
    )


def _build_attribution_panel(
    attribution: dict, *, palette: Palette = PALETTE_DARK
) -> Panel:
    realized_pnl = _safe_float(attribution.get("realized_pnl"), default=0.0) or 0.0
    total_commissions = _safe_float(attribution.get("total_commissions"), default=None)
    realized_gross_pnl = _safe_float(attribution.get("realized_gross_pnl"), default=None)
    pnl_style = (
        palette["pnl_positive"] if realized_pnl >= 0 else palette["pnl_negative"]
    )
    realized_detail_parts: list[str] = []
    if total_commissions is not None:
        realized_detail_parts.append(f"dont frais {_fmt_fee_cost(total_commissions)}")
    if realized_gross_pnl is not None:
        realized_detail_parts.append(f"brut {_fmt_signed_money(realized_gross_pnl)}")
    realized_detail = (
        f" ({' · '.join(realized_detail_parts)})"
        if realized_detail_parts
        else ""
    )
    summary = Text.assemble(
        ("Trades clôturés : ", "bold"),
        (_fmt_int(attribution.get("n_closed_trades")), palette["kpi_default"]),
        ("   P&L réalisé USD : ", "bold"),
        (_fmt_signed_money(realized_pnl), pnl_style),
        (realized_detail, palette["dim"]),
        ("   Win rate : ", "bold"),
        (_fmt_percent(attribution.get("win_rate")), palette["kpi_default"]),
        ("   Détention moy. : ", "bold"),
        (
            _fmt_number(attribution.get("avg_holding_minutes"), 1),
            palette["kpi_default"],
        ),
        (" min", palette["dim"]),
    )

    confidence_rows = _safe_list_of_dicts(attribution.get("by_confidence"))
    confidence_table = Table.grid(expand=True)
    confidence_table.add_column(ratio=2)
    confidence_table.add_column(ratio=2)
    confidence_table.add_column(ratio=3, justify="right")
    if confidence_rows:
        for row in confidence_rows:
            pnl = _safe_float(row.get("total_pnl"), default=0.0) or 0.0
            row_pnl_style = (
                palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]
            )
            confidence_table.add_row(
                Text(str(row.get("bucket", "—")), style="bold"),
                _confidence_bar(row.get("win_rate"), pnl, palette=palette),
                Text(
                    f"n={_fmt_int(row.get('n'))}  win={_fmt_percent(row.get('win_rate'))}  P&L USD {_fmt_signed_money(pnl)}",
                    style=row_pnl_style,
                ),
            )
    else:
        confidence_table.add_row(Text("—", style=palette["dim"]), Text(""), Text(""))

    exit_rows = _safe_list_of_dicts(attribution.get("by_exit_reason"))
    exit_table = Table(
        show_header=True,
        header_style=f"bold {palette['dim']}",
        box=None,
        expand=True,
        pad_edge=False,
    )
    exit_table.add_column("Raison")
    exit_table.add_column("n", justify="right")
    exit_table.add_column("Win", justify="right")
    exit_table.add_column("P&L USD", justify="right")
    if exit_rows:
        for row in exit_rows:
            pnl = _safe_float(row.get("total_pnl"), default=0.0) or 0.0
            exit_table.add_row(
                str(row.get("reason", "—")),
                _fmt_int(row.get("n")),
                _fmt_percent(row.get("win_rate")),
                Text(
                    _fmt_signed_money(pnl),
                    style=palette["pnl_positive"]
                    if pnl >= 0
                    else palette["pnl_negative"],
                ),
            )
    else:
        exit_table.add_row("—", "—", "—", "—")

    return Panel(
        Group(
            summary,
            Text("Calibration confiance", style="bold"),
            confidence_table,
            Text("Raisons de sortie", style="bold"),
            exit_table,
        ),
        title="[bold]Attribution USD[/bold]",
        border_style=palette["border_attribution"],
        expand=True,
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


def _build_exit_plans_panel(
    plans: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Plans de sortie ouverts (colonne gauche, sous positions).

    Source : state/trade_plans.json — liste brute de dicts.
    """
    if not plans:
        return Panel(
            Text("aucun plan ouvert", style=palette["dim"]),
            title="[bold]Plans sortie[/bold]",
            border_style=palette["border_plans"],
            expand=True,
        )

    lines: list[RenderableType] = []
    for plan in plans:
        symbol = str(plan.get("symbol", "?"))
        ccy = fx.currency_for(symbol)
        side = str(plan.get("side", "?"))
        entry = _safe_float(plan.get("entry_price"), default=None)
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        remaining = _safe_float(plan.get("remaining_quantity"), default=0.0) or 0.0
        max_hold = _safe_float(plan.get("max_hold_minutes"), default=None)
        take_profits = _safe_list_of_dicts(plan.get("take_profits") or [])

        side_style = (
            palette["action_buy"] if side == "LONG" else palette["action_sell"]
        )

        # Ligne principale : symbole side  entrée → stop (dist%)
        if entry is not None and stop is not None and entry > 0:
            dist_pct = abs(entry - stop) / entry * 100.0
            stop_str = f"{stop:,.2f} ({dist_pct:.1f}%)"
        elif stop is not None:
            stop_str = f"{stop:,.2f}"
        else:
            stop_str = "—"

        entry_str = f"{entry:,.2f}" if entry is not None else "—"

        header = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (side, side_style),
            (f"  prix natif {ccy} entrée:", palette["dim"]),
            (f" {entry_str}", "bold"),
            ("  stop:", palette["dim"]),
            (f" {stop_str}", palette["pnl_negative"] if stop else palette["dim"]),
            ("  qté:", palette["dim"]),
            (f" {remaining:,.4f}", "bold"),
        )
        lines.append(header)

        # Take-profits sur une ligne compacte
        if take_profits:
            tp_parts: list[tuple[str, str]] = []
            for tp in take_profits:
                tp_price = _safe_float(tp.get("price"), default=None)
                tp_name = str(tp.get("name") or "tp?")
                if tp_price is not None:
                    tp_parts.append((f"{tp_name}@{tp_price:,.2f}", palette["pnl_positive"]))
                    tp_parts.append(("  ", ""))
            if tp_parts:
                tp_line = Text.assemble((f"  TPs natifs {ccy}: ", palette["dim"]), *tp_parts)
                lines.append(tp_line)

        # max_hold
        if max_hold is not None:
            lines.append(
                Text.assemble(
                    ("  max hold:", palette["dim"]),
                    (f" {int(max_hold)}min", "bold"),
                )
            )

        lines.append(Text(""))  # séparateur

    # Retire le dernier séparateur vide
    if lines and isinstance(lines[-1], Text) and lines[-1].plain == "":
        lines.pop()

    return Panel(
        Group(*lines),
        title="[bold]Plans sortie[/bold]",
        border_style=palette["border_plans"],
        expand=True,
    )


def _expire_relative(expires_raw: str, *, now: datetime) -> str:
    try:
        candidate = f"{expires_raw[:-1]}+00:00" if expires_raw.endswith("Z") else expires_raw
        exp_dt = datetime.fromisoformat(candidate)
        if exp_dt.tzinfo is None:
            from datetime import timezone as _tz

            exp_dt = exp_dt.replace(tzinfo=_tz.utc)
        total_secs = int((exp_dt - now).total_seconds())
        if total_secs < 0:
            return "expiré"
        hours, rem = divmod(total_secs, 3600)
        minutes = rem // 60
        return f"dans {hours}h{minutes:02d}" if hours > 0 else f"dans {minutes}min"
    except Exception:
        return "?"


def _build_armed_plans_panel(
    watches: list[dict],
    *,
    open_venues: "set[str] | None" = None,
    palette: Palette = PALETTE_DARK,
) -> Panel:
    """Plans armés (colonne gauche) : scénarios d'entrée que le daemon exécutera
    au déclenchement sans appel LLM. Distincts des positions (rien n'est ouvert)
    et des veilles simples (qui ne portent pas d'ordre).
    """
    armed = [w for w in watches if _is_armed_plan(w)]
    if not armed:
        return Panel(
            Text("aucun plan armé", style=palette["dim"]),
            title="[bold]Plans armés[/bold]",
            border_style=palette["border_watches"],
            expand=True,
        )

    now_utc = datetime.now(UTC)
    lines: list[Text] = []
    for watch in armed:
        symbol = str(watch.get("symbol", "?"))
        order = watch.get("order") or {}
        intent = str(order.get("intent") or "?")
        sens = "▲LONG" if intent == "OPEN_LONG" else "▼SHORT"
        sens_style = palette["pnl_positive"] if intent == "OPEN_LONG" else palette["pnl_negative"]
        qty = order.get("qty")
        stop = None
        exit_plan = order.get("exit_plan")
        if isinstance(exit_plan, dict):
            hard_stop = exit_plan.get("hard_stop")
            stop = hard_stop.get("price") if isinstance(hard_stop, dict) else hard_stop
        conditions = _safe_list_of_dicts(watch.get("conditions") or [])
        cond_parts = [
            f"{c.get('indicator', '?')}{c.get('op', '?')}{c.get('value', '?')}"
            f"@{c.get('timeframe') or c.get('interval') or '?'}"
            for c in conditions[:2]
        ]
        cond_str = f" [{watch.get('logic', 'all')}] ".join(cond_parts) if cond_parts else "?"
        if len(conditions) > 2:
            cond_str += f" +{len(conditions) - 2}"
        confidence = order.get("confidence")
        conf_str = f"  c.{confidence:.2f}".rstrip("0").rstrip(".") if isinstance(confidence, (int, float)) else ""
        # Conteneur neutre : le style du badge reste local (span) et ne
        # contamine pas le style de base de toute la ligne (cf. review Codex).
        line = Text()
        line.append_text(_market_badge(symbol, open_venues, palette))
        line.append(" ")
        line.append_text(Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (sens, f"bold {sens_style}"),
            (f" {qty:g}" if isinstance(qty, (int, float)) else " ?", ""),
            (f"  stop {stop}" if stop is not None else "  stop ?", palette["kpi_default"]),
            ("  si ", palette["dim"]),
            (cond_str, palette["dim"]),
            ("  ", ""),
            (_expire_relative(str(watch.get("expires_at") or ""), now=now_utc), palette["dim"]),
            (conf_str, palette["dim"]),
        ))
        lines.append(line)
    return Panel(
        Group(*lines),
        title=f"[bold]Plans armés[/bold] ({len(armed)})",
        border_style=palette["border_watches"],
        expand=True,
    )


def _build_watches_panel(
    watches: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Veilles actives (colonne droite, sous logs).

    Source : _load_indicator_watches_safe(state/scheduler.json).
    Les plans armés (EXECUTE_ORDER + order) ont leur propre panneau : exclus ici.
    """
    watches = [w for w in watches if not _is_armed_plan(w)]
    if not watches:
        return Panel(
            Text("aucune veille active", style=palette["dim"]),
            title="[bold]Veilles[/bold]",
            border_style=palette["border_watches"],
            expand=True,
        )

    now_utc = datetime.now(UTC)
    watch_lines: list[Text] = []
    for watch in watches:
        symbol = str(watch.get("symbol", "?"))
        expires_raw = str(watch.get("expires_at") or "")
        logic = str(watch.get("logic", "any"))
        conditions = _safe_list_of_dicts(watch.get("conditions") or [])

        expire_str = _expire_relative(expires_raw, now=now_utc)

        # Conditions compactes
        cond_parts: list[str] = []
        for cond in conditions[:3]:  # max 3 conditions affichées
            ind = str(cond.get("indicator") or "?")
            op = str(cond.get("op") or "?")
            val = cond.get("value")
            tf = str(cond.get("timeframe") or cond.get("interval") or "?")
            val_str = f"{val}" if val is not None else "?"
            cond_parts.append(f"{ind}{op}{val_str}@{tf}")
        cond_str = f" [{logic}] ".join(cond_parts) if cond_parts else "?"

        line = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (cond_str, palette["dim"]),
            ("  ", ""),
            (expire_str, palette["kpi_vol_warn"]),
        )
        watch_lines.append(line)

    return Panel(
        Group(*watch_lines),
        title="[bold]Veilles[/bold]",
        border_style=palette["border_watches"],
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


_ACTION_VENUES = ("TW", "EU", "US")
_MAX_HOTLIST_DISPLAY = 12


def build_selection_panel(
    venue_state: dict,
    open_venues_list: list[str],
    *,
    palette: Palette = PALETTE_DARK,
) -> "RenderableType":
    """Panneau « Sélection par marché » (PURE — ne lit aucun fichier).

    Pour chaque venue d'actions TW/EU/US :
    - badge OUVERT / fermé
    - hotlist triée par attractivité (scores) décroissante, tronquée à 12
    Tolère un état vide ou venue absente.
    """
    venues = venue_state.get("venues") if isinstance(venue_state.get("venues"), dict) else {}
    blocks: list["RenderableType"] = []

    for venue in _ACTION_VENUES:
        is_open = venue in open_venues_list
        badge_style = palette["pnl_positive"] if is_open else palette["dim"]
        badge_text = "OUVERT" if is_open else "fermé"

        venue_data = venues.get(venue) if isinstance(venues.get(venue), dict) else {}
        hotlist: list[str] = venue_data.get("hotlist") if isinstance(venue_data.get("hotlist"), list) else []  # type: ignore[assignment]
        scores: dict[str, float] = venue_data.get("scores") if isinstance(venue_data.get("scores"), dict) else {}  # type: ignore[assignment]

        # Tri par attractivité décroissante
        def _attractivity(sym: str) -> float:
            v = scores.get(sym)
            try:
                return -float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0.0

        sorted_hotlist = sorted(hotlist, key=_attractivity)[: _MAX_HOTLIST_DISPLAY]

        table = Table(box=None, show_header=True, expand=True, pad_edge=False)
        table.add_column("Symbole", style="bold", no_wrap=True)
        table.add_column("Attractivité", justify="right", no_wrap=True)

        if sorted_hotlist:
            for sym in sorted_hotlist:
                score_val = scores.get(sym)
                try:
                    score_str = f"{float(score_val):.4f}"  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    score_str = "—"
                table.add_row(sym, score_str)
        else:
            table.add_row("—", "—")

        title_text = Text.assemble(
            (venue, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (f"[{badge_text}]", badge_style),
        )
        blocks.append(Panel(table, title=title_text, border_style=palette["border_default"], expand=True))

    return Group(*blocks)


def build_trades_table(
    fills: list[dict],
    *,
    limit: int = 20,
    palette: Palette = PALETTE_DARK,
) -> Table:
    """Tableau « Derniers trades » (PURE — ne lit aucun fichier).

    Colonnes : heure, symbole, sens (BUY/SELL depuis Fill.side), quantité,
    prix, commission. Les ``limit`` DERNIERS fills sont affichés.

    Champs réels de Fill : symbol, side (Side="BUY"|"SELL"), quantity, price,
    ts, commission, commission_currency, commission_model.
    """
    table = Table(title="Derniers trades", show_lines=False, expand=True)
    table.add_column("Heure", no_wrap=True, style=palette["dim"])
    table.add_column("Symbole", style="bold")
    table.add_column("Dev.", no_wrap=True)
    table.add_column("Sens")
    table.add_column("Qté", justify="right")
    table.add_column("Prix natif", justify="right")
    table.add_column("Commission", justify="right")

    recent = fills[-limit:] if len(fills) > limit else fills
    for fill in reversed(recent):
        ts_raw = str(fill.get("ts") or "")
        # Affiche HH:MM:SS si possible, sinon la chaîne brute tronquée
        try:
            candidate = f"{ts_raw[:-1]}+00:00" if ts_raw.endswith("Z") else ts_raw
            dt = datetime.fromisoformat(candidate)
            ts_str = dt.astimezone(UTC).strftime("%H:%M:%S")
        except (ValueError, AttributeError):
            ts_str = ts_raw[:8] if ts_raw else "—"

        symbol = str(fill.get("symbol") or "?")
        ccy = fx.currency_for(symbol)
        side = str(fill.get("side") or "")
        side_style = (
            palette["action_buy"] if side == "BUY" else (
                palette["action_sell"] if side == "SELL" else palette["dim"]
            )
        )
        qty_val = _safe_float(fill.get("quantity"), default=0.0) or 0.0
        price_val = _safe_float(fill.get("price"), default=None)
        price_str = f"{price_val:,.4f}" if price_val is not None else "—"
        commission_val = _safe_float(fill.get("commission"), default=None)
        if commission_val is not None:
            comm_currency = str(fill.get("commission_currency") or "USD")
            comm_str = f"{_fmt_fee_cost(commission_val)} {comm_currency}"
        else:
            comm_str = "—"

        table.add_row(
            ts_str,
            symbol,
            ccy,
            Text(side, style=side_style),
            f"{qty_val:,.4f}",
            price_str,
            comm_str,
        )

    if not recent:
        table.add_row("—", "—", "—", "—", "—", "—", "—")

    return table


# ---------------------------------------------------------------------------
# Fonctions pures — univers, PnL réalisé, panneau univers, KPI compact,
# plans de sortie enrichis
# ---------------------------------------------------------------------------


def _fmt_symbol_short(ticker: str, company_map: dict[str, str]) -> str:
    """Formate un ticker sous la forme "Nom · TICKER" tronquée à 20 chars max.

    - Si le nom est absent de company_map → retourne le ticker brut.
    - Si le nom dépasse 15 chars → utilise uniquement le premier mot.
    - Le résultat final est tronqué à 20 chars.
    """
    name = company_map.get(ticker)
    if not name:
        return ticker

    # Premier mot si nom long
    display_name = name if len(name) <= 15 else name.split()[0]
    label = f"{display_name} · {ticker}"
    return label[:20]


def _fmt_time_hms(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(candidate)
        return dt.astimezone(UTC).strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return text[:8] if text else "—"


def _fmt_price_4(value: Any) -> str:
    number = _safe_float(value, default=None)
    return "—" if number is None else f"{number:,.4f}"


def _fmt_price_pair(entry: Any, exit_: Any) -> str:
    entry_str = _fmt_price_4(entry)
    exit_str = _fmt_price_4(exit_)
    if entry_str == "—" and exit_str == "—":
        return "—"
    return f"{entry_str}→{exit_str}"


def _fmt_holding_duration(minutes: Any) -> str:
    value = _safe_float(minutes, default=None)
    if value is None or value < 0:
        return "—"
    total = int(round(value))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}h{mins:02d}"
    return f"{mins}m"


def build_closed_trades_table(
    trips: list[dict],
    company_map: dict[str, str],
    *,
    limit: int = 20,
    palette: Palette = PALETTE_DARK,
) -> Table:
    """Tableau « Sorties / Trades clôturés » depuis les round-trips d'attribution.

    PURE — ne lit aucun fichier. Tolère les trips incomplets ou corrompus.
    """
    table = Table(title="Sorties / Trades clôturés", show_lines=False, expand=True)
    table.add_column("Heure", no_wrap=True, style=palette["dim"])
    table.add_column("Nom·Ticker", style="bold")
    table.add_column("Dev.", no_wrap=True)
    table.add_column("Sens")
    table.add_column("Entrée→Sortie natif", justify="right")
    table.add_column("Net USD", justify="right")
    table.add_column("Raison")
    table.add_column("Durée", justify="right")

    clean_limit = max(0, int(limit))
    recent = trips[:clean_limit] if clean_limit else []

    for trip in recent:
        if not isinstance(trip, dict):
            continue
        symbol = str(trip.get("symbol") or "?")
        ccy = fx.currency_for(symbol)
        side = str(trip.get("side") or "—")
        side_style = (
            palette["action_buy"] if side == "LONG" else (
                palette["action_sell"] if side == "SHORT" else palette["dim"]
            )
        )

        pnl_value = _safe_float(trip.get("pnl"), default=None)
        if pnl_value is None:
            pnl_cell = Text("—", style=palette["dim"])
        else:
            pnl_style = palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"]
            pnl_cell = Text(_fmt_signed_money(pnl_value, default="—"), style=pnl_style)
            gross_pnl = _safe_float(trip.get("gross_pnl"), default=None)
            commission = _safe_float(trip.get("commission"), default=None)
            detail_parts: list[str] = []
            if gross_pnl is not None:
                detail_parts.append(f"brut {_fmt_signed_money(gross_pnl)}")
            if commission is not None:
                detail_parts.append(f"frais {_fmt_fee_cost(commission)}")
            if detail_parts:
                pnl_cell.append("\n")
                pnl_cell.append(" · ".join(detail_parts), style=palette["dim"])

        reason = str(trip.get("exit_reason") or "—")
        table.add_row(
            _fmt_time_hms(trip.get("exit_ts")),
            _fmt_symbol_short(symbol, company_map),
            ccy,
            Text(side, style=side_style),
            _fmt_price_pair(trip.get("entry_price"), trip.get("exit_price")),
            pnl_cell,
            _truncate(reason, 36),
            _fmt_holding_duration(trip.get("holding_minutes")),
        )

    if not table.rows:
        table.add_row("—", "—", "—", "—", "—", "—", "—", "—")

    return table


def compute_realized_pnl_by_fill(fills: list[dict]) -> list[float | None]:
    """Calcule le PnL réalisé net par fill selon la méthode FIFO coût moyen.

    Algo PURE et déterministe :
    - BUY  → nouveau coût moyen pondéré (commission intégrée dans la base)
              retourne None à la position correspondante.
    - SELL → net_realized = (sell_price - avg_cost) * sell_qty - sell_commission
              retourne la valeur calculée.
    - Vente sans achat préalable → retourne None (pas de crash).

    La liste retournée a exactement la même longueur que `fills`.
    """
    avg_cost: dict[str, float] = {}
    qty: dict[str, float] = {}
    result: list[float | None] = []

    for fill in fills:
        symbol = str(fill.get("symbol") or "")
        side = str(fill.get("side") or "")
        fill_qty = _safe_float(fill.get("quantity"), default=0.0) or 0.0
        fill_price = _safe_float(fill.get("price"), default=0.0) or 0.0
        commission = _safe_float(fill.get("commission"), default=0.0) or 0.0

        if side == "BUY":
            old_qty = qty.get(symbol, 0.0)
            old_avg = avg_cost.get(symbol, 0.0)
            total_qty = old_qty + fill_qty
            if total_qty > 0:
                # Commission intégrée dans le coût de base
                new_avg = (old_qty * old_avg + fill_qty * fill_price + commission) / total_qty
            else:
                new_avg = fill_price
            avg_cost[symbol] = new_avg
            qty[symbol] = total_qty
            result.append(None)

        elif side == "SELL":
            cost = avg_cost.get(symbol)
            if cost is None:
                # Vente sans achat préalable connu → pas de crash
                result.append(None)
            else:
                net = (fill_price - cost) * fill_qty - commission
                # Décrémenter la quantité (coût moyen inchangé)
                qty[symbol] = max(0.0, qty.get(symbol, 0.0) - fill_qty)
                result.append(net)

        else:
            # Side inconnu → None
            result.append(None)

    return result


def build_universe_panel(
    universe_symbols: list[str],
    venue_state: dict,
    open_venues_list: list[str],
    company_map: dict[str, str],
    *,
    palette: Palette = PALETTE_DARK,
) -> RenderableType:
    """Panneau « Univers » regroupé par venue EU/TW/US (PURE — ne lit aucun fichier).

    Pour chaque venue non vide :
    - Titre coloré vif si ouvert, atténué si fermé.
    - Chaque symbole affiché avec son score si disponible.
    - Score coloré selon seuils : >= 0.7 positif, >= 0.4 neutre, < 0.4 atténué.

    FX ignoré — pas dans l'univers actif.
    Tolère les états vides.
    """
    from trader.market.rotation.wiring import venue_of  # import local pour éviter les cycles

    venues_data = venue_state.get("venues") if isinstance(venue_state.get("venues"), dict) else {}

    # Regrouper les symboles par venue
    by_venue: dict[str, list[str]] = {}
    for sym in universe_symbols:
        try:
            v = venue_of(sym)
        except Exception:
            v = "UNKNOWN"
        if v not in _ACTION_VENUES:
            # FX et venues inconnues ignorées
            continue
        by_venue.setdefault(v, []).append(sym)

    blocks: list[RenderableType] = []
    for venue in _ACTION_VENUES:
        syms = by_venue.get(venue)
        if not syms:
            continue

        is_open = venue in open_venues_list
        venue_style = palette["kpi_default"] if is_open else palette["dim"]
        badge = "OUVERT" if is_open else "fermé"

        # Scores disponibles dans venue_state
        venue_data = venues_data.get(venue) if isinstance(venues_data.get(venue), dict) else {}
        scores: dict = venue_data.get("scores") if isinstance(venue_data.get("scores"), dict) else {}  # type: ignore[assignment]

        table = Table(box=None, show_header=False, expand=True, pad_edge=False)
        table.add_column("sym", no_wrap=True)
        table.add_column("score", justify="right", no_wrap=True)

        for sym in syms:
            label = _fmt_symbol_short(sym, company_map)
            score_val = scores.get(sym)
            try:
                score_f = float(score_val)  # type: ignore[arg-type]
                if math.isnan(score_f):
                    raise ValueError("score NaN")
                score_str = f"{score_f:.4f}"
                if score_f >= 0.7:
                    score_style = palette["pnl_positive"]
                elif score_f >= 0.4:
                    score_style = palette["kpi_default"]
                else:
                    score_style = palette["dim"]
            except (TypeError, ValueError):
                score_str = "—"
                score_style = palette["dim"]

            table.add_row(
                Text(label, style="bold"),
                Text(score_str, style=score_style),
            )

        title_text = Text.assemble(
            (venue, f"bold {venue_style}"),
            ("  ", ""),
            (f"[{badge}]", venue_style),
        )
        blocks.append(
            Panel(table, title=title_text, border_style=palette["border_default"], expand=True)
        )

    if not blocks:
        return Panel(
            Text("univers vide", style=palette["dim"]),
            title="[bold]Univers[/bold]",
            border_style=palette["border_default"],
            expand=True,
        )

    return Group(*blocks)


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


def _build_exit_plans_enriched(
    plans: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Plans de sortie enrichis : entrée, stop, TPs et bénéfice estimé par scénario.

    Enrichit l'affichage de _build_exit_plans_panel sans le modifier.
    Champs lus : entry_price, hard_stop_price, take_profits[].price,
                 remaining_quantity, max_hold_minutes, llm_confidence.
    Bénéfice estimé : (prix_scénario - entrée) × quantité_restante × sens.
    """
    if not plans:
        return Panel(
            Text("aucun plan ouvert", style=palette["dim"]),
            title="[bold]Plans sortie enrichis[/bold]",
            border_style=palette["border_plans"],
            expand=True,
        )

    lines: list[RenderableType] = []
    for plan in plans:
        symbol = str(plan.get("symbol", "?"))
        ccy = fx.currency_for(symbol)
        side = str(plan.get("side", "?"))
        direction = -1.0 if side == "SHORT" else 1.0
        entry = _safe_float(plan.get("entry_price"), default=None)
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        remaining = _safe_float(plan.get("remaining_quantity"), default=0.0) or 0.0
        max_hold = _safe_float(plan.get("max_hold_minutes"), default=None)
        confidence = _safe_float(plan.get("llm_confidence"), default=None)
        take_profits = _safe_list_of_dicts(plan.get("take_profits") or [])

        side_style = (
            palette["action_buy"] if side == "LONG" else palette["action_sell"]
        )

        # Ligne principale
        entry_str = f"{entry:,.2f}" if entry is not None else "—"
        if entry is not None and stop is not None and entry > 0:
            dist_pct = abs(entry - stop) / entry * 100.0
            stop_str = f"{stop:,.2f} ({dist_pct:.1f}%)"
        elif stop is not None:
            stop_str = f"{stop:,.2f}"
        else:
            stop_str = "—"

        conf_str = f"{confidence:.2f}" if confidence is not None else "—"

        header = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (side, side_style),
            (f"  prix natif {ccy} entrée:", palette["dim"]),
            (f" {entry_str}", "bold"),
            ("  stop:", palette["dim"]),
            (f" {stop_str}", palette["pnl_negative"] if stop else palette["dim"]),
            ("  qté:", palette["dim"]),
            (f" {remaining:,.4f}", "bold"),
            ("  conf:", palette["dim"]),
            (f" {conf_str}", palette["kpi_default"]),
        )
        lines.append(header)

        # Bénéfice estimé scénario stop
        if entry is not None and stop is not None:
            stop_gain = (stop - entry) * remaining * direction
            stop_style = palette["pnl_positive"] if stop_gain >= 0 else palette["pnl_negative"]
            stop_gain_str = f"{stop_gain:+,.2f}"
            lines.append(
                Text.assemble(
                    ("  Stop natif: ", palette["dim"]),
                    (f"{stop:,.2f}", "bold"),
                    ("  → gain natif ", palette["dim"]),
                    (stop_gain_str, stop_style),
                )
            )

        # Take-profits avec bénéfice estimé
        if take_profits:
            for tp in take_profits:
                tp_price = _safe_float(tp.get("price"), default=None)
                tp_name = str(tp.get("name") or "tp?")
                if tp_price is not None:
                    tp_gain = (
                        (tp_price - entry) * remaining * direction
                        if entry is not None
                        else None
                    )
                    tp_gain_str = f"{tp_gain:+,.2f}" if tp_gain is not None else "—"
                    tp_gain_style = (
                        palette["pnl_positive"]
                        if (tp_gain is not None and tp_gain >= 0)
                        else palette["pnl_negative"]
                    )
                    lines.append(
                        Text.assemble(
                            (f"  {tp_name} natif: ", palette["dim"]),
                            (f"{tp_price:,.2f}", palette["pnl_positive"]),
                            ("  → gain natif ", palette["dim"]),
                            (tp_gain_str, tp_gain_style),
                        )
                    )

        # Raison de sortie : champ absent dans trade_plans.json → affiche —
        lines.append(
            Text.assemble(
                ("  Raison sortie:", palette["dim"]),
                (" —", palette["dim"]),
            )
        )

        # max_hold
        if max_hold is not None:
            lines.append(
                Text.assemble(
                    ("  max hold:", palette["dim"]),
                    (f" {int(max_hold)}min", "bold"),
                )
            )

        lines.append(Text(""))  # séparateur

    # Retire le dernier séparateur vide
    if lines and isinstance(lines[-1], Text) and lines[-1].plain == "":
        lines.pop()

    return Panel(
        Group(*lines),
        title="[bold]Plans sortie enrichis[/bold]",
        border_style=palette["border_plans"],
        expand=True,
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
