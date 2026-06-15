"""cockpit — Salle de contrôle du daemon casys-trader (Textual).

Dashboard complet (KPIs, courbe d'équité, positions, attribution, décisions,
learnings) + flux de logs live (events.jsonl) + supervision du daemon.

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
    l         Toggle visibilité du panneau logs (plein écran dashboard)
    d         Dark/Light (thèmes casys-salmon / casys-ink)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Label, RichLog, Static

from trader.cockpit_supervisor import daemon_vital_state
from trader.rotation_venues import load_venue_state
from trader.rotation_schedule import load_sessions, open_venues as _open_venues

from trader.cockpit_events import (
    EventClass,
    format_event_line,
    read_new_lines,
)
from trader.palette import PALETTE_DARK, PALETTE_LIGHT, Palette
from trader.tui import (
    _build_armed_plans_panel,
    _build_attribution_panel,
    _build_data_health_panel,
    _build_decisions_table,
    _build_equity_panel,
    _build_exit_plans_panel,
    _build_kpi_band,
    _build_learnings_panel,
    _build_llm_activity_panel,
    _build_positions_panel,
    _build_watches_panel,
    _enrich_decisions_with_data_source,
    _safe_float,
    _safe_list_of_dicts,
    build_selection_panel,
    build_trades_table,
    load_runtime_state,
)

# ---------------------------------------------------------------------------
# Thèmes Textual
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _ROOT / "state"
_CONFIG_DIR = str(_ROOT)
_EVENTS_FILE = _STATE_DIR / "events.jsonl"
_KILL_FILE = _ROOT / "KILL"

# Buffer maximum pour le panneau events
_MAX_EVENT_LINES = 500

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
# Widget : barre de statut 1 ligne (en plus du Header Textual)
# ---------------------------------------------------------------------------


class CockpitStatus(Static):
    """Ligne d'état compacte : équité, cash, P&L, phase daemon, horloge UTC, kill."""

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
        portfolio = (
            state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        )
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        daemon_status = (
            state.get("daemon_status")
            if isinstance(state.get("daemon_status"), dict)
            else {}
        )

        equity = (
            _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0)
            or 0.0
        )
        cash = (
            _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
        )
        starting_cash = _safe_float(state.get("starting_cash"), default=cash) or cash
        pnl = equity - starting_cash
        ret_pct = _safe_float(portfolio.get("total_return_pct"), default=0.0) or 0.0
        phase = str(daemon_status.get("phase", "—"))
        current_symbol = str(daemon_status.get("current_symbol") or "—")
        calls_used = daemon_status.get("model_calls_used")
        calls_max = daemon_status.get("max_model_calls_per_cycle")
        calls_str = (
            f"{calls_used}/{calls_max}"
            if calls_used is not None and calls_max is not None
            else "—"
        )
        now_utc = datetime.now(UTC).strftime("%H:%M:%S UTC")
        dry_run = state.get("dry_run", True)

        # Styles via palette — mode et kill gardent leurs couleurs sémantiques fixes
        mode_str = (
            "[bold red]LIVE[/bold red]"
            if not dry_run
            else "[bold yellow]DRY-RUN[/bold yellow]"
        )
        kill_str = (
            "[bold white on red] !! KILL ACTIF !! [/bold white on red]"
            if kill_active
            else f"[{palette['status_nominal']}]nominal[/{palette['status_nominal']}]"
        )
        ret_style = palette["pnl_positive"] if ret_pct >= 0 else palette["pnl_negative"]
        pnl_style = palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]
        eq_style = palette["status_equity"]
        acc_style = palette["status_accent"]
        phase_style = palette["status_phase"]

        # Indicateur vital — pid+identité (pas de seuil temporel pour vivant/mort)
        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        if vital.status == "alive":
            if vital.battement_old:
                # Daemon vivant mais battement ancien (batch long en cours)
                _bat_mins = int(vital.since_seconds) // 60 if vital.since_seconds else 0
                _bat_secs = int(vital.since_seconds) % 60 if vital.since_seconds else 0
                vital_str = (
                    f"[bold yellow]● VIVANT[/bold yellow]"
                    f" [dim](occupé, battement {_bat_mins:02d}:{_bat_secs:02d})[/dim]"
                )
            else:
                vital_str = "[bold green]● VIVANT[/bold green]"
        elif vital.status == "stopped":
            vital_str = "[bold red]● ARRÊTÉ[/bold red]"
        else:  # never_started
            vital_str = "[dim]● jamais démarré[/dim]"

        text = (
            f"  {vital_str}"
            f"  Équité [{eq_style}]${equity:,.2f}[/{eq_style}]"
            f"  Cash [{acc_style}]${cash:,.2f}[/{acc_style}]"
            f"  P&L [{ret_style}]{ret_pct:+.2f}%[/{ret_style}]"
            f" [{pnl_style}]({pnl:+,.2f})[/{pnl_style}]"
            f"  Daemon [{phase_style}]{phase}[/{phase_style}]"
            f"  Symbole [{acc_style}]{current_symbol}[/{acc_style}]"
            f"  Appels [{acc_style}]{calls_str}[/{acc_style}]"
            f"  {now_utc}"
            f"  Mode {mode_str}"
            f"  Kill {kill_str}"
        )
        self.update(Text.from_markup(text))


# ---------------------------------------------------------------------------
# Widgets v2 — layout 3 colonnes
# ---------------------------------------------------------------------------


class LeftPane(Static):
    """Colonne gauche (25%) : positions+PnL, plans sortie, apprentissages."""

    DEFAULT_CSS = """
    LeftPane {
        width: 25%;
        height: 100%;
        border-right: solid $primary;
        overflow-y: auto;
    }
    LeftPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT  # défaut saumon

    def compose(self) -> ComposeResult:
        yield Static(id="positions-panel")
        yield Static(id="armed-plans-panel")
        yield Static(id="exit-plans-panel")
        yield Static(id="learnings-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        portfolio = (
            state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        )
        holdings = _safe_list_of_dicts(portfolio.get("holdings"))
        learnings = _safe_list_of_dicts(state.get("learnings"))
        trade_plans = (
            state.get("trade_plans")
            if isinstance(state.get("trade_plans"), list)
            else []
        )

        armed_plans = (
            state.get("armed_plans")
            if isinstance(state.get("armed_plans"), list)
            else []
        )

        self.query_one("#positions-panel", Static).update(
            _build_positions_panel(holdings, palette=palette)
        )
        self.query_one("#armed-plans-panel", Static).update(
            _build_armed_plans_panel(armed_plans, palette=palette)
        )
        self.query_one("#exit-plans-panel", Static).update(
            _build_exit_plans_panel(trade_plans, palette=palette)
        )
        learnings_renderable = _build_learnings_panel(learnings, palette=palette)
        self.query_one("#learnings-panel", Static).update(
            learnings_renderable if learnings_renderable is not None else Text("")
        )


class CenterPane(Static):
    """Colonne centre (40%) : KPI band, décisions récentes (panneau roi), attribution, équité."""

    DEFAULT_CSS = """
    CenterPane {
        width: 40%;
        height: 100%;
        border-right: solid $primary;
        overflow-y: auto;
    }
    CenterPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT  # défaut saumon

    def compose(self) -> ComposeResult:
        yield Static(id="kpi-band")
        yield Static(id="decisions-table")
        yield Static(id="attribution-panel")
        yield Static(id="equity-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        attribution = (
            state.get("attribution")
            if isinstance(state.get("attribution"), dict)
            else {}
        )
        equity_curve = [
            v
            for v in (
                _safe_float(x, default=None)
                for x in (state.get("equity_curve") or [])
            )
            if v is not None
        ]
        decisions_raw = _safe_list_of_dicts(state.get("decisions"))
        recent_decisions = (
            state.get("recent_decisions")
            if isinstance(state.get("recent_decisions"), list)
            else []
        )
        decisions = _enrich_decisions_with_data_source(decisions_raw, recent_decisions)

        self.query_one("#kpi-band", Static).update(
            _build_kpi_band(kpis, palette=palette)
        )
        self.query_one("#decisions-table", Static).update(
            _build_decisions_table(decisions, palette=palette)
        )
        self.query_one("#attribution-panel", Static).update(
            _build_attribution_panel(attribution, palette=palette)
        )
        self.query_one("#equity-panel", Static).update(
            _build_equity_panel(equity_curve, palette=palette)
        )


class RightPane(Static):
    """Colonne droite (35%) : logs live (≥60%) + panneaux compacts (veilles, santé data, LLM)."""

    DEFAULT_CSS = """
    RightPane {
        width: 35%;
        height: 100%;
        layout: vertical;
    }
    RightPane EventsPane {
        width: 100%;
        height: 60%;
    }
    RightPane #compact-bottom {
        height: 40%;
        overflow-y: auto;
        layout: vertical;
    }
    RightPane #compact-bottom Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT  # défaut saumon

    def compose(self) -> ComposeResult:
        yield EventsPane(id="events-pane")
        with Vertical(id="compact-bottom"):
            yield Static(id="selection-panel")
            yield Static(id="trades-panel")
            yield Static(id="watches-panel")
            yield Static(id="data-health-panel")
            yield Static(id="llm-activity-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        indicator_watches = (
            state.get("indicator_watches")
            if isinstance(state.get("indicator_watches"), list)
            else []
        )
        stale_streaks = (
            state.get("stale_streaks")
            if isinstance(state.get("stale_streaks"), dict)
            else {}
        )
        recent_decisions = (
            state.get("recent_decisions")
            if isinstance(state.get("recent_decisions"), list)
            else []
        )
        daemon_status = (
            state.get("daemon_status")
            if isinstance(state.get("daemon_status"), dict)
            else {}
        )
        learnings_pending = state.get("learnings_pending_count") or 0
        consolidation_status = state.get("consolidation_status")

        # Panneau sélection par marché (venue_state + sessions)
        venue_state = state.get("venue_state") if isinstance(state.get("venue_state"), dict) else {}
        open_venues_list = state.get("open_venues_list") if isinstance(state.get("open_venues_list"), list) else []
        self.query_one("#selection-panel", Static).update(
            build_selection_panel(venue_state, open_venues_list, palette=palette)
        )

        # Panneau derniers trades (fills depuis broker.json)
        fills = state.get("fills") if isinstance(state.get("fills"), list) else []
        self.query_one("#trades-panel", Static).update(
            build_trades_table(fills, palette=palette)
        )

        self.query_one("#watches-panel", Static).update(
            _build_watches_panel(indicator_watches, palette=palette)
        )
        self.query_one("#data-health-panel", Static).update(
            _build_data_health_panel(recent_decisions, stale_streaks, palette=palette)
        )
        self.query_one("#llm-activity-panel", Static).update(
            _build_llm_activity_panel(
                daemon_status, learnings_pending, consolidation_status, palette=palette
            )
        )


# ---------------------------------------------------------------------------
# Widget : panneau droit — logs live
# ---------------------------------------------------------------------------


class EventsPane(Static):
    """Panneau droit : tail live d'events.jsonl avec auto-scroll et filtres."""

    _offset: int = 0
    _show_cycles: bool = True
    _auto_scroll: bool = True
    # Dernier état connu du fichier : "ok" | "absent" | "error"
    # Permet de n'émettre le diagnostic dim qu'une fois par changement d'état.
    _last_file_status: str = "ok"
    _current_palette: Palette = PALETTE_LIGHT  # défaut saumon (thème par défaut)
    # Garde-fou pour le backlog initial : différé jusqu'au premier on_ready
    # afin que RichLog ait une largeur réelle avant le premier write().
    _backlog_loaded: bool = False

    DEFAULT_CSS = """
    EventsPane {
        width: 40%;
        height: 100%;
    }
    EventsPane RichLog {
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
        """Diffère le chargement du backlog initial jusqu'après le premier layout.

        RichLog fige le wrap de chaque ligne au moment du write(). Si on écrit
        avant que le widget ait sa taille réelle (ce qui arrive lors d'un appel
        direct dans on_mount parent), les lignes se replient sur une largeur
        minimale. call_after_refresh garantit qu'au moins un cycle de layout
        s'est exécuté avant le premier write().
        """
        self.call_after_refresh(self._load_initial_backlog)

    def _load_initial_backlog(self) -> None:
        """Charge le backlog initial une seule fois, largeur déjà connue."""
        if self._backlog_loaded:
            return
        self._backlog_loaded = True
        try:
            events_path: Path = self.app._events_file  # type: ignore[attr-defined]
        except AttributeError:
            # Fallback : importation directe de la constante module
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
        """Lit les nouvelles lignes depuis events_path et les ajoute au log.

        Si le fichier est absent, émet un diagnostic dim une seule fois
        (dédupliqué par _last_file_status). Reprend normalement dès que
        le fichier réapparaît.
        """
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

        # Fichier présent : réinitialise le statut si on revenait d'absent
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


# ---------------------------------------------------------------------------
# Modals de confirmation
# ---------------------------------------------------------------------------


class ConfirmStop(ModalScreen[bool]):
    """Modal de confirmation pour l'arrêt du daemon (binding X)."""

    DEFAULT_CSS = """
    ConfirmStop {
        align: center middle;
    }
    ConfirmStop Vertical {
        background: $surface;
        border: solid $error;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    ConfirmStop Horizontal {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    ConfirmStop Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Arrêter le daemon ?\n(SIGINT — arrêt propre)")
            with Horizontal():
                yield Button("Oui", id="confirm-stop-yes", variant="error")
                yield Button("Non", id="confirm-stop-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-stop-yes")


class ConfirmQuit(ModalScreen[bool]):
    """Modal de confirmation à la sortie quand un daemon est vivant.

    Retourne :
        True  → arrêter le daemon puis quitter
        False → annuler (rester dans le cockpit)

    Échap → False (annuler).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Annuler", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmQuit {
        align: center middle;
    }
    ConfirmQuit Vertical {
        background: $surface;
        border: solid $warning;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    ConfirmQuit Horizontal {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    ConfirmQuit Button {
        margin: 0 1;
    }
    """

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
                yield Button("Arrêter et quitter", id="confirm-quit-stop", variant="warning")
                yield Button("Annuler", id="confirm-quit-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-quit-stop")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmStart(ModalScreen[bool]):
    """Modal proposé au lancement du cockpit quand aucun daemon n'est vivant.

    Retourne :
        True  → lancer le moteur live (action_start_daemon)
        False → ne rien lancer (cockpit en lecture seule)

    Échap → False (ne pas lancer).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Plus tard", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmStart {
        align: center middle;
    }
    ConfirmStart Vertical {
        background: $surface;
        border: solid $primary;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    ConfirmStart Horizontal {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    ConfirmStart Button {
        margin: 0 1;
    }
    """

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
                yield Button("Démarrer", id="confirm-start-yes", variant="primary")
                yield Button("Plus tard", id="confirm-start-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-start-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmKill(ModalScreen[bool]):
    """Modal de confirmation pour le toggle kill-switch (binding k)."""

    DEFAULT_CSS = """
    ConfirmKill {
        align: center middle;
    }
    ConfirmKill Vertical {
        background: $surface;
        border: solid $warning;
        padding: 1 2;
        width: 50;
        height: auto;
    }
    ConfirmKill Horizontal {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    ConfirmKill Button {
        margin: 0 1;
    }
    """

    def __init__(self, kill_active: bool, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._kill_active = kill_active

    def compose(self) -> ComposeResult:
        action = "Retirer" if self._kill_active else "Activer"
        with Vertical():
            yield Label(f"{action} le kill-switch ?\n(bloque/débloque tous les ordres)")
            with Horizontal():
                yield Button("Oui", id="confirm-kill-yes", variant="warning")
                yield Button("Non", id="confirm-kill-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-kill-yes")


# ---------------------------------------------------------------------------
# App principale
# ---------------------------------------------------------------------------


class CockpitApp(App):
    """Cockpit console unifié — dashboard complet + logs live."""

    TITLE = "casys-trader — cockpit"
    CSS = """
    Screen {
        layout: vertical;
    }
    #main-body {
        layout: horizontal;
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit_confirm", "Quitter"),
        # Ctrl+C : par défaut Textual le mappe sur help_quit (notification inerte).
        # On le route vers le même avertissement que q pour qu'on ne puisse jamais
        # quitter sans voir que le moteur live va être arrêté. priority pour passer
        # devant le binding système.
        Binding("ctrl+c", "quit_confirm", "Quitter", show=False, priority=True),
        Binding("s", "start_daemon", "Démarrer daemon"),
        Binding("X", "stop_daemon_confirm", "Maj+X — Arrêter daemon"),
        Binding("k", "toggle_kill", "Kill-switch"),
        Binding("c", "toggle_cycles", "Toggle cycles"),
        Binding("f", "toggle_scroll", "Pause scroll"),
        Binding("l", "toggle_logs", "Toggle logs"),
        Binding("d", "toggle_theme", "Dark/Light"),
    ]

    _logs_visible: bool = True
    # Dernier état mémorisé pour le re-render immédiat après toggle thème
    _last_state: dict | None = None
    _last_kill_active: bool = False

    def compose(self) -> ComposeResult:
        yield CockpitStatus(id="cockpit-status")
        yield Static(id="main-body")
        yield Footer()

    def on_mount(self) -> None:
        # Enregistrement des thèmes custom
        self.register_theme(_THEME_SALMON)
        self.register_theme(_THEME_INK)
        # Thème saumon par défaut (fond clair FT editorial)
        self.theme = "casys-salmon"

        # Expose le chemin events sur self pour que EventsPane._load_initial_backlog
        # puisse le résoudre même quand _EVENTS_FILE est monkeypatché en test.
        self._events_file = _EVENTS_FILE

        body = self.query_one("#main-body", Static)
        body.mount(LeftPane(id="left-pane"))
        body.mount(CenterPane(id="center-pane"))
        body.mount(RightPane(id="right-pane"))
        # Polling état toutes les 2 s (via worker thread — I/O hors UI loop)
        self.set_interval(2.0, self._schedule_refresh_state)
        # Polling events toutes les 1 s (le backlog initial est chargé par EventsPane.on_ready)
        self.set_interval(1.0, self._poll_events)
        # Charge l'état immédiatement
        self._schedule_refresh_state()
        # Propose de démarrer le daemon s'il n'est pas vivant (différé après layout)
        self.call_after_refresh(self._maybe_propose_start)

    def _maybe_propose_start(self) -> None:
        """Propose de lancer le daemon au démarrage si aucun n'est vivant.

        Ne propose jamais quand un daemon est déjà vivant. Safe default :
        la proposition ne lance rien sans confirmation explicite.
        """
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

    def _current_palette(self) -> Palette:
        """Retourne la palette Rich correspondant au thème actif."""
        return _THEME_PALETTE.get(self.theme, PALETTE_DARK)

    def _propagate_palette(self) -> None:
        """Propage la palette courante aux 3 panes et à EventsPane."""
        palette = self._current_palette()
        for pane_id, cls in (
            ("#left-pane", LeftPane),
            ("#center-pane", CenterPane),
            ("#right-pane", RightPane),
        ):
            try:
                pane = self.query_one(pane_id, cls)  # type: ignore[arg-type]
                pane._current_palette = palette
            except Exception:
                pass
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            events_pane: EventsPane = right.query_one("#events-pane", EventsPane)
            events_pane._current_palette = palette
        except Exception:
            pass

    def _schedule_refresh_state(self) -> None:
        """Démarre le worker de refresh dans un thread dédié."""
        self.run_worker(self._load_state_worker, thread=True)

    def _load_state_worker(self) -> None:
        """Charge l'état depuis le disque dans un thread (hors UI loop).

        Poste le résultat à l'UI via call_from_thread pour éviter tout blocage.
        """
        try:
            state = load_runtime_state(state_dir=_STATE_DIR, config_dir=_CONFIG_DIR)
            kill_active = _KILL_FILE.exists()
            state["kill_switch"] = kill_active
            self.call_from_thread(self._apply_state, state, kill_active)
        except Exception:
            pass

    def _apply_state(self, state: dict, kill_active: bool) -> None:
        """Met à jour les 3 panes avec l'état chargé (appelé depuis le thread UI)."""
        # Mémoriser pour le re-render immédiat lors du toggle thème
        self._last_state = state
        self._last_kill_active = kill_active
        try:
            palette = self._current_palette()
            status: CockpitStatus = self.query_one("#cockpit-status", CockpitStatus)
            status.update_state(state, kill_active, palette=palette)

            left: LeftPane = self.query_one("#left-pane", LeftPane)
            left._current_palette = palette
            left.update_state(state)

            center: CenterPane = self.query_one("#center-pane", CenterPane)
            center._current_palette = palette
            center.update_state(state)

            right: RightPane = self.query_one("#right-pane", RightPane)
            right._current_palette = palette
            right.update_state(state)
        except Exception:
            pass  # tolérant — widgets restent à leur dernier état

    def _poll_events(self) -> None:
        """Lit les nouvelles lignes d'events.jsonl."""
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            events_pane: EventsPane = right.query_one("#events-pane", EventsPane)
            events_pane.poll_events(_EVENTS_FILE)
        except Exception:
            pass

    def action_toggle_cycles(self) -> None:
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            right.query_one("#events-pane", EventsPane).toggle_cycles()
        except Exception:
            pass

    def action_toggle_scroll(self) -> None:
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            right.query_one("#events-pane", EventsPane).toggle_scroll()
        except Exception:
            pass

    def action_toggle_logs(self) -> None:
        """Affiche/masque le panneau droit entier (logs + panneaux compacts) pour maximiser."""
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            left: LeftPane = self.query_one("#left-pane", LeftPane)
            center: CenterPane = self.query_one("#center-pane", CenterPane)
            self._logs_visible = not self._logs_visible
            if self._logs_visible:
                right.display = True
                left.styles.width = "25%"
                center.styles.width = "40%"
            else:
                right.display = False
                left.styles.width = "30%"
                center.styles.width = "70%"
        except Exception:
            pass

    def action_toggle_theme(self) -> None:
        """Bascule entre casys-salmon (clair) et casys-ink (sombre).

        Propage la nouvelle palette et re-rend immédiatement depuis le dernier
        état mémorisé — pas d'attente du prochain cycle de refresh.
        """
        if self.theme == "casys-salmon":
            self.theme = "casys-ink"
        else:
            self.theme = "casys-salmon"
        self._propagate_palette()
        # Re-render immédiat avec la nouvelle palette
        if self._last_state is not None:
            self._apply_state(self._last_state, self._last_kill_active)

    def action_quit_confirm(self) -> None:
        """Quitte avec confirmation si un daemon est vivant.

        Si aucun daemon vivant → quit direct (pas de modal).
        Si daemon vivant → ConfirmQuit modal :
            - « Arrêter et quitter » → stop_daemon (SIGINT) puis quit.
            - « Annuler » / Échap   → rester dans le cockpit.
        """
        from trader.cockpit_supervisor import daemon_vital_state, stop_daemon

        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        if vital.status != "alive":
            self.exit()
            return

        # Récupère le PID pour l'afficher dans le modal
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
        from trader.cockpit_supervisor import launch_daemon

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
            from trader.cockpit_supervisor import stop_daemon

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
            from trader.cockpit_supervisor import toggle_kill_switch

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
