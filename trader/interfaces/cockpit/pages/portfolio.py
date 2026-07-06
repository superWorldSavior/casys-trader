"""portfolio — page du cockpit casys (en cours de construction)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from trader.interfaces.cockpit.pages._shared import PANEL_CSS
from trader.interfaces.ui.palette import CASYS_FAINT


class PortfolioPage(Static):
    """Page portfolio — stub, la spec vit dans le scratchpad du redesign."""

    DEFAULT_CSS = PANEL_CSS + """
    PortfolioPage { height: 100%; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        body = Static(
            Text("portfolio — under construction", style=f"italic {CASYS_FAINT}"),
            classes="casys-panel",
        )
        body.border_title = "PORTFOLIO"
        yield body

    def update_state(self, state: dict) -> None:
        pass
