"""home — page 1 « Decision Journal » : le raisonnement de l'agent d'abord.

Grille 1.65fr | 1fr : JOURNAL (pourquoi l'agent a fait ce qu'il a fait) à
gauche ; EQUITY · POSITIONS · NEXT TO FIRE empilés à droite.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.derive import (
    equity_snapshot,
    journal_entries,
    ledger_total,
    next_to_fire,
    positions_by_pnl,
)
from trader.interfaces.cockpit.pages._shared import PANEL_CSS, build_equity_chart
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_HAIRLINE,
    CASYS_MUTED,
    CASYS_SUCCESS,
)

UTC = timezone.utc

# Fonds teintés des chips BUY/SELL (≈ rgba sur #0f0e0c)
_BUY_CHIP = f"bold {CASYS_SUCCESS} on #272c20"
_SELL_CHIP = f"bold {CASYS_ERROR} on #2d1e19"

_EFFECT_STYLE = {
    "watch": CASYS_ACCENT,
    "plan": CASYS_ACCENT,
    "wake": CASYS_ACCENT,
    "fill": CASYS_SUCCESS,
    "risk": CASYS_ERROR,
    "none": CASYS_DIM,
}


def _action_chip(action: str) -> Text:
    if action == "BUY":
        return Text(" BUY ", style=_BUY_CHIP)
    if action == "SELL":
        return Text(" SELL ", style=_SELL_CHIP)
    return Text(f" {action} ", style=CASYS_MUTED)


def build_journal(state: dict, *, now: datetime, limit: int = 8) -> RenderableType:
    """Entrées du journal : en-tête · rationale · effet, séparées par hairline."""
    entries = journal_entries(state, now=now, limit=limit)
    if not entries:
        return Text(
            "the agent's reasoning will stream here, decision by decision",
            style=f"italic {CASYS_FAINT}",
        )

    parts: list[RenderableType] = []
    for index, entry in enumerate(entries):
        if index:
            parts.append(Text("─" * 60, style=CASYS_HAIRLINE))
        header = Text()
        header.append(entry.time, style=CASYS_FAINT)
        header.append("  ")
        header.append(entry.symbol, style=f"bold {CASYS_FG}")
        header.append("  ")
        header.append_text(_action_chip(entry.action))
        header.append("  ")
        header.append_text(f.conf_meter(entry.confidence))
        parts.append(header)
        if entry.rationale:
            parts.append(Text(entry.rationale, style=CASYS_MUTED))
        if entry.effect and entry.effect != "—":
            style = _EFFECT_STYLE.get(entry.effect_kind, CASYS_DIM)
            if entry.effect_kind == "fill" and entry.action == "SELL":
                style = CASYS_ERROR
            suffix = " ✓" if entry.effect_kind == "fill" else ""
            parts.append(Text(f"→ {entry.effect}{suffix}", style=style))

    total = ledger_total(state)
    remaining = max(0, total - len(entries))
    if remaining:
        parts.append(Text(f"↓ {remaining} more in the ledger — press 3", style=CASYS_FAINT))
    return Group(*parts)


def build_equity_summary(state: dict) -> RenderableType:
    snap = equity_snapshot(state)
    headline = Text()
    headline.append(f.fmt_money(snap.equity), style=f"bold {CASYS_FG}")
    if snap.equity > 0:
        style = CASYS_SUCCESS if snap.return_pct >= 0 else CASYS_ERROR
        headline.append(f" {f.fmt_pct(snap.return_pct, decimals=2)}", style=style)
    headline.append(f" · cash {snap.cash_pct:.0f}%", style=CASYS_FAINT)
    return Group(headline, build_equity_chart(f.equity_curve(state), width=40, height=5))


def build_home_positions(state: dict, *, limit: int = 7) -> RenderableType:
    """sym bold · L/S · barre P&L signée · USD signé ; « + N more — press 2 »."""
    holdings = positions_by_pnl(state)
    if not holdings:
        return Text(
            "no positions yet — decisions land after the first cycle",
            style=f"italic {CASYS_FAINT}",
        )
    max_abs = max((abs(f.holding_pnl(h)) for h in holdings[:limit]), default=0.0)
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)
    grid.add_column(no_wrap=True, width=1)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True, justify="right")
    for holding in holdings[:limit]:
        pnl = f.holding_pnl(holding)
        pnl_style = CASYS_SUCCESS if pnl >= 0 else CASYS_ERROR
        side_long = f.holding_quantity(holding) >= 0
        grid.add_row(
            Text(f.holding_symbol(holding), style=f"bold {CASYS_FG}"),
            Text("L" if side_long else "S", style=CASYS_SUCCESS if side_long else CASYS_ERROR),
            Text(f.signed_bar(pnl, max_abs, width=8), style=pnl_style),
            Text(f.fmt_signed(pnl), style=pnl_style),
        )
    parts: list[RenderableType] = [grid]
    if len(holdings) > limit:
        parts.append(Text(f"+ {len(holdings) - limit} more — press 2", style=CASYS_FAINT))
    return Group(*parts)


def build_next_to_fire(state: dict, *, now: datetime, limit: int = 6) -> RenderableType:
    items = next_to_fire(state, now=now, limit=limit)
    if not items:
        return Text("nothing armed, nothing watched", style=f"italic {CASYS_FAINT}")
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)
    grid.add_column(no_wrap=True, overflow="ellipsis")
    grid.add_column(no_wrap=True, justify="right", width=5)
    for item in items:
        grid.add_row(
            Text(item.label, style=f"bold {CASYS_FG}" if item.kind != "wake" else CASYS_DIM),
            Text(item.detail, style=CASYS_DIM),
            Text(item.countdown, style=CASYS_ACCENT),
        )
    return grid


class HomePage(Static):
    """Page 1 — Decision Journal."""

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    HomePage {
        layout: horizontal;
        height: 100%;
        padding: 1 2 0 2;
    }
    HomePage #journal-panel {
        width: 5fr;
        height: 100%;
        margin-right: 1;
    }
    HomePage #journal-scroll { height: 100%; }
    HomePage #home-right {
        width: 3fr;
        height: 100%;
    }
    HomePage #equity-panel { height: 9; margin-bottom: 1; }
    HomePage #positions-panel { height: 1fr; margin-bottom: 1; }
    HomePage #fire-panel { height: 1fr; }
    HomePage .casys-panel Static { height: auto; }
    """
    )

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="journal-panel", classes="casys-panel") as journal:
            journal.border_title = "JOURNAL — why the agent did what it did"
            yield Static(id="journal-body")
        with Vertical(id="home-right"):
            with VerticalScroll(id="equity-panel", classes="casys-panel") as equity:
                equity.border_title = "EQUITY"
                yield Static(id="equity-body")
            with VerticalScroll(id="positions-panel", classes="casys-panel") as positions:
                positions.border_title = "POSITIONS"
                yield Static(id="positions-body")
            with VerticalScroll(id="fire-panel", classes="casys-panel") as fire:
                fire.border_title = "NEXT TO FIRE"
                yield Static(id="fire-body")

    def update_state(self, state: dict) -> None:
        now = datetime.now(UTC)
        self.query_one("#journal-body", Static).update(build_journal(state, now=now))
        self.query_one("#equity-body", Static).update(build_equity_summary(state))
        holdings = positions_by_pnl(state)
        positions_panel = self.query_one("#positions-panel", VerticalScroll)
        positions_panel.border_title = f"POSITIONS — {len(holdings)}" if holdings else "POSITIONS"
        self.query_one("#positions-body", Static).update(build_home_positions(state))
        self.query_one("#fire-body", Static).update(build_next_to_fire(state, now=now))

    def scroll_journal(self, delta: int) -> None:
        """j/k : défilement du journal."""
        scroll = self.query_one("#journal-panel", VerticalScroll)
        scroll.scroll_relative(y=delta * 3, animate=False)
