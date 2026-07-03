"""cockpit — Salle de contrôle du daemon casys-trader (Textual).

Shell cockpit navigable : home courte, pages de détail (portefeuille, décisions,
plans, observabilité, logs live) + supervision du daemon.

Layout mission-control navigable (priorité haute → bas) :
    STATUT (dock:top, h=2)
    NAV (#cockpit-nav, h=3)
    CONTENT SWITCHER (#page-switcher, h=1fr)
    Footer (dock:bottom, h=1)

Pattern superviseur : le daemon reste un process indépendant qui survit à
la fermeture du cockpit. Le cockpit peut le démarrer, l'arrêter et toggler
le kill-switch.

Au lancement, si aucun daemon n'est vivant (never_started ou stopped), le
cockpit propose de démarrer le moteur live (modal ConfirmStart). Safe default :
rien n'est lancé sans confirmation explicite — « Plus tard » / Échap n'agit pas.

Écriture autorisée (UNIQUEMENT ces fichiers) :
    state/daemon.pid          — PID du daemon au lancement
    state/daemon_console.log  — stdout/stderr du daemon (append)
    KILL                      — fichier kill-switch (toggle)

Usage :
    uv run python -m trader.cockpit
    make watch

Raccourcis :
    q / Ctrl+C  Quitter — si le daemon est vivant, modal ConfirmQuit avertit
                que le moteur live sera aussi arrêté (« Arrêter et quitter » /
                « Annuler »). Aucun daemon vivant → quit direct.
    s         Démarrer le daemon (anti-double-lancement)
    X         Arrêter le daemon (modal de confirmation → SIGINT)
    k         Toggle kill-switch (modal de confirmation)
    c         Toggle l'affichage des events cycle_started/cycle_completed
    f         Pause/reprise de l'auto-scroll du panneau logs
    l         Aller/retour page Logs
    d         Dark/Light (thèmes casys-salmon / casys-ink)
    Tab/→     Vue suivante
    ←/Shift+Tab Vue précédente
    1..6      Accès direct aux pages
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import RenderableType
from rich.panel import Panel
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, ContentSwitcher, Footer, Label, RichLog, Static

from trader.cockpit.supervisor import daemon_vital_state

from trader.cockpit.events import (
    EventClass,
    format_event_line,
    read_new_lines,
)
from trader.cockpit import overview as _cockpit_overview
from trader.ui.palette import PALETTE_DARK, PALETTE_LIGHT, Palette
from trader.read_models.runtime_state import (
    _enrich_decisions_with_data_source,
    _safe_float,
    _safe_list_of_dicts,
    load_runtime_state,
)
from trader.ui.rich_panels import (
    _build_armed_plans_panel,
    _build_attribution_panel,
    _build_data_health_panel,
    _build_decisions_table,
    _build_equity_panel,
    _build_exit_plans_enriched,
    _build_kpi_compact,
    _build_learnings_panel,
    _build_llm_activity_panel,
    _build_positions_panel,
    _build_watches_panel,
    _format_datetime,
    build_closed_trades_table,
    build_universe_panel,
)

# ---------------------------------------------------------------------------
# Thèmes Textual
# ---------------------------------------------------------------------------

UTC = timezone.utc

_THEME_SALMON = Theme(
    name="casys-salmon",
    dark=False,
    primary="#0D7680",
    secondary="#6B6057",
    warning="#C47B00",
    error="#B52A2A",
    success="#1A6B2F",
    accent="#0D7680",
    foreground="#33302E",
    background="#FFF1E5",
    surface="#FFF8F2",
    panel="#FDEEDE",
)

_THEME_INK = Theme(
    name="casys-ink",
    dark=True,
    primary="#0178D4",
    secondary="#888888",
    warning="#ffcc00",
    error="#e05252",
    success="#4caf50",
    accent="#0178D4",
    foreground="#e0e0e0",
    background="#1a1a2e",
    surface="#16213e",
    panel="#0f3460",
)

# Mapping nom de thème → palette Rich
_THEME_PALETTE: dict[str, Palette] = {
    "casys-salmon": PALETTE_LIGHT,
    "casys-ink": PALETTE_DARK,
}

OverviewPane = _cockpit_overview.OverviewPane
_build_attention_line = _cockpit_overview._build_attention_line
_build_overview_panel = _cockpit_overview._build_overview_panel

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR = _ROOT / "state"
_CONFIG_DIR = str(_ROOT)
_EVENTS_FILE = _STATE_DIR / "events.jsonl"
_KILL_FILE = _ROOT / "KILL"

# Buffer maximum pour le panneau events
_MAX_EVENT_LINES = 500


@dataclass(frozen=True)
class CockpitPage:
    key: str
    title: str
    widget_id: str


_PAGES: tuple[CockpitPage, ...] = (
    CockpitPage("home", "Accueil", "overview-page"),
    CockpitPage("portfolio", "Portefeuille", "portfolio-page"),
    CockpitPage("decisions", "Décisions", "decisions-page"),
    CockpitPage("plans", "Plans", "plans-page"),
    CockpitPage("observability", "Observabilité", "observability-page"),
    CockpitPage("logs", "Logs", "logs-page"),
)
_PAGE_BY_KEY = {page.key: page for page in _PAGES}
_PAGE_KEYS = tuple(page.key for page in _PAGES)

# ---------------------------------------------------------------------------
# Styles events par EventClass — construits depuis une palette
# ---------------------------------------------------------------------------


def _event_styles_for_palette(palette: Palette) -> dict[EventClass, str]:
    """Construit le mapping EventClass→style depuis une palette."""
    return {
        EventClass.DECISION_EXECUTED: palette["event_decision_exec"],
        EventClass.RISK_REJECT: palette["event_risk_reject"],
        EventClass.STALE: palette["event_stale"],
        EventClass.HOLD: palette["event_hold"],
        EventClass.WATCH: palette["event_watch"],
        EventClass.LEARNING: palette["event_learning"],
        EventClass.CYCLE: palette["event_cycle"],
        EventClass.ERROR: palette["event_error"],
        EventClass.OTHER: palette["event_other"],
    }


# ---------------------------------------------------------------------------
# Widget : barre de statut 1 ligne (dock:top)
# ---------------------------------------------------------------------------


class CockpitStatus(Static):
    """Barre de statut distillée — un seul endroit pour l'état runtime."""

    DEFAULT_CSS = """
    CockpitStatus {
        height: 2;
        background: $panel;
        border-bottom: solid $primary;
        padding: 0 1;
    }
    """

    def update_state(
        self, state: dict, kill_active: bool, *, palette: Palette = PALETTE_DARK
    ) -> None:
        from trader.cockpit.home import build_status_line

        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        width = self.size.width or 200
        self.update(
            build_status_line(
                state,
                kill_active=kill_active,
                palette=palette,
                width=width,
                now=datetime.now(UTC),
                vital=vital,
            )
        )

    def on_resize(self) -> None:
        app = self.app
        state = getattr(app, "_last_state", None)
        if state is not None:
            self.update_state(
                state,
                getattr(app, "_last_kill_active", False),
                palette=app._current_palette(),  # type: ignore[attr-defined]
            )


class AttentionStrip(Static):
    """Ligne de triage entre le statut global et l'espace de travail."""

    DEFAULT_CSS = """
    AttentionStrip {
        height: 3;
        background: $surface;
        border-bottom: solid $primary;
        padding: 0 1;
    }
    """

    def update_state(
        self, state: dict, kill_active: bool, *, palette: Palette = PALETTE_DARK
    ) -> None:
        self.update(_build_attention_line(state, kill_active=kill_active, palette=palette))


class CockpitNav(Static):
    """Navigation compacte entre les vues du cockpit."""

    DEFAULT_CSS = """
    CockpitNav {
        height: 3;
        background: $panel;
        border-bottom: solid $primary;
        padding: 0 1;
    }
    """

    def update_page(self, active_key: str, *, palette: Palette = PALETTE_LIGHT) -> None:
        text = Text("  ")
        text.append("Tab/←/→ ", style=palette["dim"])
        text.append("vue", style=palette["dim"])
        text.append("   ")
        for index, page in enumerate(_PAGES, start=1):
            if index > 1:
                text.append("  ")
            label = f"{index} {page.title}"
            if page.key == active_key:
                text.append(f"[{label}]", style=f"bold {palette['status_accent']}")
            else:
                text.append(label, style=palette["dim"])
        self.update(text)


# ---------------------------------------------------------------------------
# Panes du nouveau layout
# ---------------------------------------------------------------------------


class PositionsPlansPane(Static):
    """Bande haute gauche (40%) : positions+PnL net PUIS plans de sortie enrichis."""

    DEFAULT_CSS = """
    PositionsPlansPane {
        width: 100%;
        height: 1fr;
        overflow-y: auto;
    }
    PositionsPlansPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield Static(id="positions-panel")
        yield Static(id="exit-plans-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        portfolio = (
            state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        )
        holdings = _safe_list_of_dicts(portfolio.get("holdings"))
        trade_plans = (
            state.get("trade_plans")
            if isinstance(state.get("trade_plans"), list)
            else []
        )

        self.query_one("#positions-panel", Static).update(
            _build_positions_panel(holdings, palette=palette)
        )
        self.query_one("#exit-plans-panel", Static).update(
            _build_exit_plans_enriched(trade_plans, palette=palette)
        )


class ArmedPlansPane(Static):
    """Bande haute droite (60%) : plans armés."""

    DEFAULT_CSS = """
    ArmedPlansPane {
        width: 100%;
        height: 1fr;
        overflow-y: auto;
    }
    ArmedPlansPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield Static(id="armed-plans-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        armed_plans = (
            state.get("armed_plans")
            if isinstance(state.get("armed_plans"), list)
            else []
        )
        self.query_one("#armed-plans-panel", Static).update(
            _build_armed_plans_panel(armed_plans, palette=palette)
        )


class DecisionsPane(Static):
    """Corps gauche (30%) : table décisions + attribution."""

    DEFAULT_CSS = """
    DecisionsPane {
        width: 100%;
        height: 1fr;
        overflow-y: auto;
    }
    DecisionsPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield Static(id="decisions-table")
        yield Static(id="attribution-panel")
        yield Static(id="data-health-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        attribution = (
            state.get("attribution")
            if isinstance(state.get("attribution"), dict)
            else {}
        )
        decisions_raw = _safe_list_of_dicts(state.get("decisions"))
        recent_decisions = (
            state.get("recent_decisions")
            if isinstance(state.get("recent_decisions"), list)
            else []
        )
        decisions = _enrich_decisions_with_data_source(decisions_raw, recent_decisions)
        stale_streaks = (
            state.get("stale_streaks")
            if isinstance(state.get("stale_streaks"), dict)
            else {}
        )

        self.query_one("#decisions-table", Static).update(
            _build_decisions_table(decisions, palette=palette)
        )
        self.query_one("#attribution-panel", Static).update(
            _build_attribution_panel(attribution, palette=palette)
        )
        self.query_one("#data-health-panel", Static).update(
            _build_data_health_panel(recent_decisions, stale_streaks, palette=palette)
        )


class EquityTradesPane(Static):
    """Corps centre (40%) : KPI compact + courbe équité + trades clôturés avec net P&L."""

    DEFAULT_CSS = """
    EquityTradesPane {
        width: 100%;
        height: 1fr;
        overflow-y: auto;
    }
    EquityTradesPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield Static(id="kpi-compact")
        yield Static(id="equity-panel")
        yield Static(id="trades-panel")
        yield Static(id="llm-activity-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        equity_curve = [
            v
            for v in (
                _safe_float(x, default=None)
                for x in (state.get("equity_curve") or [])
            )
            if v is not None
        ]
        recent_trips = (
            state.get("recent_trips") if isinstance(state.get("recent_trips"), list) else []
        )
        daemon_status = (
            state.get("daemon_status")
            if isinstance(state.get("daemon_status"), dict)
            else {}
        )
        learnings_pending = state.get("learnings_pending_count") or 0
        consolidation_status = state.get("consolidation_status")
        company_map = state.get("company_map") if isinstance(state.get("company_map"), dict) else {}

        self.query_one("#kpi-compact", Static).update(
            _build_kpi_compact(kpis, equity_curve, palette=palette)
        )
        self.query_one("#equity-panel", Static).update(
            _build_equity_panel(equity_curve, palette=palette)
        )
        self.query_one("#trades-panel", Static).update(
            build_closed_trades_table(recent_trips, company_map, palette=palette)
        )
        self.query_one("#llm-activity-panel", Static).update(
            _build_llm_activity_panel(
                daemon_status, learnings_pending, consolidation_status, palette=palette
            )
        )


class UniversePane(Static):
    """Corps droit (30%) : univers, veilles et derniers learnings."""

    DEFAULT_CSS = """
    UniversePane {
        width: 100%;
        height: 1fr;
        overflow-y: auto;
    }
    UniversePane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield Static(id="universe-panel")
        yield Static(id="watches-panel")
        yield Static(id="learnings-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        universe_symbols = (
            state.get("universe_symbols")
            if isinstance(state.get("universe_symbols"), list)
            else []
        )
        venue_state = (
            state.get("venue_state") if isinstance(state.get("venue_state"), dict) else {}
        )
        open_venues_list = (
            state.get("open_venues_list")
            if isinstance(state.get("open_venues_list"), list)
            else []
        )
        company_map = (
            state.get("company_map") if isinstance(state.get("company_map"), dict) else {}
        )
        indicator_watches = (
            state.get("indicator_watches")
            if isinstance(state.get("indicator_watches"), list)
            else []
        )
        learnings = _safe_list_of_dicts(state.get("learnings"))

        self.query_one("#universe-panel", Static).update(
            build_universe_panel(
                universe_symbols, venue_state, open_venues_list, company_map, palette=palette
            )
        )
        self.query_one("#watches-panel", Static).update(
            _build_watches_panel(indicator_watches, palette=palette)
        )
        learnings_panel = _build_learnings_panel(learnings, palette=palette)
        self.query_one("#learnings-panel", Static).update(
            learnings_panel
            if learnings_panel is not None
            else Panel(
                Text("aucun learning récent", style=palette["dim"]),
                title="[bold]Derniers apprentissages[/bold]",
                border_style=palette["border_learnings"],
                expand=True,
            )
        )


# ---------------------------------------------------------------------------
# Builder pur : table trades avec net P&L et noms de sociétés
# ---------------------------------------------------------------------------


def _build_trades_with_pnl(
    fills: list[dict],
    pnl_by_fill: list[float | None],
    company_map: dict[str, str],
    *,
    limit: int = 20,
    palette: Palette = PALETTE_DARK,
) -> RenderableType:
    """Table des derniers trades clôturés avec net P&L calculé et noms de sociétés.

    PURE — ne lit aucun fichier. Les fills BUY affichent — en P&L.
    Les fills SELL affichent le bénéfice net réalisé coloré +/−.
    Raison de sortie : non disponible dans les fills → —.
    """
    from rich.table import Table
    from rich.text import Text
    from trader.ui.rich_panels import _fmt_symbol_short

    table = Table(title="Trades clôturés", show_lines=False, expand=True)
    table.add_column("Heure", no_wrap=True, style=palette["dim"])
    table.add_column("Société · Ticker", style="bold")
    table.add_column("Sens")
    table.add_column("Qté", justify="right")
    table.add_column("Prix", justify="right")
    table.add_column("Net P&L", justify="right")

    # Aligner fills et pnl_by_fill (même longueur garantie par compute_realized_pnl_by_fill)
    paired = list(zip(fills, pnl_by_fill)) if pnl_by_fill else [(f, None) for f in fills]
    recent = paired[-limit:] if len(paired) > limit else paired

    for fill, net_pnl in reversed(recent):
        ts_raw = str(fill.get("ts") or "")
        try:
            candidate = f"{ts_raw[:-1]}+00:00" if ts_raw.endswith("Z") else ts_raw
            dt = datetime.fromisoformat(candidate)
            ts_str = dt.astimezone(UTC).strftime("%H:%M:%S")
        except (ValueError, AttributeError):
            ts_str = ts_raw[:8] if ts_raw else "—"

        symbol = str(fill.get("symbol") or "?")
        label = _fmt_symbol_short(symbol, company_map)
        side = str(fill.get("side") or "")
        side_style = (
            palette["action_buy"] if side == "BUY" else (
                palette["action_sell"] if side == "SELL" else palette["dim"]
            )
        )
        qty_val = _safe_float(fill.get("quantity"), default=0.0) or 0.0
        price_val = _safe_float(fill.get("price"), default=None)
        price_str = f"{price_val:,.4f}" if price_val is not None else "—"

        # Net P&L : None (BUY) → —, float → coloré
        if net_pnl is None:
            pnl_cell = Text("—", style=palette["dim"])
        else:
            pnl_style = palette["pnl_positive"] if net_pnl >= 0 else palette["pnl_negative"]
            pnl_cell = Text(f"{net_pnl:+,.2f}", style=pnl_style)

        table.add_row(
            ts_str,
            label,
            Text(side, style=side_style),
            f"{qty_val:,.4f}",
            price_str,
            pnl_cell,
        )

    if not recent:
        table.add_row("—", "—", "—", "—", "—", "—")

    return table


# ---------------------------------------------------------------------------
# Widget : panneau logs live (dock:bottom)
# ---------------------------------------------------------------------------


class LogsPane(Static):
    """Panneau bas : tail live d'events.jsonl avec auto-scroll et filtres.

    Remplace l'ancien EventsPane intégré dans RightPane. Garde exactement
    les mêmes comportements : toggle cycles (c), toggle scroll (f), toggle
    visibilité (l), backlog initial différé, poll périodique.
    """

    _offset: int = 0
    _show_cycles: bool = True
    _auto_scroll: bool = True
    _last_file_status: str = "ok"
    _current_palette: Palette = PALETTE_LIGHT
    _backlog_loaded: bool = False

    DEFAULT_CSS = """
    LogsPane {
        height: 100%;
        border: solid $primary;
    }
    LogsPane RichLog {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]Logs live[/bold]  [dim]c:cycles  f:scroll  l:toggle pane[/dim]",
            id="logs-title",
        )
        yield RichLog(
            id="events-log",
            highlight=False,
            markup=False,
            max_lines=_MAX_EVENT_LINES,
        )

    def on_mount(self) -> None:
        """Diffère le chargement du backlog jusqu'après le premier layout."""
        self.call_after_refresh(self._load_initial_backlog)

    def _load_initial_backlog(self) -> None:
        if self._backlog_loaded:
            return
        self._backlog_loaded = True
        try:
            events_path: Path = self.app._events_file  # type: ignore[attr-defined]
        except AttributeError:
            import trader.cockpit as _mod
            events_path = _mod._EVENTS_FILE
        self.poll_events(events_path)

    def toggle_cycles(self) -> None:
        self._show_cycles = not self._show_cycles
        status = "visibles" if self._show_cycles else "masqués"
        log: RichLog = self.query_one("#events-log", RichLog)
        log.write(Text(f"[cycles {status}]", style="dim italic"))

    def toggle_scroll(self) -> None:
        self._auto_scroll = not self._auto_scroll
        log: RichLog = self.query_one("#events-log", RichLog)
        status = "repris" if self._auto_scroll else "pausé"
        log.write(Text(f"[scroll {status}]", style="dim italic"))

    def poll_events(self, events_path: Path) -> None:
        """Lit les nouvelles lignes et les ajoute au RichLog."""
        log: RichLog = self.query_one("#events-log", RichLog)

        if not events_path.exists():
            if self._last_file_status != "absent":
                self._last_file_status = "absent"
                log.write(
                    Text(
                        f"[events] {events_path.name} absent — en attente…", style="dim"
                    )
                )
            return

        if self._last_file_status == "absent":
            self._last_file_status = "ok"
            log.write(Text(f"[events] {events_path.name} disponible", style="dim"))

        new_dicts, new_offset = read_new_lines(events_path, self._offset)
        self._offset = new_offset

        if not new_dicts:
            return

        event_styles = _event_styles_for_palette(self._current_palette)
        for ev_dict in new_dicts:
            ev_line = format_event_line(ev_dict)
            if ev_line.markup_class == EventClass.CYCLE and not self._show_cycles:
                continue
            style = event_styles.get(ev_line.markup_class, "")
            log.write(Text(ev_line.text, style=style))

        if self._auto_scroll:
            log.scroll_end(animate=False)


# Alias de rétrocompatibilité — anciens tests qui importent EventsPane
EventsPane = LogsPane


# ---------------------------------------------------------------------------
# Modals de confirmation
# ---------------------------------------------------------------------------


def _confirm_modal_css(screen_name: str, *, border: str, width: int) -> str:
    return f"""
    {screen_name} {{
        align: center middle;
    }}
    {screen_name} Vertical {{
        background: $surface;
        border: solid {border};
        padding: 1 2;
        width: {width};
        height: auto;
    }}
    {screen_name} Horizontal {{
        height: auto;
        align: center middle;
        margin-top: 1;
    }}
    {screen_name} Button {{
        margin: 0 1;
    }}
    """


def _confirmation_buttons(
    confirm_label: str,
    *,
    confirm_id: str,
    confirm_variant: str,
    cancel_label: str,
    cancel_id: str,
) -> tuple[Button, Button]:
    return (
        Button(confirm_label, id=confirm_id, variant=confirm_variant),
        Button(cancel_label, id=cancel_id, variant="default"),
    )


class _ConfirmModal(ModalScreen[bool]):
    _confirm_button_id = ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == self._confirm_button_id)


class ConfirmStop(_ConfirmModal):
    """Modal de confirmation pour l'arrêt du daemon (binding X)."""

    _confirm_button_id = "confirm-stop-yes"
    DEFAULT_CSS = _confirm_modal_css("ConfirmStop", border="$error", width=50)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Arrêter le daemon ?\n(SIGINT — arrêt propre)")
            with Horizontal():
                yield from _confirmation_buttons(
                    "Oui",
                    confirm_id="confirm-stop-yes",
                    confirm_variant="error",
                    cancel_label="Non",
                    cancel_id="confirm-stop-no",
                )


class ConfirmQuit(_ConfirmModal):
    """Modal de confirmation à la sortie quand un daemon est vivant."""

    _confirm_button_id = "confirm-quit-stop"
    BINDINGS = [
        Binding("escape", "cancel", "Annuler", show=False),
    ]
    DEFAULT_CSS = _confirm_modal_css("ConfirmQuit", border="$warning", width=60)

    def __init__(self, pid: int | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._pid = pid

    def compose(self) -> ComposeResult:
        pid_info = f" (PID {self._pid})" if self._pid else ""
        with Vertical():
            yield Label(
                f"Quitter — le moteur live sera aussi arrêté{pid_info}."
            )
            with Horizontal():
                yield from _confirmation_buttons(
                    "Arrêter et quitter",
                    confirm_id="confirm-quit-stop",
                    confirm_variant="warning",
                    cancel_label="Annuler",
                    cancel_id="confirm-quit-cancel",
                )

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmStart(_ConfirmModal):
    """Modal proposé au lancement du cockpit quand aucun daemon n'est vivant."""

    _confirm_button_id = "confirm-start-yes"
    BINDINGS = [
        Binding("escape", "cancel", "Plus tard", show=False),
    ]
    DEFAULT_CSS = _confirm_modal_css("ConfirmStart", border="$primary", width=60)

    def __init__(self, *, never_started: bool, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._never_started = never_started

    def compose(self) -> ComposeResult:
        intro = (
            "Aucun daemon en cours."
            if self._never_started
            else "Le daemon est arrêté."
        )
        with Vertical():
            yield Label(
                f"{intro}\nDémarrer le moteur live (PAPER réel — exécute les ordres simulés) ?"
            )
            with Horizontal():
                yield from _confirmation_buttons(
                    "Démarrer",
                    confirm_id="confirm-start-yes",
                    confirm_variant="primary",
                    cancel_label="Plus tard",
                    cancel_id="confirm-start-no",
                )

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmKill(_ConfirmModal):
    """Modal de confirmation pour le toggle kill-switch (binding k)."""

    _confirm_button_id = "confirm-kill-yes"
    DEFAULT_CSS = _confirm_modal_css("ConfirmKill", border="$warning", width=50)

    def __init__(self, kill_active: bool, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._kill_active = kill_active

    def compose(self) -> ComposeResult:
        action = "Retirer" if self._kill_active else "Activer"
        with Vertical():
            yield Label(f"{action} le kill-switch ?\n(bloque/débloque tous les ordres)")
            with Horizontal():
                yield from _confirmation_buttons(
                    "Oui",
                    confirm_id="confirm-kill-yes",
                    confirm_variant="warning",
                    cancel_label="Non",
                    cancel_id="confirm-kill-no",
                )


# ---------------------------------------------------------------------------
# App principale
# ---------------------------------------------------------------------------


class CockpitApp(App):
    """Cockpit console unifié — dashboard F-pattern + logs live."""

    TITLE = "casys-trader — cockpit"

    # CSS TCSS décrivant le shell navigable
    CSS = """
    Screen {
        layout: vertical;
    }
    #workspace {
        height: 1fr;
        layout: vertical;
    }
    #page-switcher {
        width: 100%;
        height: 100%;
    }
    #overview-page {
        width: 100%;
        height: 100%;
    }
    #portfolio-page {
        width: 100%;
        height: 100%;
        layout: horizontal;
    }
    #portfolio-page PositionsPlansPane {
        width: 45%;
        height: 100%;
        layout: vertical;
        border-right: solid $primary;
    }
    #portfolio-page EquityTradesPane {
        width: 55%;
        height: 100%;
    }
    #decisions-page,
    #plans-page,
    #observability-page,
    #logs-page {
        width: 100%;
        height: 100%;
        layout: vertical;
    }
    #decisions-page DecisionsPane,
    #plans-page ArmedPlansPane,
    #observability-page UniversePane,
    #logs-page LogsPane {
        height: 100%;
        width: 100%;
    }
    .cockpit-page {
        width: 100%;
        height: 100%;
    }
    """

    BINDINGS = [
        Binding("q", "quit_confirm", "Quitter"),
        # Ctrl+C → même comportement que q (avertissement si daemon vivant)
        Binding("ctrl+c", "quit_confirm", "Quitter", show=False, priority=True),
        Binding("s", "start_daemon", "Démarrer daemon"),
        Binding("X", "stop_daemon_confirm", "Maj+X — Arrêter daemon"),
        Binding("k", "toggle_kill", "Kill-switch"),
        Binding("c", "toggle_cycles", "Toggle cycles"),
        Binding("f", "toggle_scroll", "Pause scroll"),
        Binding("l", "toggle_logs", "Logs"),
        Binding("d", "toggle_theme", "Dark/Light"),
        Binding("tab", "next_page", "Vue suivante", show=False, priority=True),
        Binding("right", "next_page", "Vue suivante", show=False, priority=True),
        Binding("left", "previous_page", "Vue précédente", show=False, priority=True),
        Binding("shift+tab", "previous_page", "Vue précédente", show=False, priority=True),
        Binding("1", "show_page('home')", "Accueil", show=False),
        Binding("2", "show_page('portfolio')", "Portefeuille", show=False),
        Binding("3", "show_page('decisions')", "Décisions", show=False),
        Binding("4", "show_page('plans')", "Plans", show=False),
        Binding("5", "show_page('observability')", "Observabilité", show=False),
        Binding("6", "show_page('logs')", "Logs", show=False),
    ]

    _last_state: dict | None = None
    _last_kill_active: bool = False
    _active_page_key: str = "home"
    _previous_non_logs_page: str = "home"

    def compose(self) -> ComposeResult:
        """Structure : statut → navigation → page active → footer."""
        yield CockpitStatus(id="cockpit-status")
        yield CockpitNav(id="cockpit-nav")
        with Vertical(id="workspace"):
            with ContentSwitcher(id="page-switcher", initial="overview-page"):
                yield OverviewPane(id="overview-page", classes="cockpit-page")
                with Horizontal(id="portfolio-page", classes="cockpit-page"):
                    yield PositionsPlansPane(id="positions-plans-pane")
                    yield EquityTradesPane(id="equity-trades-pane")
                with Vertical(id="decisions-page", classes="cockpit-page"):
                    yield DecisionsPane(id="decisions-pane")
                with Vertical(id="plans-page", classes="cockpit-page"):
                    yield ArmedPlansPane(id="armed-plans-pane")
                with Vertical(id="observability-page", classes="cockpit-page"):
                    yield UniversePane(id="universe-pane")
                with Vertical(id="logs-page", classes="cockpit-page"):
                    yield LogsPane(id="logs-pane")
        yield Footer()

    def on_mount(self) -> None:
        # Thèmes custom
        self.register_theme(_THEME_SALMON)
        self.register_theme(_THEME_INK)
        self.theme = "casys-salmon"
        self._set_active_page("home")

        # Expose le chemin events sur self pour que LogsPane._load_initial_backlog
        # puisse le résoudre même quand _EVENTS_FILE est monkeypatché en test.
        self._events_file = _EVENTS_FILE

        # Polling état toutes les 2 s (via worker thread — I/O hors UI loop)
        self.set_interval(2.0, self._schedule_refresh_state)
        # Polling events toutes les 1 s
        self.set_interval(1.0, self._poll_events)
        # Charge l'état immédiatement
        self._schedule_refresh_state()
        # Propose de démarrer le daemon s'il n'est pas vivant
        self.call_after_refresh(self._maybe_propose_start)

    def _maybe_propose_start(self) -> None:
        """Propose de lancer le daemon au démarrage si aucun n'est vivant."""
        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        if vital.status == "alive":
            return

        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self.action_start_daemon()

        self.push_screen(
            ConfirmStart(never_started=vital.status == "never_started"),
            _on_confirm,
        )

    def _set_active_page(self, page_key: str) -> None:
        """Affiche une page et masque les autres."""
        if page_key not in _PAGE_BY_KEY:
            return
        self._active_page_key = page_key
        if page_key != "logs":
            self._previous_non_logs_page = page_key

        try:
            switcher = self.query_one("#page-switcher", ContentSwitcher)
            switcher.current = _PAGE_BY_KEY[page_key].widget_id
        except Exception:
            pass
        try:
            nav = self.query_one("#cockpit-nav", CockpitNav)
            nav.update_page(page_key, palette=self._current_palette())
        except Exception:
            pass

    def action_next_page(self) -> None:
        index = _PAGE_KEYS.index(self._active_page_key)
        self._set_active_page(_PAGE_KEYS[(index + 1) % len(_PAGE_KEYS)])

    def action_previous_page(self) -> None:
        index = _PAGE_KEYS.index(self._active_page_key)
        self._set_active_page(_PAGE_KEYS[(index - 1) % len(_PAGE_KEYS)])

    def action_show_page(self, page_key: str) -> None:
        self._set_active_page(page_key)

    def _current_palette(self) -> Palette:
        """Retourne la palette Rich correspondant au thème actif."""
        return _THEME_PALETTE.get(self.theme, PALETTE_DARK)

    def _propagate_palette(self) -> None:
        """Propage la palette courante à tous les panes et au LogsPane."""
        palette = self._current_palette()
        pane_map = [
            ("#overview-page", OverviewPane),
            ("#positions-plans-pane", PositionsPlansPane),
            ("#armed-plans-pane", ArmedPlansPane),
            ("#decisions-pane", DecisionsPane),
            ("#equity-trades-pane", EquityTradesPane),
            ("#universe-pane", UniversePane),
        ]
        for pane_id, cls in pane_map:
            try:
                pane = self.query_one(pane_id, cls)  # type: ignore[arg-type]
                pane._current_palette = palette
            except Exception:
                pass
        try:
            nav = self.query_one("#cockpit-nav", CockpitNav)
            nav.update_page(self._active_page_key, palette=palette)
        except Exception:
            pass
        try:
            logs_pane: LogsPane = self.query_one("#logs-pane", LogsPane)
            logs_pane._current_palette = palette
        except Exception:
            pass

    def _schedule_refresh_state(self) -> None:
        """Démarre le worker de refresh dans un thread dédié."""
        self.run_worker(self._load_state_worker, thread=True)

    def _load_state_worker(self) -> None:
        """Charge l'état depuis le disque dans un thread (hors UI loop)."""
        try:
            state = load_runtime_state(state_dir=_STATE_DIR, config_dir=_CONFIG_DIR)
            kill_active = _KILL_FILE.exists()
            state["kill_switch"] = kill_active
            self.call_from_thread(self._apply_state, state, kill_active)
        except Exception:
            pass

    def _apply_state(self, state: dict, kill_active: bool) -> None:
        """Met à jour tous les panes avec l'état chargé (appelé depuis le thread UI)."""
        self._last_state = state
        self._last_kill_active = kill_active
        try:
            palette = self._current_palette()

            status: CockpitStatus = self.query_one("#cockpit-status", CockpitStatus)
            status.update_state(state, kill_active, palette=palette)

            overview: OverviewPane = self.query_one("#overview-page", OverviewPane)
            overview._current_palette = palette
            overview.update_state(state, kill_active)

            positions_plans: PositionsPlansPane = self.query_one(
                "#positions-plans-pane", PositionsPlansPane
            )
            positions_plans._current_palette = palette
            positions_plans.update_state(state)

            armed_plans: ArmedPlansPane = self.query_one("#armed-plans-pane", ArmedPlansPane)
            armed_plans._current_palette = palette
            armed_plans.update_state(state)

            decisions: DecisionsPane = self.query_one("#decisions-pane", DecisionsPane)
            decisions._current_palette = palette
            decisions.update_state(state)

            equity_trades: EquityTradesPane = self.query_one(
                "#equity-trades-pane", EquityTradesPane
            )
            equity_trades._current_palette = palette
            equity_trades.update_state(state)

            universe: UniversePane = self.query_one("#universe-pane", UniversePane)
            universe._current_palette = palette
            universe.update_state(state)

        except Exception:
            pass  # tolérant — widgets restent à leur dernier état

    def _poll_events(self) -> None:
        """Lit les nouvelles lignes d'events.jsonl et les ajoute au LogsPane."""
        try:
            logs_pane: LogsPane = self.query_one("#logs-pane", LogsPane)
            logs_pane.poll_events(_EVENTS_FILE)
        except Exception:
            pass

    def action_toggle_cycles(self) -> None:
        try:
            self.query_one("#logs-pane", LogsPane).toggle_cycles()
        except Exception:
            pass

    def action_toggle_scroll(self) -> None:
        try:
            self.query_one("#logs-pane", LogsPane).toggle_scroll()
        except Exception:
            pass

    def action_toggle_logs(self) -> None:
        """Bascule entre la page Logs et la dernière page métier."""
        if self._active_page_key == "logs":
            self._set_active_page(self._previous_non_logs_page or "home")
        else:
            self._set_active_page("logs")

    def action_toggle_theme(self) -> None:
        """Bascule entre casys-salmon (clair) et casys-ink (sombre)."""
        if self.theme == "casys-salmon":
            self.theme = "casys-ink"
        else:
            self.theme = "casys-salmon"
        self._propagate_palette()
        if self._last_state is not None:
            self._apply_state(self._last_state, self._last_kill_active)

    def action_quit_confirm(self) -> None:
        """Quitte avec confirmation si un daemon est vivant."""
        from trader.cockpit.supervisor import daemon_vital_state, stop_daemon

        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        if vital.status != "alive":
            self.exit()
            return

        try:
            import json as _json

            _data = _json.loads(
                (_STATE_DIR / "daemon_status.json").read_text(encoding="utf-8")
            )
            _pid: int | None = int(_data.get("pid")) if _data.get("pid") else None
        except Exception:
            _pid = None

        async def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                result = stop_daemon(
                    pid_file=_STATE_DIR / "daemon.pid",
                    status_file=_STATE_DIR / "daemon_status.json",
                )
                if result.stopped:
                    self.notify(
                        f"Daemon arrêté (PID {result.pid})", severity="information"
                    )
                else:
                    self.notify(
                        "Daemon non arrêté (déjà mort ou identité KO)",
                        severity="warning",
                    )
            except Exception as exc:  # noqa: BLE001
                self.notify(
                    f"Erreur arrêt daemon : {exc}",
                    severity="warning",
                )
            finally:
                self.exit()

        self.push_screen(ConfirmQuit(pid=_pid), _on_confirm)

    def action_start_daemon(self) -> None:
        """Lance le daemon en process détaché (anti-double-lancement via daemon.pid)."""
        from trader.cockpit.supervisor import launch_daemon

        result = launch_daemon(
            pid_file=_STATE_DIR / "daemon.pid",
            log_file=_STATE_DIR / "daemon_console.log",
            root=_ROOT,
            status_file=_STATE_DIR / "daemon_status.json",
        )
        if result.launched:
            self.notify(f"Daemon lancé (PID {result.pid})", severity="information")
        else:
            self.notify("Daemon déjà en marche", severity="warning")

    def action_stop_daemon_confirm(self) -> None:
        """Ouvre le modal de confirmation pour arrêter le daemon."""

        async def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            from trader.cockpit.supervisor import stop_daemon

            result = stop_daemon(
                pid_file=_STATE_DIR / "daemon.pid",
                status_file=_STATE_DIR / "daemon_status.json",
            )
            if result.stopped:
                self.notify(f"Daemon arrêté (PID {result.pid})", severity="information")
            else:
                self.notify("Pas de daemon en cours", severity="warning")

        self.push_screen(ConfirmStop(), _on_confirm)

    def action_toggle_kill(self) -> None:
        """Ouvre le modal de confirmation pour toggler le kill-switch."""
        kill_active = _KILL_FILE.exists()

        async def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            from trader.cockpit.supervisor import toggle_kill_switch

            active = toggle_kill_switch(kill_file=_KILL_FILE)
            status = "activé" if active else "désactivé"
            self.notify(
                f"Kill-switch {status}",
                severity="warning" if active else "information",
            )

        self.push_screen(ConfirmKill(kill_active=kill_active), _on_confirm)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def main() -> None:
    """Lance le cockpit. Quitter avec q ou Ctrl+C."""
    app = CockpitApp()
    app.run()


if __name__ == "__main__":
    main()
