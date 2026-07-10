"""portfolio — page 2 du cockpit casys (positions, exposition, trades fermés).

Grille 1fr | 44 cols : POSITIONS (SymbolTable avec drill-down, tri cycling) à
gauche ; EXPOSURE · FX → USD · CLOSED TRADES empilés à droite.

Builders PURS : (state[, now]) → renderable, sans I/O, sans horloge implicite.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.derive import equity_snapshot, exposure, positions_by_pnl
from trader.interfaces.cockpit.pages._shared import PANEL_CSS, ResizeRefresh, SymbolTable, preserve_cursor, rows_available
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_HAIRLINE,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
)
from trader.support.coercion import (
    dict_list as _safe_list_of_dicts,
    finite_float as _safe_float,
)

UTC = timezone.utc

_SORT_LABELS = ("by |P&L|", "by value", "by %")
_N_SORT_MODES = 3

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


# ---------------------------------------------------------------------------
# Pure helpers (local — format.py untouched)
# ---------------------------------------------------------------------------


def _pnl_pct(holding: dict) -> float:
    """P&L% = unrealized net USD / cost basis USD (signed)."""
    pnl = f.holding_pnl(holding)
    qty = f.holding_quantity(holding)
    avg = _safe_float(holding.get("avg_price"), default=None)
    fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0
    cost = abs(qty * (avg or 0.0) * fx_rate)
    return (pnl / cost * 100.0) if cost > 0 else 0.0


def _fmt_date_exit(ts: object) -> str:
    """exit_ts ISO → "03 Jul" locale-independent / "—" si invalide."""
    parsed = f.parse_ts(ts)
    if parsed is None:
        return "—"
    return f"{parsed.day:02d} {_MONTHS[parsed.month - 1]}"


def _sort_holdings(holdings: list[dict], sort_mode: int) -> list[dict]:
    """Sort: 0 = |P&L| desc, 1 = notional value desc, 2 = |P&L%| desc."""
    if sort_mode == 1:
        return sorted(holdings, key=lambda h: f.holding_notional(h), reverse=True)
    if sort_mode == 2:
        return sorted(holdings, key=lambda h: abs(_pnl_pct(h)), reverse=True)
    # Default: |P&L|
    return sorted(holdings, key=lambda h: abs(f.holding_pnl(h)), reverse=True)


def _data_cell(state: dict, symbol: str) -> Text:
    """DATA column: ● (success, fresh) or ▲ Xh (warning, stale).

    La staleness se juge par symbol_is_stale (présence de la clé) — une
    entrée stale sans âge affiche ▲ seul, jamais un faux ● fresh.
    """
    if not f.symbol_is_stale(state, symbol):
        return Text("●", style=CASYS_SUCCESS)
    age_m = f.staleness_age_m(state, symbol)
    label = f"▲ {f.age_m(age_m)}" if age_m is not None else "▲"
    return Text(label, style=CASYS_WARNING)


def _fmt_qty(qty: float) -> str:
    abs_qty = abs(qty)
    if abs_qty == int(abs_qty):
        return f"{int(abs_qty):,}"
    return f"{abs_qty:g}"


# ---------------------------------------------------------------------------
# EXPOSURE builder (pure, no `now` needed)
# ---------------------------------------------------------------------------


def build_exposure(state: dict) -> RenderableType:
    """EXPOSURE : barres long/short signées + par venue, rel. au gross."""
    try:
        exp = exposure(state)
    except Exception:
        return Text("exposure data unavailable", style=f"italic {CASYS_FAINT}")

    snap = equity_snapshot(state)
    gross = max(exp.gross, 1.0)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=5, no_wrap=True)       # label
    grid.add_column(min_width=18, no_wrap=True)  # bar
    grid.add_column(width=7, no_wrap=True)       # $ value

    bar_long = f.bar(exp.long_usd, gross, width=18)
    grid.add_row(
        Text("long", style=CASYS_SUCCESS),
        Text(bar_long, style=CASYS_SUCCESS),
        Text(f"${exp.long_usd / 1000:.1f}k", style=CASYS_MUTED),
    )
    bar_short = f.bar(exp.short_usd, gross, width=18)
    grid.add_row(
        Text("short", style=CASYS_ERROR),
        Text(bar_short, style=CASYS_ERROR),
        Text(f"${exp.short_usd / 1000:.1f}k", style=CASYS_MUTED),
    )

    # Net / gross footnote
    net = exp.net
    equity_val = max(snap.equity, 1.0)
    gross_pct = exp.gross / equity_val * 100.0
    net_label = "long" if net >= 0 else "short"
    nl_style = CASYS_SUCCESS if net >= 0 else CASYS_ERROR
    net_footnote = Text()
    net_footnote.append("net ", style=CASYS_DIM)
    net_footnote.append(f"{net_label} ${abs(net) / 1000:.1f}k", style=nl_style)
    net_footnote.append(f" · gross {gross_pct:.0f}% of equity", style=CASYS_DIM)

    # Venue breakdown
    parts: list[RenderableType] = [grid, net_footnote]
    venue_items = sorted(
        ((v, a) for v, a in exp.by_venue.items() if v != "?" and a > 0),
        key=lambda x: x[1],
        reverse=True,
    )
    if venue_items:
        vgrid = Table.grid(padding=(0, 1))
        vgrid.add_column(width=5, no_wrap=True)
        vgrid.add_column(min_width=18, no_wrap=True)
        vgrid.add_column(width=7, no_wrap=True)
        for venue, amt in venue_items:
            vgrid.add_row(
                Text(venue, style=CASYS_DIM),
                Text(f.bar(amt, gross, width=18), style=CASYS_ACCENT),
                Text(f"${amt / 1000:.1f}k", style=CASYS_MUTED),
            )
        parts.append(vgrid)

    return Group(*parts)


# ---------------------------------------------------------------------------
# FX → USD builder (pure)
# ---------------------------------------------------------------------------


def build_fx(state: dict, *, now: datetime) -> RenderableType:
    """FX → USD : taux actuels + horodatage de refresh (state["ts"])."""
    fx_rates: dict = f.safe_dict(state.get("fx_rates"))
    non_usd = {k: v for k, v in fx_rates.items() if k != "USD"}
    if not non_usd:
        return Text("FX data not available", style=f"italic {CASYS_FAINT}")

    row = Table.grid(padding=(0, 2))
    for _ in non_usd:
        row.add_column(no_wrap=True)
    cells = []
    for ccy, rate in non_usd.items():
        cell = Text()
        cell.append(ccy, style=CASYS_FAINT)
        rate_str = f"{float(rate):.4f}" if rate is not None else "?"
        cell.append(f" {rate_str}", style=CASYS_MUTED)
        cells.append(cell)
    row.add_row(*cells)

    ts_label = f.hhmm(state.get("ts"))
    if ts_label == "—":
        ts_label = now.strftime("%H:%M")
    footnote = Text(
        f"refreshed {ts_label} UTC · P&L is always net of fees, in USD",
        style=CASYS_FAINT,
    )
    return Group(row, footnote)


# ---------------------------------------------------------------------------
# CLOSED TRADES builder (pure)
# ---------------------------------------------------------------------------


def build_closed_trades(
    state: dict,
    *,
    now: datetime,
    limit: int = 8,
    wide: bool = True,
) -> RenderableType:
    """CLOSED TRADES — récents : grille DATE/SYM/dir/P&L$/REASON[/DUR] + footer stats.

    wide=True  (panel ≥ 36 usable cols): include DUR column.
    wide=False (narrower panel):         drop DUR to keep REASON visible.
    limit: max rows, computed adaptively in update_state via rows_available().
    """
    trips = _safe_list_of_dicts(state.get("recent_trips"))
    if not trips:
        attribution = f.safe_dict(state.get("attribution"))
        trips = _safe_list_of_dicts(attribution.get("recent_trips"))

    attribution = f.safe_dict(state.get("attribution"))

    if not trips:
        return Text("no closed trades yet", style=f"italic {CASYS_FAINT}")

    reason_w = 12 if wide else 10
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=6, no_wrap=True)         # DATE
    grid.add_column(width=9, no_wrap=True)         # SYM
    grid.add_column(width=1, no_wrap=True)         # dir
    grid.add_column(width=9, justify="right", no_wrap=True)  # P&L $ (montant net, jusqu'à ±99,999)
    grid.add_column(width=reason_w, no_wrap=True)  # REASON
    if wide:
        grid.add_column(width=5, no_wrap=True)     # DUR

    for trip in trips[:limit]:
        symbol = str(trip.get("symbol") or "—")
        side = str(trip.get("side") or "LONG").upper()
        side_long = side == "LONG"
        pnl = _safe_float(trip.get("pnl"), default=0.0) or 0.0
        pnl_style = CASYS_SUCCESS if pnl >= 0 else CASYS_ERROR
        holding_m = trip.get("holding_minutes")
        reason_raw = str(trip.get("exit_reason") or "—")[:reason_w]

        row_cells: list[Text] = [
            Text(_fmt_date_exit(trip.get("exit_ts")), style=CASYS_FAINT),
            Text(symbol, style=f"bold {CASYS_FG}"),
            Text("L" if side_long else "S", style=CASYS_SUCCESS if side_long else CASYS_ERROR),
            Text(f.fmt_signed(pnl), style=pnl_style),
            Text(reason_raw, style=CASYS_DIM),
        ]
        if wide:
            row_cells.append(Text(f.duration_m(holding_m), style=CASYS_FAINT))
        grid.add_row(*row_cells)

    realized_pnl = _safe_float(attribution.get("realized_pnl"), default=0.0) or 0.0
    total_fees = _safe_float(attribution.get("total_commissions"), default=0.0) or 0.0
    n_closed = int(attribution.get("n_closed_trades") or 0)

    sep = Text("─" * 38, style=CASYS_HAIRLINE)
    footer = Text()
    footer.append("realized ", style=CASYS_DIM)
    footer.append(f.fmt_signed_money(realized_pnl), style=CASYS_SUCCESS if realized_pnl >= 0 else CASYS_ERROR)
    footer.append(f" · fees ${total_fees:,.0f}", style=CASYS_DIM)
    footer.append(f" · {n_closed} trips", style=CASYS_DIM)

    return Group(grid, sep, footer)


# ---------------------------------------------------------------------------
# Positions row data (pure builder, testable without UI)
# ---------------------------------------------------------------------------


def build_positions_rows(state: dict, sort_mode: int = 0) -> list[dict]:
    """Données structurées des lignes de position (sans Rich/Textual)."""
    holdings = _sort_holdings(positions_by_pnl(state), sort_mode)
    trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
    rows: list[dict] = []
    for holding in holdings:
        symbol = f.holding_symbol(holding)
        qty = f.holding_quantity(holding)
        pnl = f.holding_pnl(holding)
        pnl_pct_val = _pnl_pct(holding)
        notional = f.holding_notional(holding)
        avg = _safe_float(holding.get("avg_price"), default=None)
        last = _safe_float(holding.get("last_price"), default=None)
        plan = f.plan_for_symbol(trade_plans, symbol)
        stop_dist = f.stop_distance_pct(plan, last) if plan else None
        stop_left = f.stop_left_pct(plan, last) if plan else None
        stop_entry_risk = f.stop_entry_risk_pct(plan) if plan else None
        rows.append(
            {
                "symbol": symbol,
                "side": "L" if qty >= 0 else "S",
                "qty": qty,
                "avg": avg,
                "last": last,
                "notional": notional,
                "pnl": pnl,
                "pnl_pct": pnl_pct_val,
                "stop_dist": stop_dist,
                "stop_left_pct": stop_left,
                "stop_entry_risk_pct": stop_entry_risk,
                "is_stale": f.symbol_is_stale(state, symbol),
                "data_age_m": f.staleness_age_m(state, symbol),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class PortfolioPage(ResizeRefresh, Static):
    """Page 2 — Portfolio : positions complètes, exposition, trades fermés.

    Binding ``o`` : cycle tri |P&L| → value → %.
    Enter sur une ligne : drill-down symbole (SymbolChosen → app.py).
    """

    BINDINGS = [
        Binding("o", "cycle_sort", "sort |pnl| / value / %", show=False),
    ]

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    PortfolioPage {
        layout: horizontal;
        height: 100%;
        padding: 1 2 0 2;
    }
    PortfolioPage #positions-panel {
        width: 1fr;
        height: 100%;
        margin-right: 1;
    }
    PortfolioPage #positions-table {
        height: 1fr;
    }
    PortfolioPage #positions-footer { height: auto; }
    PortfolioPage #portfolio-right {
        height: 100%;
        layout: vertical;
    }
    PortfolioPage #exposure-panel {
        height: auto;
        min-height: 8;
        margin-bottom: 1;
    }
    PortfolioPage #fx-panel {
        height: 6;
        margin-bottom: 1;
    }
    PortfolioPage #closed-panel {
        height: 1fr;
    }
    PortfolioPage .casys-panel Static { height: auto; }
    """
    )

    _sort_mode: int = 0
    _last_state: dict | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="positions-panel", classes="casys-panel") as pos:
            pos.border_title = "POSITIONS — sorted by |P&L|"
            yield SymbolTable(id="positions-table")
            yield Static(id="positions-footer")
        with Vertical(id="portfolio-right", classes="right-col"):
            with VerticalScroll(id="exposure-panel", classes="casys-panel") as exp:
                exp.border_title = "EXPOSURE"
                yield Static(id="exposure-body")
            with VerticalScroll(id="fx-panel", classes="casys-panel") as fx_w:
                fx_w.border_title = "FX → USD"
                yield Static(id="fx-body")
            with VerticalScroll(id="closed-panel", classes="casys-panel") as ct:
                ct.border_title = "CLOSED TRADES — recent"
                yield Static(id="closed-body")

    # Jeux de colonnes par largeur décroissante : (seuil_min, clés droppées).
    # Les moins décisionnelles partent d'abord : AVG, puis VALUE, puis QTY.
    _COLUMN_DROPS: tuple[tuple[int, frozenset[str]], ...] = (
        (95, frozenset()),
        (85, frozenset({"AVG"})),
        (75, frozenset({"AVG", "VALUE $"})),
        (0, frozenset({"AVG", "VALUE $", "QTY"})),
    )
    _ALL_COLUMNS: tuple[tuple[str, int], ...] = (
        ("SYM", 9),
        ("", 2),  # side L/S
        ("QTY", 7),
        ("AVG", 8),
        ("LAST", 8),
        ("VALUE $", 8),
        ("P&L $", 7),
        ("P&L %", 7),
        ("STOP LEFT", 9),
        ("DATA", 10),
    )
    _active_drops: frozenset[str] | None = None

    def on_mount(self) -> None:
        self._rebuild_columns(force=True)

    def _drops_for_width(self) -> frozenset[str]:
        width = self.query_one("#positions-table", SymbolTable).size.width or 0
        if width <= 0:
            return frozenset()
        for threshold, drops in self._COLUMN_DROPS:
            if width >= threshold:
                return drops
        return self._COLUMN_DROPS[-1][1]

    def _rebuild_columns(self, *, force: bool = False) -> None:
        """Reconstruit les colonnes si le jeu adapté à la largeur a changé."""
        drops = self._drops_for_width()
        if not force and drops == self._active_drops:
            return
        self._active_drops = drops
        table = self.query_one("#positions-table", SymbolTable)
        table.clear(columns=True)
        for name, width in self._ALL_COLUMNS:
            if name not in drops:
                table.add_column(name, width=width)

    def _refresh_positions(self, state: dict) -> None:
        """Repopule la SymbolTable + footer depuis l'état courant."""
        table = self.query_one("#positions-table", SymbolTable)
        self._rebuild_columns()
        drops = self._active_drops or frozenset()
        with preserve_cursor(table):
            self._populate_positions(table, state, drops)

    def _populate_positions(self, table: SymbolTable, state: dict, drops: frozenset[str]) -> None:
        table.clear()

        holdings = _sort_holdings(positions_by_pnl(state), self._sort_mode)
        trade_plans = _safe_list_of_dicts(state.get("trade_plans"))

        gross_long = gross_short = unrealized_total = 0.0

        for idx, holding in enumerate(holdings):
            symbol = f.holding_symbol(holding)
            qty = f.holding_quantity(holding)
            side_long = qty >= 0
            pnl = f.holding_pnl(holding)
            pnl_pct_val = _pnl_pct(holding)
            notional = f.holding_notional(holding)
            avg = _safe_float(holding.get("avg_price"), default=None)
            last = _safe_float(holding.get("last_price"), default=None)
            pnl_style = CASYS_SUCCESS if pnl >= 0 else CASYS_ERROR

            plan = f.plan_for_symbol(trade_plans, symbol)
            stop_left = f.stop_left_pct(plan, last) if plan else None
            stop_str = f"{stop_left:.1f}%" if stop_left is not None else "—"

            if side_long:
                gross_long += notional
            else:
                gross_short += notional
            unrealized_total += pnl

            cells: dict[str, Text] = {
                "SYM": Text(symbol, style=f"bold {CASYS_FG}"),
                "": Text("L" if side_long else "S", style=CASYS_SUCCESS if side_long else CASYS_ERROR),
                "QTY": Text(_fmt_qty(qty), style=CASYS_MUTED),
                "AVG": Text(f.fmt_compact(avg, decimals=2), style=CASYS_DIM),
                "LAST": Text(f.fmt_compact(last, decimals=2), style=CASYS_MUTED),
                "VALUE $": Text(f"${notional:,.0f}" if notional else "—", style=CASYS_FG),
                "P&L $": Text(f.fmt_signed(pnl), style=pnl_style),
                "P&L %": Text(f"{pnl_pct_val:+.1f}%", style=pnl_style),
                "STOP LEFT": Text(stop_str, style=CASYS_DIM),
                "DATA": _data_cell(state, symbol),
            }
            table.add_row(
                *(cell for name, cell in cells.items() if name not in drops),
                key=f"{symbol}|{idx}",
            )

        n = len(holdings)
        label = _SORT_LABELS[self._sort_mode]
        pos_panel = self.query_one("#positions-panel", VerticalScroll)
        pos_panel.border_title = (
            f"POSITIONS — {n} · sorted {label}" if n else "POSITIONS"
        )

        gross = gross_long + gross_short
        net_long = gross_long - gross_short
        footer = Text()
        footer.append(f"{n} positions", style=CASYS_DIM)
        footer.append(" · gross ", style=CASYS_DIM)
        footer.append(f"${gross / 1000:.1f}k" if gross else "$0", style=CASYS_MUTED)
        footer.append(" · net long ", style=CASYS_DIM)
        footer.append(
            f"${net_long / 1000:.1f}k",
            style=CASYS_SUCCESS if net_long >= 0 else CASYS_ERROR,
        )
        footer.append(" · unrealized ", style=CASYS_DIM)
        footer.append(
            f.fmt_signed(unrealized_total),
            style=CASYS_SUCCESS if unrealized_total >= 0 else CASYS_ERROR,
        )
        footer.append(" · enter inspect symbol", style=CASYS_FAINT)
        self.query_one("#positions-footer", Static).update(footer)

    def update_state(self, state: dict) -> None:
        """Pull pur depuis le read model — jamais d'exception (état partiel toléré)."""
        now = datetime.now(UTC)
        self._last_state = state
        try:
            self._refresh_positions(state)
        except Exception:
            pass
        try:
            self.query_one("#exposure-body", Static).update(build_exposure(state))
        except Exception:
            pass
        try:
            self.query_one("#fx-body", Static).update(build_fx(state, now=now))
        except Exception:
            pass
        try:
            closed_panel = self.query_one("#closed-panel", VerticalScroll)
            _limit = rows_available(closed_panel, reserved=3, minimum=4)
            # DUR n'apparaît que si les 5 colonnes principales + DUR tiennent (~47 cols)
            _wide = (closed_panel.content_size.width or 40) >= 48
            self.query_one("#closed-body", Static).update(
                build_closed_trades(state, now=now, limit=_limit, wide=_wide)
            )
        except Exception:
            pass

    def action_cycle_sort(self) -> None:
        """Cycle: |P&L| → value → % → |P&L|"""
        self._sort_mode = (self._sort_mode + 1) % _N_SORT_MODES
        if self._last_state is not None:
            try:
                self._refresh_positions(self._last_state)
            except Exception:
                pass
