"""cockpit — Cockpit console unifié v1 (Textual).

Fusion COMPLÈTE du dashboard TUI (KPIs, courbe d'équité, positions, attribution,
décisions, learnings, header daemon) et d'un flux de logs live (events.jsonl),
en une seule app 100 % console.

Parité garantie avec tui.py : toutes les fonctions _build_* sont importées et
réutilisées via Static.update(renderable) — aucune logique n'est dupliquée.

Usage :
    uv run python -m trader.cockpit
    make cockpit

Raccourcis :
    q         Quitter
    c         Toggle l'affichage des events cycle_started/cycle_completed
    f         Pause/reprise de l'auto-scroll du panneau logs
    l         Toggle visibilité du panneau logs (plein écran dashboard)
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, RichLog, Static

from trader.cockpit_events import (
    EventClass,
    format_event_line,
    read_new_lines,
)
from trader.tui import (
    _build_attribution_panel,
    _build_decisions_table,
    _build_equity_panel,
    _build_kpi_band,
    _build_learnings_panel,
    _build_positions_panel,
    _safe_float,
    _safe_list_of_dicts,
    load_runtime_state,
)

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _ROOT / "state"
_EVENTS_FILE = _STATE_DIR / "events.jsonl"
_KILL_FILE = _ROOT / "KILL"

# Buffer maximum pour le panneau events
_MAX_EVENT_LINES = 500

# ---------------------------------------------------------------------------
# Couleurs par EventClass (Rich markup style)
# ---------------------------------------------------------------------------
_EVENT_STYLES: dict[EventClass, str] = {
    EventClass.DECISION_EXECUTED: "bold green",
    EventClass.RISK_REJECT:       "bold red",
    EventClass.STALE:             "dim",
    EventClass.HOLD:              "dim white",
    EventClass.WATCH:             "bold cyan",
    EventClass.LEARNING:          "blue",
    EventClass.CYCLE:             "dim grey50",
    EventClass.ERROR:             "bold yellow",
    EventClass.OTHER:             "dim",
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

    def update_state(self, state: dict, kill_active: bool) -> None:
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        daemon_status = state.get("daemon_status") if isinstance(state.get("daemon_status"), dict) else {}

        equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
        cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
        starting_cash = _safe_float(state.get("starting_cash"), default=cash) or cash
        pnl = equity - starting_cash
        ret_pct = _safe_float(portfolio.get("total_return_pct"), default=0.0) or 0.0
        phase = str(daemon_status.get("phase", "—"))
        current_symbol = str(daemon_status.get("current_symbol") or "—")
        calls_used = daemon_status.get("model_calls_used")
        calls_max = daemon_status.get("max_model_calls_per_cycle")
        calls_str = f"{calls_used}/{calls_max}" if calls_used is not None and calls_max is not None else "—"
        now_utc = datetime.now(UTC).strftime("%H:%M:%S UTC")
        dry_run = state.get("dry_run", True)

        mode_str = "[bold red]LIVE[/bold red]" if not dry_run else "[bold yellow]DRY-RUN[/bold yellow]"
        kill_str = "[bold red on white] KILL ACTIF [/bold red on white]" if kill_active else "[green]nominal[/green]"
        ret_style = "green" if ret_pct >= 0 else "red"
        pnl_style = "green" if pnl >= 0 else "red"

        text = (
            f"  Équité [bold cyan]${equity:,.2f}[/bold cyan]"
            f"  Cash [cyan]${cash:,.2f}[/cyan]"
            f"  P&L [{ret_style}]{ret_pct:+.2f}%[/{ret_style}]"
            f" [{pnl_style}]({pnl:+,.2f})[/{pnl_style}]"
            f"  Daemon [magenta]{phase}[/magenta]"
            f"  Symbole [cyan]{current_symbol}[/cyan]"
            f"  Appels [cyan]{calls_str}[/cyan]"
            f"  {now_utc}"
            f"  Mode {mode_str}"
            f"  Kill {kill_str}"
        )
        self.update(Text.from_markup(text))


# ---------------------------------------------------------------------------
# Widget : panneau gauche — tout le dashboard TUI
# ---------------------------------------------------------------------------


class DashboardPane(Static):
    """Panneau gauche : reprend intégralement le contenu de tui.build_view.

    Utilise les fonctions _build_* importées de tui.py via Static.update().
    """

    DEFAULT_CSS = """
    DashboardPane {
        width: 60%;
        height: 100%;
        border-right: solid $primary;
        overflow-y: auto;
    }
    DashboardPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="kpi-band")
        yield Static(id="equity-panel")
        yield Static(id="positions-panel")
        yield Static(id="attribution-panel")
        yield Static(id="decisions-table")
        yield Static(id="learnings-panel")

    def update_state(self, state: dict) -> None:
        """Recharge tous les sous-panneaux avec le dernier état."""
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        attribution = state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
        holdings = _safe_list_of_dicts(portfolio.get("holdings"))
        decisions = _safe_list_of_dicts(state.get("decisions"))
        equity_curve_raw = state.get("equity_curve") or []
        equity_curve = [
            v for v in (_safe_float(x, default=None) for x in equity_curve_raw)
            if v is not None
        ]
        learnings = _safe_list_of_dicts(state.get("learnings"))

        self.query_one("#kpi-band", Static).update(_build_kpi_band(kpis))
        self.query_one("#equity-panel", Static).update(_build_equity_panel(equity_curve))
        self.query_one("#positions-panel", Static).update(_build_positions_panel(holdings))
        self.query_one("#attribution-panel", Static).update(_build_attribution_panel(attribution))
        self.query_one("#decisions-table", Static).update(_build_decisions_table(decisions))

        learnings_renderable = _build_learnings_panel(learnings)
        if learnings_renderable is not None:
            self.query_one("#learnings-panel", Static).update(learnings_renderable)
        else:
            self.query_one("#learnings-panel", Static).update(Text(""))


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
                log.write(Text(f"[events] {events_path.name} absent — en attente…", style="dim"))
            return

        # Fichier présent : réinitialise le statut si on revenait d'absent
        if self._last_file_status == "absent":
            self._last_file_status = "ok"
            log.write(Text(f"[events] {events_path.name} disponible", style="dim"))

        new_dicts, new_offset = read_new_lines(events_path, self._offset)
        self._offset = new_offset

        if not new_dicts:
            return

        for ev_dict in new_dicts:
            ev_line = format_event_line(ev_dict)
            if ev_line.markup_class == EventClass.CYCLE and not self._show_cycles:
                continue
            style = _EVENT_STYLES.get(ev_line.markup_class, "")
            log.write(Text(ev_line.text, style=style))

        if self._auto_scroll:
            log.scroll_end(animate=False)


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
        Binding("q", "quit", "Quitter"),
        Binding("c", "toggle_cycles", "Toggle cycles"),
        Binding("f", "toggle_scroll", "Pause scroll"),
        Binding("l", "toggle_logs", "Toggle logs"),
    ]

    _logs_visible: bool = True

    def compose(self) -> ComposeResult:
        yield CockpitStatus(id="cockpit-status")
        yield Static(id="main-body")
        yield Footer()

    def on_mount(self) -> None:
        body = self.query_one("#main-body", Static)
        body.mount(DashboardPane(id="dashboard-pane"))
        body.mount(EventsPane(id="events-pane"))
        # Polling état toutes les 2 s (via worker thread — I/O hors UI loop)
        self.set_interval(2.0, self._schedule_refresh_state)
        # Polling events toutes les 1 s
        self.set_interval(1.0, self._poll_events)
        # Charge immédiatement
        self._schedule_refresh_state()
        self._poll_events()

    def _schedule_refresh_state(self) -> None:
        """Démarre le worker de refresh dans un thread dédié."""
        self.run_worker(self._load_state_worker, thread=True)

    def _load_state_worker(self) -> None:
        """Charge l'état depuis le disque dans un thread (hors UI loop).

        Poste le résultat à l'UI via call_from_thread pour éviter tout blocage.
        """
        try:
            state = load_runtime_state(state_dir=_STATE_DIR)
            kill_active = _KILL_FILE.exists()
            state["kill_switch"] = kill_active
            self.call_from_thread(self._apply_state, state, kill_active)
        except Exception:
            pass

    def _apply_state(self, state: dict, kill_active: bool) -> None:
        """Met à jour les widgets avec l'état chargé (appelé depuis le thread UI)."""
        try:
            status: CockpitStatus = self.query_one("#cockpit-status", CockpitStatus)
            status.update_state(state, kill_active)

            dashboard: DashboardPane = self.query_one("#dashboard-pane", DashboardPane)
            dashboard.update_state(state)
        except Exception:
            pass  # tolérant — widgets restent à leur dernier état

    def _poll_events(self) -> None:
        """Lit les nouvelles lignes d'events.jsonl."""
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            events_pane.poll_events(_EVENTS_FILE)
        except Exception:
            pass

    def action_toggle_cycles(self) -> None:
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            events_pane.toggle_cycles()
        except Exception:
            pass

    def action_toggle_scroll(self) -> None:
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            events_pane.toggle_scroll()
        except Exception:
            pass

    def action_toggle_logs(self) -> None:
        """Affiche/masque le panneau logs pour maximiser le dashboard."""
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            dashboard: DashboardPane = self.query_one("#dashboard-pane", DashboardPane)
            self._logs_visible = not self._logs_visible
            if self._logs_visible:
                events_pane.display = True
                dashboard.styles.width = "60%"
            else:
                events_pane.display = False
                dashboard.styles.width = "100%"
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def main() -> None:
    """Lance le cockpit. Quitter avec q ou Ctrl+C."""
    app = CockpitApp()
    app.run()


if __name__ == "__main__":
    main()
