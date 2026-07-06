"""symbol_detail — drill-down symbole (modal, Esc ferme). Spec design 2f."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.aggregates import open_venue_set
from trader.interfaces.ui.palette import (
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_SUCCESS,
)
from trader.reporting.read_models.runtime_state import _safe_float, _safe_list_of_dicts

UTC = timezone.utc


def build_symbol_header(state: dict, symbol: str, *, now: datetime) -> Text:
    """`3443.TW · company · TPE ● open · TWD · last 4,825`."""
    from trader.market import fx
    from trader.market.rotation.wiring import venue_of

    company_map = f.safe_dict(state.get("company_map"))
    header = Text()
    header.append(symbol, style=f"bold {CASYS_FG}")
    company = str(company_map.get(symbol) or "").strip()
    if company:
        header.append(f"  {company}", style=CASYS_DIM)
    try:
        venue = venue_of(symbol)
    except Exception:
        venue = "?"
    sessions = state.get("sessions")
    open_set = open_venue_set(sessions, now) if isinstance(sessions, dict) and sessions else None
    header.append(f"   {venue} ", style=CASYS_DIM)
    if open_set is None:
        header.append("· market ?", style=CASYS_FAINT)
    else:
        is_open = venue in open_set
        header.append(
            "● open" if is_open else "○ closed",
            style=CASYS_SUCCESS if is_open else CASYS_FAINT,
        )
    try:
        header.append(f"   {fx.currency_for(symbol)}", style=CASYS_DIM)
    except Exception:
        pass
    price = f.price_for_symbol(state, symbol)
    if price is not None:
        header.append("  last ", style=CASYS_DIM)
        header.append(f.fmt_compact(price, decimals=2), style=f"bold {CASYS_FG}")
    if f.symbol_is_stale(state, symbol):
        age = f.staleness_age_m(state, symbol)
        header.append(f"  ▲ {f.age_m(age)}" if age else "  ▲ stale", style="#e5c07b")
    return header


def build_symbol_body(state: dict, symbol: str, *, now: datetime) -> RenderableType:
    """Contenu du drill-down — position · why · decisions (spec 2f à compléter)."""
    parts: list[RenderableType] = [build_symbol_header(state, symbol, now=now)]

    portfolio = f.safe_dict(state.get("portfolio"))
    holdings = [h for h in _safe_list_of_dicts(portfolio.get("holdings")) if f.holding_symbol(h) == symbol]
    if holdings:
        holding = holdings[0]
        pnl = f.holding_pnl(holding)
        pnl_style = CASYS_SUCCESS if pnl >= 0 else CASYS_ERROR
        qty = f.holding_quantity(holding)
        avg = _safe_float(holding.get("avg_price"), default=None)
        line = Text()
        line.append("position ", style=CASYS_DIM)
        line.append(f.fmt_money(f.holding_notional(holding)), style=f"bold {CASYS_FG}")
        line.append(
            f" ({qty:g} @ {f.fmt_compact(avg, decimals=2)} avg)",
            style=CASYS_DIM,
        )
        line.append(" · P&L ", style=CASYS_DIM)
        line.append(f.fmt_signed_money(pnl), style=pnl_style)
        parts.append(line)
    else:
        parts.append(Text("no open position", style=CASYS_FAINT))

    rows = [
        row
        for row in _safe_list_of_dicts(state.get("recent_decisions"))
        if str(row.get("symbol") or "") == symbol
    ]
    latest = next(
        (row for row in reversed(rows) if str(row.get("rationale") or "").strip()),
        None,
    )
    if latest is not None:
        why = Text()
        why.append("WHY  ", style="bold #FFB86F")
        why.append(str(latest.get("action") or "—").upper(), style=f"bold {CASYS_FG}")
        conf = _safe_float(latest.get("confidence"), default=None)
        if conf is not None:
            why.append(f" · conf {conf:.2f}", style=CASYS_DIM)
        why.append(f" · {f.decision_time(latest)}", style=CASYS_FAINT)
        parts.append(why)
        parts.append(Text(f"▎ {str(latest.get('rationale')).strip()}", style=CASYS_DIM))

    return Group(*parts)


class SymbolDetailScreen(ModalScreen[None]):
    """Modal ~112 cols au-dessus de la page assombrie. Esc ferme."""

    BINDINGS = [Binding("escape", "close_detail", "close")]
    DEFAULT_CSS = """
    SymbolDetailScreen { align: center middle; }
    SymbolDetailScreen > VerticalScroll {
        width: 112;
        max-width: 95%;
        height: 80%;
        border: solid #332c23;
        border-title-color: #FFB86F;
        border-title-style: bold;
        background: $background;
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
