"""cockpit — casys control room (Textual), thème signature « casys ».

Shell :
    Screen = Horizontal( NavRail 17 cols │ Vertical( KpiBand · page · Footer ) )

8 pages (1 module = 1 page, voir ``pages/``) : home (Decision Journal),
portfolio, decisions, plans, health, logs, universe, settings.

Pattern superviseur inchangé : le daemon reste un process indépendant qui
survit à la fermeture du cockpit (start ``s`` / stop ``x`` / kill ``k``).
Premier lancement (never_started, aucun état) → écran preflight plein page ;
rien ne démarre sans un ``s`` explicite.

Écriture autorisée (UNIQUEMENT ces fichiers) :
    state/daemon.pid          — PID du daemon au lancement
    state/daemon.lock         — verrou anti-double-lancement (flock)
    state/daemon_console.log  — stdout/stderr du daemon (append)
    state/agent_trace.log     — trace séparée des appels/outcomes agent
    KILL                      — fichier kill-switch (toggle)
    config/universe.yaml      — bloc overrides: (pin/ban, page Universe)
    config/universe.yaml.lock — verrou du read-modify-write overrides
    config/*.yaml             — écritures explicites de la page Settings (w)

Usage :
    uv run python -m trader.interfaces.cockpit
    make watch
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.theme import Theme
from textual.widgets import ContentSwitcher

from trader.interfaces.cockpit.first_run import FirstRunScreen
from trader.interfaces.cockpit.modals import (
    ClassFilterModal,
    ConfirmKill,
    ConfirmQuit,
    ConfirmStop,
    HelpOverlay,
    RegexModal,
)
from trader.interfaces.cockpit.pages import PAGE_BY_KEY, PAGE_KEYS, PAGES
from trader.interfaces.cockpit.pages._shared import SymbolChosen
from trader.interfaces.cockpit.pages.decisions import DecisionsPage
from trader.interfaces.cockpit.pages.health import HealthPage
from trader.interfaces.cockpit.pages.home import HomePage
from trader.interfaces.cockpit.pages.logs import AgentTracePane, LogsPage, LogsPane
from trader.interfaces.cockpit.pages.plans import PlansPage
from trader.interfaces.cockpit.pages.portfolio import PortfolioPage
from trader.interfaces.cockpit.pages.settings import SettingsPage
from trader.interfaces.cockpit.pages.universe import UniversePage
from trader.interfaces.cockpit.shell import CockpitFooter, KpiBand, NavItem, NavRail
from trader.interfaces.cockpit.supervisor import daemon_vital_state
from trader.interfaces.ui.palette import PALETTE_CASYS, Palette
from trader.reporting.read_models.runtime_state import load_runtime_state

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Thème unique
# ---------------------------------------------------------------------------

THEME_CASYS = Theme(
    name="casys",
    dark=True,
    primary="#FFB86F",  # accent structurel : titres, nav active, countdowns, sélection
    background="#0f0e0c",
    surface="#14110e",  # rail
    panel="#1a1815",  # footer / barres
    success="#a5c98c",  # gain · long · buy · running · fresh
    error="#e87f66",  # loss · short · sell
    warning="#e5c07b",  # stale · attention (jamais structurel)
    foreground="#f5f0ea",
    secondary="#8d8177",
    accent="#FFB86F",
)

# ---------------------------------------------------------------------------
# Chemins (constantes patchables par les tests)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[3]
_STATE_DIR = _ROOT / "state"
_CONFIG_DIR = str(_ROOT)
_EVENTS_FILE = _STATE_DIR / "events.jsonl"
_AGENT_TRACE_FILE = _STATE_DIR / "agent_trace.log"
_KILL_FILE = _ROOT / "KILL"

_PAGE_WIDGETS = {
    "home": HomePage,
    "portfolio": PortfolioPage,
    "decisions": DecisionsPage,
    "plans": PlansPage,
    "health": HealthPage,
    "logs": LogsPage,
    "universe": UniversePage,
    "settings": SettingsPage,
}

_NAV_ITEMS = tuple(NavItem(key=p.key, number=p.number, label=p.label) for p in PAGES)

# Alias de rétrocompatibilité — anciens tests/imports
EventsPane = LogsPane


class CockpitApp(App):
    """Cockpit casys — rail + KPI band + 8 pages + footer contextuel."""

    TITLE = "casys cockpit"

    # Classes de breakpoint posées sur le Screen selon la largeur du terminal :
    # -compact < 110 ≤ -medium < 140 ≤ -wide. Le CSS d'app (prioritaire sur les
    # DEFAULT_CSS des pages) adapte les layouts sans toucher aux modules.
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (110, "-medium"), (140, "-wide")]

    CSS = """
    Screen {
        layout: horizontal;
    }
    #main {
        width: 1fr;
        height: 100%;
        layout: vertical;
    }
    #page-switcher {
        width: 100%;
        height: 1fr;
    }
    .cockpit-page {
        width: 100%;
        height: 100%;
    }
    * {
        scrollbar-size: 1 1;
        scrollbar-color: #332c23;
        scrollbar-background: #14110e;
    }

    /* ---- medium (110-139 cols) : colonnes droites fixes compressées ---- */
    Screen.-medium PortfolioPage #portfolio-right { width: 34; }
    Screen.-medium PlansPage #plans-right { width: 36; }
    Screen.-medium UniversePage #universe-right { width: 36; }
    Screen.-medium DecisionsPage #decisions-right { width: 32; }

    /* ---- compact (<110 cols) : la donnée principale garde toute la largeur,
       la colonne secondaire passe dessous (scroll interne) ---- */
    Screen.-compact NavRail { width: 6; min-width: 6; max-width: 6; }
    Screen.-compact HomePage { layout: vertical; }
    Screen.-compact HomePage #journal-panel { width: 100%; height: 2fr; margin-right: 0; }
    Screen.-compact HomePage #home-right { width: 100%; height: 16; layout: horizontal; }
    Screen.-compact HomePage #home-right > .casys-panel {
        width: 1fr; height: 100%; margin-bottom: 0; margin-right: 1;
    }
    Screen.-compact PortfolioPage { layout: vertical; }
    Screen.-compact PortfolioPage #portfolio-right {
        width: 100%; height: 14; overflow-y: auto;
    }
    Screen.-compact PlansPage { layout: vertical; }
    Screen.-compact PlansPage #plans-right { width: 100%; height: 14; overflow-y: auto; }
    Screen.-compact UniversePage { layout: vertical; }
    Screen.-compact UniversePage #universe-right { width: 100%; height: 14; overflow-y: auto; }
    Screen.-compact DecisionsPage { layout: vertical; }
    Screen.-compact DecisionsPage #decisions-right { width: 100%; height: 12; overflow-y: auto; }
    Screen.-compact SettingsPage { layout: vertical; }
    Screen.-compact HealthPage { layout: vertical; }
    Screen.-compact LogsPage { layout: vertical; }
    Screen.-compact LogsPage #events-panel { width: 100%; height: 2fr; margin-right: 0; }
    Screen.-compact LogsPage #agent-trace-panel { width: 100%; height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit_confirm", "quit"),
        # Ctrl+C → même comportement que q (avertissement si daemon vivant)
        Binding("ctrl+c", "quit_confirm", "quit", show=False, priority=True),
        Binding("s", "start_daemon", "start daemon"),
        Binding("x", "stop_daemon_confirm", "stop daemon"),
        Binding("k", "toggle_kill", "kill-switch"),
        Binding("c", "toggle_cycles", "cycles", show=False),
        Binding("f", "toggle_scroll", "follow", show=False),
        Binding("F", "filter_classes", "classes", show=False),
        Binding("slash", "filter_regex", "regex", show=False),
        Binding("question_mark", "help", "help", show=False),
        *(
            Binding(str(page.number), f"show_page('{page.key}')", page.label, show=False)
            for page in PAGES
        ),
    ]

    _last_state: dict | None = None
    _last_kill_active: bool = False
    _active_page_key: str = "home"

    def __init__(self, *, force_preflight: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._force_preflight = force_preflight

    def compose(self) -> ComposeResult:
        rail = NavRail(id="nav-rail")
        yield rail
        with Vertical(id="main"):
            yield KpiBand(id="kpi-band")
            with ContentSwitcher(id="page-switcher", initial="home-page"):
                for page in PAGES:
                    widget_cls = _PAGE_WIDGETS[page.key]
                    yield widget_cls(id=page.widget_id, classes="cockpit-page")
            yield CockpitFooter(id="cockpit-footer")

    def on_mount(self) -> None:
        self.register_theme(THEME_CASYS)
        self.theme = "casys"

        rail = self.query_one("#nav-rail", NavRail)
        rail.set_items(_NAV_ITEMS)
        self._set_active_page("home")

        # Chemins exposés sur self : les panes les résolvent même monkeypatchés.
        self._root = _ROOT
        self._state_dir = _STATE_DIR
        self._events_file = _EVENTS_FILE
        self._agent_trace_file = _AGENT_TRACE_FILE

        # Polling état 2 s (worker thread — I/O hors UI loop) + events 1 s.
        self.set_interval(2.0, self._schedule_refresh_state)
        self.set_interval(1.0, self._poll_events)
        self._schedule_refresh_state()
        self.call_after_refresh(self._maybe_first_run)

    # ------------------------------------------------------------------
    # First run
    # ------------------------------------------------------------------

    def _maybe_first_run(self) -> None:
        """Écran preflight au premier lancement (jamais de démarrage implicite).

        Conditions : daemon jamais démarré ET aucun historique dans state/.
        ``--preflight`` force l'affichage (prévisualisation / captures).
        """
        from trader.interfaces.cockpit.first_run import has_state_history

        if self._force_preflight:
            self.push_screen(FirstRunScreen())
            return
        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        if vital.status != "never_started":
            return
        if has_state_history(_STATE_DIR):
            return
        self.push_screen(FirstRunScreen())

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _set_active_page(self, page_key: str) -> None:
        if page_key not in PAGE_BY_KEY:
            return
        self._active_page_key = page_key
        try:
            switcher = self.query_one("#page-switcher", ContentSwitcher)
            switcher.current = PAGE_BY_KEY[page_key].widget_id
        except Exception:
            pass
        try:
            self.query_one("#nav-rail", NavRail).set_active(page_key)
        except Exception:
            pass
        try:
            self.query_one("#cockpit-footer", CockpitFooter).set_page(page_key)
        except Exception:
            pass
        # La page qui devient visible repart du dernier état connu.
        if self._last_state is not None:
            self._update_page(page_key, self._last_state)
        # Focus dans la page : ses BINDINGS (p/b/u, w/r, o…) s'activent via la
        # chaîne de focus ; les tables gardent leurs flèches/enter.
        try:
            page = self.query_one(f"#{PAGE_BY_KEY[page_key].widget_id}")
            focusables = [w for w in page.query("*") if w.can_focus]
            (focusables[0] if focusables else page).focus()
        except Exception:
            pass

    def action_show_page(self, page_key: str) -> None:
        self._set_active_page(page_key)

    def action_next_page(self) -> None:
        index = PAGE_KEYS.index(self._active_page_key)
        self._set_active_page(PAGE_KEYS[(index + 1) % len(PAGE_KEYS)])

    def action_previous_page(self) -> None:
        index = PAGE_KEYS.index(self._active_page_key)
        self._set_active_page(PAGE_KEYS[(index - 1) % len(PAGE_KEYS)])

    def on_symbol_chosen(self, message: SymbolChosen) -> None:
        from trader.interfaces.cockpit.pages.symbol_detail import SymbolDetailScreen

        self.push_screen(SymbolDetailScreen(message.symbol))

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _current_palette(self) -> Palette:
        """Palette Rich unique du thème casys (compat interface historique)."""
        return PALETTE_CASYS

    def _schedule_refresh_state(self) -> None:
        # exclusive : un refresh lent ne peut pas réappliquer un état périmé
        # par-dessus un refresh plus récent (le précédent est annulé).
        self.run_worker(
            self._load_state_worker, thread=True, exclusive=True, group="state-refresh"
        )

    def _load_state_worker(self) -> None:
        try:
            state = load_runtime_state(state_dir=_STATE_DIR, config_dir=_CONFIG_DIR)
            kill_active = _KILL_FILE.exists()
            state["kill_switch"] = kill_active
            self.call_from_thread(self._apply_state, state, kill_active)
        except Exception:
            pass

    def _apply_state(self, state: dict, kill_active: bool) -> None:
        """Met à jour le shell + la page active (les autres au switch)."""
        self._last_state = state
        self._last_kill_active = kill_active
        now = datetime.now(UTC)
        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        try:
            self.query_one("#nav-rail", NavRail).update_state(
                state, vital=vital, kill_active=kill_active, now=now
            )
        except Exception:
            pass
        try:
            self.query_one("#kpi-band", KpiBand).update_state(state, now=now)
        except Exception:
            pass
        self._update_page(self._active_page_key, state)

    def _update_page(self, page_key: str, state: dict) -> None:
        spec = PAGE_BY_KEY.get(page_key)
        if spec is None:
            return
        try:
            page = self.query_one(f"#{spec.widget_id}")
            page.update_state(state)  # type: ignore[attr-defined]
        except Exception:
            pass  # tolérant — la page garde son dernier rendu

    def _poll_events(self) -> None:
        events_path = getattr(self, "_events_file", _EVENTS_FILE)
        agent_trace_path = getattr(self, "_agent_trace_file", _AGENT_TRACE_FILE)
        try:
            self.query_one("#logs-page", LogsPage).poll(events_path, agent_trace_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Logs : filtres
    # ------------------------------------------------------------------

    def _logs_pane(self) -> LogsPane:
        return self.query_one("#events-panel", LogsPane)

    def action_toggle_cycles(self) -> None:
        try:
            self._logs_pane().toggle_cycles()
        except Exception:
            pass

    def action_toggle_scroll(self) -> None:
        try:
            self._logs_pane().toggle_scroll()
        except Exception:
            pass
        try:
            self.query_one("#agent-trace-panel", AgentTracePane).toggle_scroll()
        except Exception:
            pass

    def action_filter_classes(self) -> None:
        try:
            pane = self._logs_pane()
        except Exception:
            return

        def _apply(classes) -> None:
            if classes == pane._class_filter:
                return
            pane.set_filters(classes, pane._regex_text)

        self.push_screen(ClassFilterModal(pane._class_filter), _apply)

    def action_filter_regex(self) -> None:
        try:
            pane = self._logs_pane()
        except Exception:
            return

        def _apply(regex_text: str | None) -> None:
            if regex_text == pane._regex_text:
                return
            pane.set_filters(pane._class_filter, regex_text)

        self.push_screen(RegexModal(pane._regex_text), _apply)

    # ------------------------------------------------------------------
    # Aide
    # ------------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpOverlay())

    # ------------------------------------------------------------------
    # Supervision daemon
    # ------------------------------------------------------------------

    def action_quit_confirm(self) -> None:
        """Quitte avec confirmation si un daemon est vivant."""
        from trader.interfaces.cockpit.supervisor import daemon_vital_state, stop_daemon

        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        if vital.status != "alive":
            self.exit()
            return

        try:
            import json as _json

            _data = _json.loads((_STATE_DIR / "daemon_status.json").read_text(encoding="utf-8"))
            _pid: int | None = int(_data.get("pid")) if _data.get("pid") else None
        except Exception:
            _pid = None

        async def _on_confirm(choice: str | None) -> None:
            if choice is None:
                return
            if choice == "quit-only":
                self.exit()  # le daemon survit au cockpit (pattern superviseur)
                return
            try:
                result = stop_daemon(
                    pid_file=_STATE_DIR / "daemon.pid",
                    status_file=_STATE_DIR / "daemon_status.json",
                )
                if result.stopped:
                    self.notify(f"Daemon stopped (PID {result.pid})", severity="information")
                else:
                    self.notify("Daemon not stopped (already dead or identity mismatch)", severity="warning")
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Error stopping daemon: {exc}", severity="warning")
            finally:
                self.exit()

        self.push_screen(ConfirmQuit(pid=_pid), _on_confirm)

    def action_start_daemon(self) -> None:
        """Lance le daemon en process détaché (anti-double-lancement via daemon.pid)."""
        from trader.interfaces.cockpit.supervisor import launch_daemon

        result = launch_daemon(
            pid_file=_STATE_DIR / "daemon.pid",
            log_file=_STATE_DIR / "daemon_console.log",
            root=_ROOT,
            status_file=_STATE_DIR / "daemon_status.json",
        )
        if result.launched:
            self.notify(f"Daemon started (PID {result.pid})", severity="information")
        else:
            self.notify("Daemon already running", severity="warning")

    def action_stop_daemon_confirm(self) -> None:
        async def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            from trader.interfaces.cockpit.supervisor import stop_daemon

            result = stop_daemon(
                pid_file=_STATE_DIR / "daemon.pid",
                status_file=_STATE_DIR / "daemon_status.json",
            )
            if result.stopped:
                self.notify(f"Daemon stopped (PID {result.pid})", severity="information")
            else:
                self.notify("No daemon running", severity="warning")

        self.push_screen(ConfirmStop(), _on_confirm)

    def action_toggle_kill(self) -> None:
        kill_active = _KILL_FILE.exists()

        async def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            from trader.interfaces.cockpit.supervisor import toggle_kill_switch

            active = toggle_kill_switch(kill_file=_KILL_FILE)
            self.notify(
                "Kill switch engaged" if active else "Kill switch released",
                severity="warning" if active else "information",
            )

        self.push_screen(ConfirmKill(kill_active=kill_active), _on_confirm)


def main() -> None:
    """Lance le cockpit. Quitter avec q ou Ctrl+C.

    ``--preflight`` : force l'écran de premier lancement (prévisualisation).
    """
    import sys

    app = CockpitApp(force_preflight="--preflight" in sys.argv[1:])
    app.run()


if __name__ == "__main__":
    main()
