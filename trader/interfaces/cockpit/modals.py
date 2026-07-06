"""modals — confirmations et overlays du cockpit casys (UI anglaise).

Safe defaults conservés : Échap / « Cancel » n'agit jamais ; les actions
destructrices (stop, kill) passent toujours par une confirmation explicite.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from trader.interfaces.cockpit.events import EventClass
from rich.text import Text

from trader.interfaces.ui.palette import CASYS_ACCENT, CASYS_DIM, CASYS_FAINT


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


class _ConfirmModal(ModalScreen[bool]):
    _confirm_button_id = ""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == self._confirm_button_id)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmStop(_ConfirmModal):
    """Confirmation d'arrêt du daemon (binding x)."""

    _confirm_button_id = "confirm-stop-yes"
    DEFAULT_CSS = _confirm_modal_css("ConfirmStop", border="$error", width=50)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Stop the daemon?\n(SIGINT — clean shutdown)")
            with Horizontal():
                yield Button("Stop", id="confirm-stop-yes", variant="error")
                yield Button("Cancel", id="confirm-stop-no", variant="default")


class ConfirmQuit(ModalScreen["str | None"]):
    """Sortie quand un daemon est vivant : le daemon peut lui survivre.

    Retourne "stop-quit" (arrêter le daemon puis quitter), "quit-only"
    (quitter, le daemon continue) ou None (annuler).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = _confirm_modal_css("ConfirmQuit", border="$warning", width=72)

    def __init__(self, pid: int | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._pid = pid

    def compose(self) -> ComposeResult:
        pid_info = f" (PID {self._pid})" if self._pid else ""
        with Vertical():
            yield Label(f"The daemon is running{pid_info} — it can survive the cockpit.")
            with Horizontal():
                yield Button("Quit, keep daemon", id="confirm-quit-only", variant="primary")
                yield Button("Stop and quit", id="confirm-quit-stop", variant="warning")
                yield Button("Cancel", id="confirm-quit-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-quit-stop":
            self.dismiss("stop-quit")
        elif event.button.id == "confirm-quit-only":
            self.dismiss("quit-only")
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmKill(_ConfirmModal):
    """Confirmation du toggle kill-switch (binding k)."""

    _confirm_button_id = "confirm-kill-yes"
    DEFAULT_CSS = _confirm_modal_css("ConfirmKill", border="$warning", width=54)

    def __init__(self, kill_active: bool, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._kill_active = kill_active

    def compose(self) -> ComposeResult:
        action = "Release" if self._kill_active else "Engage"
        with Vertical():
            yield Label(f"{action} the kill switch?\n(blocks/unblocks all orders)")
            with Horizontal():
                yield Button(action, id="confirm-kill-yes", variant="warning")
                yield Button("Cancel", id="confirm-kill-no", variant="default")


class ConfirmBanHeld(_ConfirmModal):
    """Ban d'un symbole avec position ouverte (page Universe)."""

    _confirm_button_id = "confirm-ban-yes"
    DEFAULT_CSS = _confirm_modal_css("ConfirmBanHeld", border="$warning", width=64)

    def __init__(self, symbol: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._symbol = symbol

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Ban {self._symbol} while a position is open?\n"
                "The position keeps being managed — the symbol just leaves selection."
            )
            with Horizontal():
                yield Button("Ban", id="confirm-ban-yes", variant="warning")
                yield Button("Cancel", id="confirm-ban-no", variant="default")


class ClassFilterModal(ModalScreen["set[EventClass] | None"]):
    """Toggles par classe d'événement (page Logs, binding F)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = _confirm_modal_css("ClassFilterModal", border="$primary", width=44)

    def __init__(self, active: "set[EventClass] | None", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Event classes shown")
            for event_class in EventClass:
                yield Checkbox(
                    event_class.name.lower(),
                    value=self._active is None or event_class in self._active,
                    id=f"class-{event_class.name}",
                )
            with Horizontal():
                yield Button("Apply", id="class-filter-apply", variant="primary")
                yield Button("All", id="class-filter-all", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "class-filter-all":
            self.dismiss(None)
            return
        selected = {
            event_class for event_class in EventClass if self.query_one(f"#class-{event_class.name}", Checkbox).value
        }
        self.dismiss(selected if len(selected) < len(list(EventClass)) else None)

    def action_cancel(self) -> None:
        self.dismiss(self._active)


class RegexModal(ModalScreen["str | None"]):
    """Filtre regex sur le texte des lignes du flux (binding /)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = _confirm_modal_css("RegexModal", border="$primary", width=60)

    def __init__(self, current: str | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._current = current or ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Regex filter (empty = none)")
            yield Input(value=self._current, placeholder="e.g. 2303|SELL", id="regex-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)

    def action_cancel(self) -> None:
        self.dismiss(self._current or None)


# ---------------------------------------------------------------------------
# Aide (?)
# ---------------------------------------------------------------------------

_HELP_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "NAVIGATE",
        (
            ("1-8", "switch page"),
            ("tab", "focus panels"),
            ("j/k", "move cursor / scroll"),
            ("enter", "inspect symbol · expand row · edit setting"),
            ("esc", "close modal / cancel edit"),
        ),
    ),
    (
        "DAEMON",
        (
            ("s", "start the daemon"),
            ("x", "stop the daemon (confirm)"),
            ("k", "toggle the kill switch (confirm)"),
        ),
    ),
    (
        "LOGS",
        (
            ("c", "show/hide cycle events"),
            ("f", "pause/resume follow"),
            ("F", "filter by event class"),
            ("/", "regex filter"),
        ),
    ),
    (
        "UNIVERSE",
        (
            ("p", "pin symbol — always in selection"),
            ("b", "ban symbol — never selected"),
            ("u", "undo override"),
        ),
    ),
    (
        "SETTINGS",
        (
            ("enter", "edit value"),
            ("w", "write pending changes to config/"),
            ("r", "revert all pending changes"),
        ),
    ),
    (
        "SESSION",
        (
            ("?", "this help"),
            ("q", "quit (confirm if the daemon is alive)"),
        ),
    ),
)


def build_help_text() -> Text:
    text = Text()
    for index, (group, bindings) in enumerate(_HELP_GROUPS):
        if index:
            text.append("\n\n")
        text.append(group, style=f"bold {CASYS_ACCENT}")
        for key, label in bindings:
            text.append(f"\n  {key:<6}", style=f"bold {CASYS_ACCENT}")
            text.append(f" {label}", style=CASYS_DIM)
    text.append("\n\n")
    text.append("press esc or ? to close", style=CASYS_FAINT)
    return text


class HelpOverlay(ModalScreen[None]):
    """Tous les raccourcis, groupés — le footer n'en montre que trois groupes."""

    BINDINGS = [
        Binding("escape", "close_help", "Close", show=False),
        Binding("question_mark", "close_help", "Close", show=False),
    ]
    DEFAULT_CSS = """
    HelpOverlay { align: center middle; }
    HelpOverlay VerticalScroll {
        background: $surface;
        border: solid #332c23;
        border-title-color: #FFB86F;
        border-title-style: bold;
        padding: 1 3;
        width: 64;
        height: 80%;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll() as panel:
            panel.border_title = "KEYS"
            yield Static(build_help_text())

    def action_close_help(self) -> None:
        self.dismiss(None)
