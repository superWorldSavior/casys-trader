"""health — page du cockpit casys (en cours de construction)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from trader.interfaces.cockpit.pages._shared import PANEL_CSS
from trader.interfaces.ui.palette import CASYS_FAINT


class HealthPage(Static):
    """Page health — stub, la spec vit dans le scratchpad du redesign."""

    DEFAULT_CSS = PANEL_CSS + """
    HealthPage { height: 100%; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        body = Static(
            Text("health — under construction", style=f"italic {CASYS_FAINT}"),
            classes="casys-panel",
        )
        body.border_title = "HEALTH"
        yield body

    def update_state(self, state: dict) -> None:
        pass
