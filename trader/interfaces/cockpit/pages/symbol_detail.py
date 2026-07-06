"""symbol_detail — drill-down symbole (modal, Esc ferme). Spec design 2f.

Modal ~112 cols :
  header  symbol · company · venue ● open|closed · ccy · last price   esc close
  ligne position
  ─────────────────────────────────────────────────────────────
  col gauche (1.4fr)         │ col droite (1fr)
  ─ WHY citation bordée ─────│ ─ EXIT PLAN ────────────────────
  ─ RECENT DECISIONS ────────│ ─ WATCH ─────────────────────────
  ─ realized line ───────────│ ─ MARKET ────────────────────────
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.aggregates import open_venue_set
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_BORDER,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
)
from trader.reporting.read_models.runtime_state import _safe_float, _safe_list_of_dicts

UTC = timezone.utc


# ─── Local pure helpers ───────────────────────────────────────────────────────


def _short_ts(row: dict, now: datetime) -> str:
    """'HH:MM' if today, 'Mon HH:MM' otherwise."""
    ts = f.parse_ts(row.get("cycle_ts") or row.get("ts"))
    if ts is None:
        return "—"
    if ts.date() == now.date():
        return ts.strftime("%H:%M")
    return ts.strftime("%a %H:%M")


def _amend_rejected_info(rows: list[dict]) -> tuple[str, str] | None:
    """Scan decisions (latest-first) for a rejected amend_exit tool call.

    Returns (time_str, warning_code) or None.
    """
    for row in reversed(rows):
        rt = f.safe_dict(row.get("runtime"))
        for call in _safe_list_of_dicts(rt.get("tool_calls")):
            if not isinstance(call, dict):
                continue
            if call.get("tool") == "amend_exit" and call.get("outcome") == "rejected":
                detail = f.safe_dict(call.get("detail"))
                warnings = detail.get("warnings") or []
                code = ""
                if warnings:
                    first = warnings[0]
                    code = str(first.get("code") if isinstance(first, dict) else first)
                return f.decision_time(row), code
    return None


def _earnings_label(rows: list[dict]) -> str | None:
    """Latest earnings_in_h → 'in Xd' / 'in Xh', None if absent."""
    for row in reversed(rows):
        news = f.safe_dict(row.get("news"))
        hours = _safe_float(news.get("earnings_in_h"), default=None)
        if hours is not None and hours > 0:
            if hours >= 24:
                return f"in {int(hours / 24)}d"
            return f"in {int(hours)}h"
    return None


def _symbol_watch(state: dict, symbol: str) -> dict | None:
    """First indicator watch for this symbol (any on_trigger)."""
    for watch in _safe_list_of_dicts(state.get("indicator_watches")):
        if str(watch.get("symbol") or "") == symbol:
            return watch
    return None


def _realized_total(state: dict, symbol: str) -> tuple[float, int]:
    """(total_net_pnl_usd, n_trips) from recent_trips filtered by symbol."""
    total = 0.0
    count = 0
    for trip in _safe_list_of_dicts(state.get("recent_trips")):
        if str(trip.get("symbol") or "") == symbol:
            pnl = _safe_float(trip.get("pnl"), default=0.0) or 0.0
            total += pnl
            count += 1
    return total, count


def _venue_bias(state: dict, symbol: str) -> str | None:
    """Bias string from venue_state candidates for this symbol, or None."""
    venue_state = f.safe_dict(state.get("venue_state"))
    venues_dict = f.safe_dict(venue_state.get("venues"))
    for venue_data in venues_dict.values():
        if not isinstance(venue_data, dict):
            continue
        for candidate in _safe_list_of_dicts(venue_data.get("candidates")):
            if str(candidate.get("symbol") or "") == symbol:
                bias = str(candidate.get("bias") or "").strip()
                return bias if bias else None
    return None


# ─── Panel builders (pure) ────────────────────────────────────────────────────


def _build_left_column(
    state: dict,
    symbol: str,
    rows: list[dict],
    *,
    now: datetime,
) -> RenderableType:
    """WHY block + RECENT DECISIONS + realized line."""
    parts: list[RenderableType] = []

    # ── WHY ──────────────────────────────────────────────────────────────────
    latest = next(
        (row for row in reversed(rows) if str(row.get("rationale") or "").strip()),
        None,
    )
    if latest is not None:
        action = str(latest.get("action") or "—").upper()
        conf = _safe_float(latest.get("confidence"), default=None)
        time_str = _short_ts(latest, now)

        why_label = Text()
        why_label.append("WHY — ", style=CASYS_FAINT)
        why_label.append(time_str, style=CASYS_FAINT)
        why_label.append(" · ", style=CASYS_FAINT)
        if action == "BUY":
            why_label.append(action, style=f"bold {CASYS_SUCCESS}")
        elif action == "SELL":
            why_label.append(action, style=f"bold {CASYS_ERROR}")
        else:
            why_label.append(action, style=CASYS_DIM)
        if conf is not None:
            why_label.append(f" · conf {conf:.2f}", style=CASYS_FAINT)
        parts.append(why_label)

        # Citation with ▎ border-left prefix (accent color)
        rationale = str(latest.get("rationale") or "").strip()
        rat = Text()
        rat.append("▎ ", style=CASYS_ACCENT)
        rat.append(rationale, style=CASYS_MUTED)
        parts.append(rat)
    else:
        parts.append(Text("no reasoning recorded", style=f"italic {CASYS_FAINT}"))

    parts.append(Text(""))  # spacer

    # ── RECENT DECISIONS ─────────────────────────────────────────────────────
    parts.append(Text("RECENT DECISIONS", style=CASYS_FAINT))

    if rows:
        dec_grid = Table.grid(padding=(0, 2))
        dec_grid.add_column(width=10, no_wrap=True)  # timestamp
        dec_grid.add_column(width=5, no_wrap=True)   # action
        dec_grid.add_column(width=4, no_wrap=True)   # conf
        dec_grid.add_column(no_wrap=True, overflow="ellipsis")  # effect

        for row in list(reversed(rows))[:3]:
            act = str(row.get("action") or "—").upper()
            conf = _safe_float(row.get("confidence"), default=None)
            effect, kind = f.decision_effect(row)

            if act == "BUY":
                act_style = f"bold {CASYS_SUCCESS}"
            elif act == "SELL":
                act_style = f"bold {CASYS_ERROR}"
            else:
                act_style = CASYS_DIM

            if kind == "fill":
                effect_style = CASYS_ERROR if act == "SELL" else CASYS_SUCCESS
            elif kind == "risk":
                effect_style = CASYS_ERROR
            else:
                effect_style = CASYS_DIM
            conf_str = f".{int(conf * 100):02d}" if conf is not None else "— "

            dec_grid.add_row(
                Text(_short_ts(row, now), style=CASYS_DIM),
                Text(act, style=act_style),
                Text(conf_str, style=CASYS_DIM),
                Text(f.clip(effect, limit=42), style=effect_style),
            )

        parts.append(dec_grid)
    else:
        parts.append(Text("no decisions yet", style=f"italic {CASYS_FAINT}"))

    parts.append(Text(""))  # spacer

    # ── Realized ─────────────────────────────────────────────────────────────
    total, count = _realized_total(state, symbol)
    real_line = Text()
    if count == 0:
        real_line.append("no closed trips for this symbol", style=CASYS_FAINT)
    else:
        real_line.append("realized on this symbol  ", style=CASYS_DIM)
        real_line.append(
            f.fmt_signed_money(total),
            style=CASYS_SUCCESS if total >= 0 else CASYS_ERROR,
        )
        real_line.append(
            f"  over {count} closed trip{'s' if count != 1 else ''}",
            style=CASYS_DIM,
        )
    parts.append(real_line)

    return Group(*parts)


def _build_exit_plan_panel(
    state: dict,
    symbol: str,
    rows: list[dict],
) -> RenderableType:
    """EXIT PLAN: hard stop + take profits + amend rejected warning."""
    trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
    plan = f.plan_for_symbol(trade_plans, symbol)

    if not plan:
        body: RenderableType = Text("no exit plan", style=f"italic {CASYS_FAINT}")
    else:
        price = f.price_for_symbol(state, symbol)

        ep_grid = Table.grid(padding=(0, 2))
        ep_grid.add_column(width=11, no_wrap=True)
        ep_grid.add_column(no_wrap=True)

        # hard stop
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        if stop is not None:
            stop_text = Text()
            stop_text.append(f.fmt_compact(stop, decimals=2), style=CASYS_MUTED)
            left_pct = f.stop_left_pct(plan, price)
            if left_pct is not None:
                stop_text.append(f"  left {left_pct:.1f}%", style=CASYS_ERROR)
            entry_risk_pct = f.stop_entry_risk_pct(plan)
            if entry_risk_pct is not None:
                stop_text.append(f" · entry risk {entry_risk_pct:.1f}%", style=CASYS_FAINT)
            ep_grid.add_row(Text("hard stop", style=CASYS_FAINT), stop_text)

        # take-profits
        tps = _safe_list_of_dicts(plan.get("take_profits"))
        if tps:
            tp_parts = []
            for tp in tps[:3]:
                tp_price = _safe_float(tp.get("price"), default=None)
                if tp_price is not None:
                    tp_parts.append(f.fmt_compact(tp_price, decimals=2))
            if tp_parts:
                ep_grid.add_row(
                    Text("take-profit", style=CASYS_FAINT),
                    Text(" · ".join(tp_parts), style=CASYS_MUTED),
                )
            else:
                ep_grid.add_row(Text("take-profit", style=CASYS_FAINT), Text("none", style=CASYS_DIM))
        else:
            ep_grid.add_row(Text("take-profit", style=CASYS_FAINT), Text("none", style=CASYS_DIM))

        # protect
        protect = f.protect_label(plan)
        if protect != "—":
            ep_grid.add_row(
                Text("protect", style=CASYS_FAINT),
                Text(protect, style=CASYS_MUTED),
            )

        ep_parts: list[RenderableType] = [ep_grid]

        # amend rejected warning
        amend = _amend_rejected_info(rows)
        if amend is not None:
            time_str, code = amend
            warn = Text()
            warn.append("▲ ", style=CASYS_WARNING)
            warn.append(f"{time_str} amend rejected", style=CASYS_WARNING)
            if code:
                warn.append(f" — {code}", style=CASYS_WARNING)
            ep_parts.append(warn)

        body = Group(*ep_parts)

    return Panel(
        body,
        title=Text("EXIT PLAN", style=CASYS_FAINT),
        border_style=CASYS_BORDER,
        padding=(0, 1),
    )


def _build_watch_panel(state: dict, symbol: str, *, now: datetime) -> RenderableType:
    """WATCH: condition + on_trigger + TTL countdown."""
    watch = _symbol_watch(state, symbol)

    if watch is None:
        body: RenderableType = Text("no watch set", style=f"italic {CASYS_FAINT}")
    else:
        condition = f.condition_summary(
            watch.get("conditions"), watch.get("logic"), max_items=1, limit=36
        )
        on_trigger = str(watch.get("on_trigger") or "WAKE").upper()
        ttl = f.countdown(watch.get("expires_at"), now=now, prefix="")

        t = Text()
        t.append(condition, style=CASYS_MUTED)
        t.append("  ·  ", style=CASYS_DIM)
        t.append(on_trigger, style=CASYS_DIM)
        t.append("  ·  ", style=CASYS_DIM)
        if ttl in ("expired", "—"):
            t.append(ttl, style=CASYS_FAINT)
        else:
            t.append(ttl, style=CASYS_ACCENT)
            t.append(" left", style=CASYS_DIM)
        body = t

    return Panel(
        body,
        title=Text("WATCH", style=CASYS_FAINT),
        border_style=CASYS_BORDER,
        padding=(0, 1),
    )


def _build_market_panel(
    state: dict,
    symbol: str,
    rows: list[dict],
) -> RenderableType:
    """MARKET: freshness · earnings · bias."""
    mk_grid = Table.grid(padding=(0, 2))
    mk_grid.add_column(width=11, no_wrap=True)
    mk_grid.add_column(no_wrap=True)

    # data freshness
    if f.symbol_is_stale(state, symbol):
        age = f.staleness_age_m(state, symbol)
        data_text = Text()
        data_text.append("▲ stale", style=CASYS_WARNING)
        if age is not None:
            data_text.append(f" {f.age_m(age)}", style=CASYS_WARNING)
    else:
        data_text = Text("● fresh", style=CASYS_SUCCESS)
    mk_grid.add_row(Text("data", style=CASYS_FAINT), data_text)

    # earnings
    earnings = _earnings_label(rows)
    if earnings is not None:
        mk_grid.add_row(Text("earnings", style=CASYS_FAINT), Text(earnings, style=CASYS_MUTED))

    # bias from venue_state
    bias = _venue_bias(state, symbol)
    if bias:
        mk_grid.add_row(Text("bias", style=CASYS_FAINT), Text(bias, style=CASYS_MUTED))

    return Panel(
        mk_grid,
        title=Text("MARKET", style=CASYS_FAINT),
        border_style=CASYS_BORDER,
        padding=(0, 1),
    )


# ─── Public builders (spec signatures) ───────────────────────────────────────


def build_symbol_header(state: dict, symbol: str, *, now: datetime) -> Text:
    """`3443.TW  company  TPE ● open  TWD  last 4,825   esc close`."""
    from trader.market import fx
    from trader.market.rotation.wiring import venue_of

    company_map = f.safe_dict(state.get("company_map"))
    header = Text(overflow="ellipsis")
    header.append(symbol, style=f"bold {CASYS_FG}")

    company = str(company_map.get(symbol) or "").strip()
    if company:
        header.append(f"     {company}", style=CASYS_DIM)

    try:
        venue = venue_of(symbol)
    except Exception:
        venue = "?"

    sessions = state.get("sessions")
    open_set = open_venue_set(sessions, now) if isinstance(sessions, dict) and sessions else None
    header.append(f"     {venue} ", style=CASYS_DIM)
    if open_set is None:
        header.append("· market ?", style=CASYS_FAINT)
    else:
        is_open = venue in open_set
        header.append(
            "● open" if is_open else "○ closed",
            style=CASYS_SUCCESS if is_open else CASYS_FAINT,
        )

    try:
        header.append(f"     {fx.currency_for(symbol)}", style=CASYS_DIM)
    except Exception:
        pass

    price = f.price_for_symbol(state, symbol)
    if price is not None:
        header.append("   last ", style=CASYS_DIM)
        header.append(f.fmt_compact(price, decimals=2), style=f"bold {CASYS_MUTED}")
        if f.symbol_is_stale(state, symbol):
            age = f.staleness_age_m(state, symbol)
            header.append(f"  ▲ {f.age_m(age)}" if age else "  ▲ stale", style=CASYS_WARNING)

    # esc close — appended right, serves as visual anchor
    header.append("   esc close", style=CASYS_FAINT)
    return header


def build_symbol_body(state: dict, symbol: str, *, now: datetime) -> RenderableType:
    """Full drill-down (spec 2f) : position + 2-column grid (WHY/decisions | plans/watch/market)."""
    # Decisions for this symbol (from recent ledger)
    rows = [
        row
        for row in _safe_list_of_dicts(state.get("recent_decisions"))
        if str(row.get("symbol") or "") == symbol
    ]

    # Position line
    portfolio = f.safe_dict(state.get("portfolio"))
    holdings = [
        h
        for h in _safe_list_of_dicts(portfolio.get("holdings"))
        if f.holding_symbol(h) == symbol
    ]

    pos_line = Text()
    if holdings:
        holding = holdings[0]
        pnl = f.holding_pnl(holding)
        pnl_style = CASYS_SUCCESS if pnl >= 0 else CASYS_ERROR
        qty = f.holding_quantity(holding)
        avg = _safe_float(holding.get("avg_price"), default=None)
        notional = f.holding_notional(holding)

        pos_line.append("position  ", style=CASYS_DIM)
        pos_line.append(f.fmt_money(notional), style=f"bold {CASYS_MUTED}")
        if avg is not None:
            pos_line.append(f"  ({qty:g} @ {f.fmt_compact(avg, decimals=2)} avg)", style=CASYS_DIM)
        pos_line.append("  ·  P&L  ", style=CASYS_DIM)
        pos_line.append(f.fmt_signed_money(pnl), style=pnl_style)

        # P&L pct
        last_price = f.holding_native_price(holding) or 0.0
        if avg and avg > 0 and last_price:
            side = 1 if qty >= 0 else -1
            pct = (last_price - avg) / avg * 100.0 * side
            pos_line.append(f" ({pct:+.1f}%)", style=pnl_style)

        # opened date from trade plan
        trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
        plan = f.plan_for_symbol(trade_plans, symbol)
        opened_at = plan.get("opened_at") if plan else None
        if opened_at:
            ts = f.parse_ts(opened_at)
            if ts:
                pos_line.append("  ·  opened ", style=CASYS_DIM)
                pos_line.append(ts.strftime("%a %H:%M"), style=CASYS_DIM)
    else:
        pos_line.append("no open position", style=CASYS_FAINT)

    # 2-column body grid
    body_grid = Table(
        show_header=False,
        show_edge=False,
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 0),
    )
    body_grid.add_column(ratio=14)
    body_grid.add_column(ratio=10)
    body_grid.add_row(
        _build_left_column(state, symbol, rows, now=now),
        Group(
            _build_exit_plan_panel(state, symbol, rows),
            _build_watch_panel(state, symbol, now=now),
            _build_market_panel(state, symbol, rows),
        ),
    )

    return Group(
        build_symbol_header(state, symbol, now=now),
        pos_line,
        Text(""),  # spacer
        body_grid,
    )


# ─── Widget ───────────────────────────────────────────────────────────────────


class SymbolDetailScreen(ModalScreen[None]):
    """Modal ~112 cols au-dessus de la page assombrie. Esc ferme.

    Poussé via push_screen(SymbolDetailScreen(symbol)) depuis l'app.
    """

    BINDINGS = [Binding("escape", "close_detail", "close")]
    DEFAULT_CSS = """
    SymbolDetailScreen { align: center middle; background: rgba(10,9,8,0.55); }
    SymbolDetailScreen > VerticalScroll {
        width: 112;
        max-width: 95%;
        height: 82%;
        border: solid #332c23;
        border-title-color: #FFB86F;
        border-title-style: bold;
        background: #14110e;
        padding: 1 2;
    }
    """

    def __init__(self, symbol: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._symbol = symbol

    def compose(self) -> ComposeResult:
        with VerticalScroll() as panel:
            panel.border_title = self._symbol
            yield Static(id="symbol-detail-body")

    def on_mount(self) -> None:
        state = getattr(self.app, "_last_state", None) or {}
        self.query_one("#symbol-detail-body", Static).update(
            build_symbol_body(state, self._symbol, now=datetime.now(UTC))
        )

    def action_close_detail(self) -> None:
        self.dismiss(None)
