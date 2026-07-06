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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.derive import (
    equity_snapshot,
    journal_entries,
    ledger_total,
    next_to_fire,
    positions_by_pnl,
)
from trader.interfaces.cockpit.pages._shared import ResizeRefresh, PANEL_CSS, build_equity_chart, rows_available
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


def build_equity_summary(state: dict, *, width: int = 40) -> RenderableType:
    snap = equity_snapshot(state)
    headline = Text()
    headline.append(f.fmt_money(snap.equity), style=f"bold {CASYS_FG}")
    if snap.equity > 0:
        style = CASYS_SUCCESS if snap.return_pct >= 0 else CASYS_ERROR
        headline.append(f" {f.fmt_pct(snap.return_pct, decimals=2)}", style=style)
    headline.append(f" · cash free {snap.cash_pct:.0f}%", style=CASYS_FAINT)
    return Group(
        headline, build_equity_chart(f.equity_curve(state), width=max(20, width), height=5)
    )


def build_home_positions(state: dict, *, limit: int = 7, bar_width: int = 8) -> RenderableType:
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
            Text(f.signed_bar(pnl, max_abs, width=bar_width), style=pnl_style),
            Text(f.fmt_signed(pnl), style=pnl_style),
        )
    parts: list[RenderableType] = [grid]
    if len(holdings) > limit:
        parts.append(Text(f"+ {len(holdings) - limit} more — press 2", style=CASYS_FAINT))
    return Group(*parts)


def build_next_to_fire(
    state: dict, *, now: datetime, limit: int = 6, width: int = 38
) -> RenderableType:
    """Le countdown a priorité : la condition est clippée à la place restante."""
    items = next_to_fire(state, now=now, limit=limit)
    if not items:
        return Text("nothing armed, nothing watched", style=f"italic {CASYS_FAINT}")
    detail_width = max(8, width - 9 - 5 - 4)  # sym(9) + countdown(5) + paddings
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)
    grid.add_column(no_wrap=True, overflow="ellipsis")
    grid.add_column(no_wrap=True, justify="right", width=5)
    for item in items:
        grid.add_row(
            Text(item.label, style=f"bold {CASYS_FG}" if item.kind != "wake" else CASYS_DIM),
            Text(f.clip(item.detail, limit=detail_width), style=CASYS_DIM),
            Text(item.countdown, style=CASYS_ACCENT),
        )
    return grid


_IDLE_PHASES = {"", "cycle_completed", "idle", "idle_waiting_for_wake", "sleeping", "halted"}


def build_agent_now(state: dict, *, now: datetime) -> Text | None:
    """Strip « AGENT NOW » — activité live. None quand l'agent dort (collapse).

    Rendu tant que le daemon travaille (phase active ou symbole en cours de
    décision). Data : daemon_status.phase/current_symbol/decisions_done/total.
    """
    status = f.safe_dict(state.get("daemon_status"))
    phase = str(status.get("phase") or "")
    current = str(status.get("current_symbol") or "")
    if phase in _IDLE_PHASES and not current:
        return None

    text = Text()
    text.append(" ▘ agent  ", style=f"bold {CASYS_ACCENT}")
    if current:
        text.append("deciding ", style=CASYS_DIM)
        text.append(f"{current} ▸", style=f"bold {CASYS_FG}")
    else:
        text.append("running", style=CASYS_SUCCESS)
    text.append("  │  ", style=CASYS_HAIRLINE)
    text.append("phase ", style=CASYS_DIM)
    text.append(phase or "—", style=CASYS_MUTED)

    done = status.get("decisions_done")
    total = status.get("symbols_total")
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        text.append("  │  ", style=CASYS_HAIRLINE)
        text.append("cycle ", style=CASYS_DIM)
        text.append(f"{done}/{total}", style=CASYS_MUTED)

    # dernière décision connue
    recent = f._safe_list_of_dicts(state.get("recent_decisions"))
    if recent:
        last = recent[-1]
        text.append("  │  ", style=CASYS_HAIRLINE)
        text.append("last ", style=CASYS_DIM)
        text.append(str(last.get("symbol") or "—"), style=CASYS_MUTED)
        text.append(f" {str(last.get('action') or '').upper()}", style=CASYS_DIM)

    wake = f.parse_ts(state.get("default_next_wake"))
    if wake is not None and wake > now:
        text.append("  │  ", style=CASYS_HAIRLINE)
        text.append("then → ", style=CASYS_DIM)
        text.append(wake.strftime("%H:%M UTC"), style=CASYS_ACCENT)
    return text


def build_today(state: dict, *, now: datetime, limit: int = 3) -> RenderableType:
    """TODAY — fills du jour (00:00 UTC) + résumé fills/decisions/LLM.

    Dérivé de recent_decisions (pas d'I/O) : approximation sur les 50 dernières.
    """
    floor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [
        r
        for r in f._safe_list_of_dicts(state.get("recent_decisions"))
        if (ts := f.parse_ts(r.get("cycle_ts") or r.get("ts"))) is not None and ts >= floor
    ]
    if not rows:
        return Text("nothing yet today", style=f"italic {CASYS_FAINT}")

    fills = [r for r in rows if r.get("executed") is True]
    n_llm = sum(1 for r in rows if r.get("model_called") is True)

    parts: list[RenderableType] = []
    if fills:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True, width=5)
        grid.add_column(no_wrap=True, width=9)
        grid.add_column(no_wrap=True)
        for r in fills[-limit:]:
            action = str(r.get("action") or "").upper()
            act_style = CASYS_SUCCESS if action == "BUY" else CASYS_ERROR if action == "SELL" else CASYS_DIM
            price = f._safe_float(r.get("price"), default=None)
            grid.add_row(
                Text(f.hhmm(r.get("cycle_ts") or r.get("ts")), style=CASYS_FAINT),
                Text(str(r.get("symbol") or "—"), style=f"bold {CASYS_FG}"),
                Text(f"{action} @ {f.fmt_compact(price, decimals=2)}", style=act_style),
            )
        parts.append(grid)

    summary = Text()
    summary.append(f"{len(fills)} fills", style=CASYS_SUCCESS if fills else CASYS_DIM)
    summary.append(" · ", style=CASYS_FAINT)
    summary.append(f"{len(rows)} decisions", style=CASYS_DIM)
    summary.append(" · ", style=CASYS_FAINT)
    summary.append(f"{n_llm} LLM calls", style=CASYS_DIM)
    parts.append(summary)
    return Group(*parts)


class AgentNowStrip(Static):
    """Strip d'activité live entre KPI band et corps — masqué quand l'agent dort."""

    DEFAULT_CSS = """
    AgentNowStrip {
        height: 1;
        display: none;
        background: #1a1611;
        margin-bottom: 1;
    }
    AgentNowStrip.visible { display: block; }
    """

    def update_state(self, state: dict, *, now: datetime) -> None:
        strip = build_agent_now(state, now=now)
        self.set_class(strip is not None, "visible")
        if strip is not None:
            self.update(strip)


class HomePage(ResizeRefresh, Static):
    """Page 1 — Decision Journal (Rév. 3 : AGENT NOW strip + TODAY)."""

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    HomePage {
        layout: vertical;
        height: 100%;
        padding: 1 2 0 2;
    }
    HomePage #home-body {
        layout: horizontal;
        height: 1fr;
    }
    HomePage #journal-panel {
        width: 1fr;
        height: 100%;
        margin-right: 1;
    }
    HomePage #journal-scroll { height: 100%; }
    HomePage #home-right {
        height: 100%;
    }
    HomePage #equity-panel { height: 9; margin-bottom: 1; }
    HomePage #positions-panel { height: 1fr; margin-bottom: 1; }
    HomePage #today-panel { height: auto; max-height: 30%; min-height: 4; margin-bottom: 1; }
    HomePage #fire-panel { height: 1fr; }
    HomePage .casys-panel Static { height: auto; }
    """
    )

    def compose(self) -> ComposeResult:
        yield AgentNowStrip(id="agent-now")
        with Horizontal(id="home-body"):
            with VerticalScroll(id="journal-panel", classes="casys-panel") as journal:
                journal.border_title = "JOURNAL — why the agent did what it did"
                yield Static(id="journal-body")
            with Vertical(id="home-right", classes="right-col"):
                with VerticalScroll(id="equity-panel", classes="casys-panel") as equity:
                    equity.border_title = "EQUITY"
                    yield Static(id="equity-body")
                with VerticalScroll(id="positions-panel", classes="casys-panel") as positions:
                    positions.border_title = "POSITIONS"
                    yield Static(id="positions-body")
                with VerticalScroll(id="today-panel", classes="casys-panel") as today:
                    today.border_title = "TODAY"
                    yield Static(id="today-body")
                with VerticalScroll(id="fire-panel", classes="casys-panel") as fire:
                    fire.border_title = "NEXT TO FIRE"
                    yield Static(id="fire-body")

    def update_state(self, state: dict) -> None:
        self._last_state = state
        now = datetime.now(UTC)

        self.query_one("#agent-now", AgentNowStrip).update_state(state, now=now)
        self.query_one("#today-body", Static).update(build_today(state, now=now))

        # — Journal : généreux, une entrée ≈ 4-6 lignes, ne pas plafonner à 8
        journal_panel = self.query_one("#journal-panel", VerticalScroll)
        journal_limit = max(8, rows_available(journal_panel) // 4)
        self.query_one("#journal-body", Static).update(
            build_journal(state, now=now, limit=journal_limit)
        )

        equity_panel = self.query_one("#equity-panel", VerticalScroll)
        chart_width = max(20, (equity_panel.size.width or 44) - 4)
        self.query_one("#equity-body", Static).update(
            build_equity_summary(state, width=chart_width)
        )

        holdings = positions_by_pnl(state)
        positions_panel = self.query_one("#positions-panel", VerticalScroll)
        positions_panel.border_title = f"POSITIONS — {len(holdings)}" if holdings else "POSITIONS"
        # sym(9) + side(1) + montant(~7) + paddings(~7) — le reste pour la barre
        bar_width = max(4, min(8, (positions_panel.size.width or 44) - 24))
        pos_limit = rows_available(positions_panel, reserved=1, minimum=4)
        self.query_one("#positions-body", Static).update(
            build_home_positions(state, limit=pos_limit, bar_width=bar_width)
        )

        fire_panel = self.query_one("#fire-panel", VerticalScroll)
        fire_limit = rows_available(fire_panel, reserved=0, minimum=3)
        self.query_one("#fire-body", Static).update(
            build_next_to_fire(
                state,
                now=now,
                limit=fire_limit,
                width=max(24, (fire_panel.size.width or 42) - 4),
            )
        )

    def scroll_journal(self, delta: int) -> None:
        """j/k : défilement du journal."""
        scroll = self.query_one("#journal-panel", VerticalScroll)
        scroll.scroll_relative(y=delta * 3, animate=False)
