"""Positions, attribution, and trade table Rich builders."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trader.domain.execution.fill_accounting import POSITION_EPSILON
from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.interfaces.ui.panels.common import (
    _fmt_fee_cost,
    _fmt_holding_duration,
    _fmt_int,
    _fmt_number,
    _fmt_percent,
    _fmt_price_pair,
    _fmt_signed_money,
    _fmt_symbol_short,
    _fmt_time_hms,
    _market_badge,
    _truncate,
)
from trader.domain.market import fx
from trader.reporting.read_models.runtime_state import (
    UTC,
    _safe_float,
    _safe_list_of_dicts,
)


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

    visible_holdings = [
        holding
        for holding in holdings
        if abs(_safe_float(holding.get("quantity"), default=0.0) or 0.0)
        > POSITION_EPSILON
    ]

    for h in visible_holdings:
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

    if not visible_holdings:
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


__all__ = [
    "_build_attribution_panel",
    "_build_positions_panel",
    "_confidence_bar",
    "build_closed_trades_table",
    "build_trades_table",
    "compute_realized_pnl_by_fill",
]
