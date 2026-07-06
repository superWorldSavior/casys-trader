"""first_run — écran preflight du premier lancement (remplace ConfirmStart).

Affiché quand ``daemon_vital_state`` = never_started et qu'aucun état
n'existe : wordmark, checks préflight réels, cartes sécurité, action « s ».
Safe default conservé : rien ne démarre sans un appui explicite sur ``s``.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_MUTED,
    CASYS_SUCCESS,
)

_WORDMARK = "█▀▀ ▄▀█ █▀ █▄█ █▀\n█▄▄ █▀█ ▄█ ░█░ ▄█"

_TAGLINE = (
    "an autonomous paper-trading agent — the strategy, indicators and wake-ups\n"
    "are defined by the LLM through its mandate, not hard-coded"
)

_IB_FOOTNOTE = (
    "market bars come from Interactive Brokers only — cycles are skipped and "
    "retried until it connects"
)


@dataclass(frozen=True)
class PreflightCheck:
    key: str
    ok: bool
    detail: str
    error_detail: str = ""  # fragment mis en évidence quand ok=False


def _check_ib_gateway(host: str, port: int, *, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_STATE_HISTORY_FILES = (
    "decisions.jsonl",
    "current_report.json",
    "last_report.json",
    "broker.json",
    "trade_plans.json",
)


def has_state_history(state_dir: Path) -> bool:
    """True si au moins un fichier d'état connu existe dans *state_dir*."""
    return any((state_dir / name).exists() for name in _STATE_HISTORY_FILES)


def preflight_checks(
    root: Path, *, state_dir: Path | None = None, skip_ib: bool = False
) -> list[PreflightCheck]:
    """Checks réels du premier lancement. Lecture seule, jamais d'exception.

    ``skip_ib=True`` — insère une ligne IB placeholder "… checking" sans faire
    le DNS/socket (utile pour rendre l'UI immédiatement puis lancer le vrai
    check dans un worker thread).
    """
    checks: list[PreflightCheck] = []

    config_dir = root / "config"
    try:
        yaml_files = sorted(p.stem for p in config_dir.glob("*.yaml"))
    except Exception:
        yaml_files = []
    names = ", ".join(yaml_files[:5]) + ("…" if len(yaml_files) > 5 else "")
    checks.append(
        PreflightCheck(
            "config",
            ok=bool(yaml_files),
            detail=f"{len(yaml_files)} files — {names}" if yaml_files else "",
            error_detail="config/ is empty — clone incomplete?",
        )
    )

    mandate_ok = (root / "mandate" / "mandate.md").exists()
    memory_ok = (root / "mandate" / "memory.md").exists()
    checks.append(
        PreflightCheck(
            "mandate",
            ok=mandate_ok and memory_ok,
            detail="mandate.md + memory.md loaded — the agent's contract",
            error_detail="mandate/mandate.md missing — the agent has no contract",
        )
    )

    state_dir = state_dir if state_dir is not None else root / "state"
    history = has_state_history(state_dir)
    checks.append(
        PreflightCheck(
            "state",
            ok=True,
            detail=(
                "existing history found — resuming"
                if history
                else "fresh — no history yet, this is a first run"
            ),
        )
    )

    if skip_ib:
        checks.append(PreflightCheck("IB Gateway", ok=True, detail="… checking"))
    else:
        host = os.getenv("CASYS_IB_HOST", "127.0.0.1")
        try:
            port = int(os.getenv("CASYS_IB_PORT", "4002"))
        except ValueError:
            port = 4002
        ib_ok = _check_ib_gateway(host, port)
        checks.append(
            PreflightCheck(
                "IB Gateway",
                ok=ib_ok,
                detail=f"{host}:{port} reachable" if ib_ok else "",
                error_detail=f"{host}:{port} unreachable — start IB Gateway / TWS and enable the API",
            )
        )
    return checks


def build_preflight_body(checks: list[PreflightCheck]) -> RenderableType:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True, width=2)
    grid.add_column(no_wrap=True, width=12)
    grid.add_column(overflow="fold")
    for check in checks:
        if check.ok:
            icon = Text("✓", style=f"bold {CASYS_SUCCESS}")
            detail = Text(check.detail, style=CASYS_DIM)
        else:
            icon = Text("✗", style=f"bold {CASYS_ERROR}")
            detail = Text()
            unreachable, _, hint = check.error_detail.partition(" — ")
            detail.append(unreachable, style=CASYS_ERROR)
            if hint:
                detail.append(f" — {hint}", style=CASYS_DIM)
        grid.add_row(icon, Text(check.key, style=CASYS_MUTED), detail)
    return Group(grid, Text(_IB_FOOTNOTE, style=CASYS_FAINT))


_SAFETY_CARDS: tuple[tuple[str, str], ...] = (
    ("DRY-RUN DEFAULT", "every run is dry until you pass --live — and live still means the paper simulator"),
    ("KILL SWITCH", "touch KILL at the repo root — or press k — blocks all orders instantly"),
    ("RISK GATE", "config/risk.yaml caps every order — the agent can't negotiate it"),
)


def build_safety_cards() -> RenderableType:
    grid = Table.grid(padding=(0, 3), expand=True)
    for _ in _SAFETY_CARDS:
        grid.add_column(ratio=1)
    grid.add_row(
        *(
            Group(Text(title, style=CASYS_FAINT), Text(body, style=CASYS_DIM))
            for title, body in _SAFETY_CARDS
        )
    )
    return grid


_GHOSTS: tuple[tuple[str, str], ...] = (
    ("POSITIONS", "no positions yet — decisions land after the first cycle"),
    ("JOURNAL", "the agent's reasoning will stream here, decision by decision"),
    ("EQUITY", "needs two cycles to draw a curve"),
)


class FirstRunScreen(Screen[None]):
    """Preflight plein écran — ``s`` démarre le daemon, rien ne démarre sans."""

    BINDINGS = [
        Binding("s", "start_daemon", "start daemon"),
        Binding("question_mark", "help", "help", show=False),
        Binding("q", "quit_app", "quit", show=False),
    ]

    DEFAULT_CSS = """
    FirstRunScreen {
        align: center top;
        background: $background;
    }
    FirstRunScreen #first-run-scroll {
        width: 100;
        height: 100%;
        padding: 2 2 0 2;
    }
    FirstRunScreen Static { height: auto; }
    FirstRunScreen .fr-block { margin-bottom: 1; }
    FirstRunScreen #preflight-panel,
    FirstRunScreen .fr-ghost {
        border: solid #332c23;
        border-title-color: #FFB86F;
        border-title-style: bold;
        padding: 0 2;
        margin-bottom: 1;
    }
    FirstRunScreen .fr-ghost {
        border: solid #26211b;
        border-title-color: #6b6157;
    }
    FirstRunScreen #first-run-footer {
        dock: bottom;
        height: 1;
        background: $panel;
        border-top: solid #2c261f;
    }
    """

    _cursor_on: bool = True

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="first-run-scroll"):
            with Center(classes="fr-block"):
                yield Static(Text(_WORDMARK, style=f"bold {CASYS_ACCENT}"))
            with Center(classes="fr-block"):
                yield Static(Text(_TAGLINE, style=CASYS_DIM, justify="center"))
            preflight = Static(id="preflight-panel")
            preflight.border_title = "PREFLIGHT"
            yield preflight
            yield Static(build_safety_cards(), classes="fr-block")
            yield Static(id="first-run-cta", classes="fr-block")
            with Vertical():
                for title, body in _GHOSTS:
                    ghost = Static(Text(body, style=f"italic {CASYS_FAINT}"), classes="fr-ghost")
                    ghost.border_title = title
                    yield ghost
        yield Static(id="first-run-footer")

    def on_mount(self) -> None:
        root: Path = getattr(self.app, "_root", Path.cwd())
        state_dir: Path | None = getattr(self.app, "_state_dir", None)
        self._resuming = has_state_history(state_dir if state_dir is not None else root / "state")
        if self._resuming:
            # resume : les ghost panels « no positions yet » seraient faux
            for ghost in self.query(".fr-ghost"):
                ghost.display = False
        # Rendu immédiat sans IB (pas de DNS/socket) ; IB mis à jour par worker thread.
        self.query_one("#preflight-panel", Static).update(
            build_preflight_body(preflight_checks(root, state_dir=state_dir, skip_ib=True))
        )
        self._render_cta()
        self._render_footer()
        self.set_interval(0.55, self._blink)
        self.run_worker(lambda: self._worker_ib_check(root, state_dir), thread=True)

    def _worker_ib_check(self, root: Path, state_dir: Path | None) -> None:
        """Thread worker — fait le check IB réel et met à jour le panel."""
        body = build_preflight_body(preflight_checks(root, state_dir=state_dir, skip_ib=False))
        try:
            self.call_from_thread(self._update_preflight_panel, body)
        except Exception:
            pass  # app déjà terminée (test teardown ou exit rapide)

    def _update_preflight_panel(self, body: object) -> None:
        self.query_one("#preflight-panel", Static).update(body)

    def _blink(self) -> None:
        self._cursor_on = not self._cursor_on
        self._render_cta()

    def _render_cta(self) -> None:
        text = Text()
        text.append("▸ press s to start the daemon", style=f"bold {CASYS_ACCENT}")
        hint = (
            " — resumes from existing state/"
            if getattr(self, "_resuming", False)
            else " — the first cycle runs dry and begins writing state/"
        )
        text.append(hint, style=CASYS_DIM)
        text.append("▮" if self._cursor_on else " ", style=CASYS_ACCENT)
        self.query_one("#first-run-cta", Static).update(text)

    def _render_footer(self) -> None:
        text = Text(" ")
        text.append("s", style=f"bold {CASYS_ACCENT}")
        text.append(" start daemon", style=CASYS_DIM)
        text.append("  │  ", style="#332c23")
        text.append("esc", style=f"bold {CASYS_ACCENT}")
        text.append(" skip · ", style=CASYS_DIM)
        text.append("?", style=f"bold {CASYS_ACCENT}")
        text.append(" help · ", style=CASYS_DIM)
        text.append("q", style=f"bold {CASYS_ACCENT}")
        text.append(" quit", style=CASYS_DIM)
        text.append("        docs: how-to/run-the-daemon.md", style=CASYS_FAINT)
        self.query_one("#first-run-footer", Static).update(text)

    def key_escape(self) -> None:
        """Ferme l'écran sans rien lancer — le cockpit normal apparaît (binding later)."""
        self.dismiss(None)

    def action_start_daemon(self) -> None:
        self.app.action_start_daemon()  # type: ignore[attr-defined]
        self.dismiss(None)

    def action_help(self) -> None:
        from trader.interfaces.cockpit.modals import HelpOverlay

        self.app.push_screen(HelpOverlay())

    def action_quit_app(self) -> None:
        self.app.exit()
