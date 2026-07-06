"""shell — chrome du cockpit casys : rail gauche, bande KPI, footer.

Remplace les 3 barres empilées (CockpitStatus + AttentionLine + CockpitNav) :
le chrome passe à l'horizontale, la hauteur revient au contenu.

    Screen = Horizontal( NavRail 17 cols │ Vertical( KpiBand · page · CockpitFooter ) )

Builders purs (état → Text) + widgets Textual minces. Le rail ne connaît pas
les pages : il reçoit une liste de ``NavItem`` (narrow contract, câblé par app).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from trader.interfaces.cockpit.derive import (
    CycleProgress,
    RailVitals,
    cycle_progress,
    equity_snapshot,
    llm_calls_label,
)
from trader.interfaces.cockpit import format as f
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

UTC = timezone.utc


@dataclass(frozen=True)
class NavItem:
    key: str
    number: int
    label: str  # "home", "portfolio", …


# ---------------------------------------------------------------------------
# Builders purs
# ---------------------------------------------------------------------------


def build_rail_brand() -> Text:
    text = Text()
    text.append(" ▘casys", style=f"bold {CASYS_ACCENT}")
    text.append("\n  cockpit", style=CASYS_FAINT)
    return text


def build_rail_nav(
    items: tuple[NavItem, ...],
    *,
    active_key: str,
    health_alerts: int = 0,
) -> Text:
    """Items de nav — actif : ▎ accent + bold ; badge ▲N sur l'item health."""
    text = Text()
    for index, item in enumerate(items):
        if index:
            text.append("\n")
        active = item.key == active_key
        marker = "▎" if active else " "
        style = f"bold {CASYS_ACCENT}" if active else CASYS_DIM
        text.append(marker, style=CASYS_ACCENT if active else "")
        text.append(f"{item.number} {item.label}", style=style)
        if item.key == "health" and health_alerts:
            text.append(f" ▲{health_alerts}", style=CASYS_WARNING)
    return text


def build_rail_vitals(vitals: RailVitals) -> Text:
    """Bloc bas du rail — un vital par ligne."""
    text = Text()

    if vitals.vital_status == "alive":
        style = f"bold {CASYS_WARNING}" if vitals.heartbeat_old else CASYS_SUCCESS
        text.append(" ● running", style=style)
    elif vitals.vital_status == "stopped":
        text.append(" ● stopped", style=f"bold {CASYS_ERROR}")
    else:
        text.append(" ○ never started", style=CASYS_DIM)
    text.append("\n")

    text.append(" pid ", style=CASYS_FAINT)
    text.append(str(vitals.pid) if vitals.pid else "—", style=CASYS_DIM)
    text.append("\n ")
    if vitals.dry_run:
        text.append(" DRY RUN ", style=CASYS_WARNING)
    else:
        text.append(" PAPER·LIVE ", style=CASYS_SUCCESS)
    text.append("\n")

    if vitals.kill_active:
        text.append(" !! KILL !! ", style="bold white on red")
    else:
        text.append(" kill ", style=CASYS_FAINT)
        text.append("○ off", style=CASYS_DIM)
    text.append("\n")
    if vitals.halted:
        text.append(f" HALT {vitals.halted}"[:16], style="bold white on red")
        text.append("\n")

    if vitals.session_venue:
        text.append(f" {vitals.session_venue} ●", style=CASYS_SUCCESS)
        text.append(" open", style=CASYS_FAINT)
    else:
        text.append(" markets ○", style=CASYS_DIM)
        text.append(" closed", style=CASYS_FAINT)
    text.append("\n")

    text.append(f" {vitals.clock_utc}", style=CASYS_FAINT)
    return text


def _kpi_cell(label: str, value: Text) -> Text:
    cell = Text()
    cell.append(label.upper(), style=CASYS_FAINT)
    cell.append("\n")
    cell.append_text(value)
    return cell


def build_kpi_band(state: dict, *, now: datetime) -> Table:
    """Bande KPI 6 cellules : EQUITY · CASH · UNREALIZED · CYCLE · NEXT WAKE · LLM."""
    snap = equity_snapshot(state)
    cycle: CycleProgress = cycle_progress(state)

    equity_value = Text()
    equity_value.append(f.fmt_money(snap.equity), style=f"bold {CASYS_FG}")
    if snap.equity > 0:
        style = CASYS_SUCCESS if snap.return_pct >= 0 else CASYS_ERROR
        equity_value.append(f" {f.fmt_pct(snap.return_pct, decimals=2)}", style=style)

    cash_value = Text()
    cash_value.append(f.fmt_money(snap.cash), style=CASYS_MUTED)
    cash_value.append(f" {snap.cash_pct:.0f}%", style=CASYS_DIM)

    unrealized_value = Text(
        f.fmt_signed_money(snap.unrealized),
        style=CASYS_SUCCESS if snap.unrealized >= 0 else CASYS_ERROR,
    )

    cycle_value = Text()
    if cycle.running:
        cycle_value.append(f.bar(cycle.done, cycle.total, width=5), style=CASYS_ACCENT)
        cycle_value.append(f" {cycle.done}/{cycle.total}", style=CASYS_MUTED)
    else:
        cycle_value.append("idle", style=CASYS_FAINT)

    wake_value = Text(
        f.countdown(state.get("default_next_wake"), now=now, prefix="in "),
        style=CASYS_ACCENT,
    )

    llm_value = Text(llm_calls_label(state), style=CASYS_MUTED)

    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in range(6):
        grid.add_column(ratio=1, no_wrap=True)
    grid.add_row(
        _kpi_cell("equity", equity_value),
        _kpi_cell("cash", cash_value),
        _kpi_cell("unrealized", unrealized_value),
        _kpi_cell("cycle", cycle_value),
        _kpi_cell("next wake", wake_value),
        _kpi_cell("llm", llm_value),
    )
    return grid


# Groupes contextuels du footer, par page.
_FOOTER_CONTEXT: dict[str, tuple[tuple[str, str], ...]] = {
    "home": (("j/k", "scroll journal"), ("enter", "inspect symbol")),
    "portfolio": (("o", "sort |pnl| / value / %"), ("enter", "inspect symbol")),
    "decisions": (("b/s/h", "filter action"), ("/", "regex"), ("enter", "expand")),
    "plans": (("enter", "inspect symbol"),),
    "health": (),
    "logs": (("c", "cycles"), ("f", "follow"), ("F", "classes"), ("/", "regex")),
    "universe": (("p", "pin"), ("b", "ban"), ("u", "undo override")),
    "settings": (("enter", "edit"), ("w", "write"), ("r", "revert")),
}


def build_footer(page_key: str) -> Text:
    """Footer 3 groupes : nav globale · touches de page · aide/quit."""
    text = Text(" ")

    def _key(key: str, label: str) -> None:
        text.append(key, style=f"bold {CASYS_ACCENT}")
        text.append(f" {label}", style=CASYS_DIM)

    def _sep_dot() -> None:
        text.append(" · ", style=CASYS_DIM)

    _key("1-8", "views")
    for key, label in _FOOTER_CONTEXT.get(page_key, ()):
        _sep_dot()
        _key(key, label)
    text.append("  │  ", style=CASYS_BORDER)
    _key("s", "start")
    _sep_dot()
    _key("x", "stop")
    _sep_dot()
    _key("k", "kill-switch")
    text.append("  │  ", style=CASYS_BORDER)
    _key("?", "help")
    _sep_dot()
    _key("q", "quit")
    return text


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class NavRail(Static):
    """Rail gauche 17 colonnes : brand · nav · (espace) · vitals."""

    DEFAULT_CSS = """
    NavRail {
        width: 17;
        min-width: 17;
        max-width: 17;
        height: 100%;
        background: $surface;
        border-right: solid #2c261f;
        layout: vertical;
        padding: 1 0;
    }
    NavRail #rail-brand { height: 3; }
    NavRail #rail-nav { height: auto; }
    NavRail #rail-spacer { height: 1fr; }
    NavRail #rail-vitals { height: auto; }
    """

    _items: tuple[NavItem, ...] = ()
    _active_key: str = "home"
    _health_alerts: int = 0

    def compose(self) -> ComposeResult:
        yield Static(build_rail_brand(), id="rail-brand")
        yield Static(id="rail-nav")
        yield Static(id="rail-spacer")
        yield Static(id="rail-vitals")

    def set_items(self, items: tuple[NavItem, ...]) -> None:
        self._items = items
        self._render_nav()

    def set_active(self, key: str) -> None:
        self._active_key = key
        self._render_nav()

    def update_state(self, state: dict, *, vital, kill_active: bool, now: datetime | None = None) -> None:
        from trader.interfaces.cockpit.derive import health_alert_count, rail_vitals

        now = now or datetime.now(UTC)
        self._health_alerts = health_alert_count(state, kill_active=kill_active, now=now)
        self._render_nav()
        self.query_one("#rail-vitals", Static).update(
            build_rail_vitals(rail_vitals(state, vital=vital, kill_active=kill_active, now=now))
        )

    def _render_nav(self) -> None:
        try:
            nav = self.query_one("#rail-nav", Static)
        except Exception:
            return
        nav.update(
            build_rail_nav(self._items, active_key=self._active_key, health_alerts=self._health_alerts)
        )


class KpiBand(Static):
    """Bande KPI d'une rangée, identique sur toutes les pages."""

    DEFAULT_CSS = """
    KpiBand {
        height: 3;
        padding: 0 2;
        border-bottom: solid #26211b;
    }
    """

    def update_state(self, state: dict, *, now: datetime | None = None) -> None:
        self.update(build_kpi_band(state, now=now or datetime.now(UTC)))


class CockpitFooter(Static):
    """Footer contextuel — remplace le Footer Textual standard."""

    DEFAULT_CSS = """
    CockpitFooter {
        dock: bottom;
        height: 1;
        background: $panel;
        border-top: solid #2c261f;
    }
    """

    def set_page(self, page_key: str) -> None:
        self.update(build_footer(page_key))
