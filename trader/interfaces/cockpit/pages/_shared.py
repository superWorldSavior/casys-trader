"""_shared — primitives communes aux pages du cockpit casys.

Uniquement ce que plusieurs pages consomment : message SymbolChosen,
DataTable de base (Enter → drill-down), courbe d'équité braille, CSS panel.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual.message import Message
from textual.widgets import DataTable

from trader.interfaces.ui.palette import CASYS_ACCENT, CASYS_FAINT
from trader.interfaces.ui.rich_panels import sparkline

# CSS commun d'un panneau casys : une seule couleur de bordure, l'identité
# vient du titre (bold accent). À inclure dans le DEFAULT_CSS des pages via
# la classe .casys-panel.
PANEL_CSS = """
.casys-panel {
    border: solid #332c23;
    border-title-color: #FFB86F;
    border-title-style: bold;
    padding: 0 1;
}
"""


class SymbolChosen(Message):
    """Une ligne portant un symbole a été validée (Enter)."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__()


class SymbolTable(DataTable):
    """DataTable casys : cursor row + Enter → SymbolChosen(symbole de la row key).

    Convention row key : ``"SYMBOL|…"`` — la partie avant ``|`` est le symbole.
    """

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.show_cursor = True

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        raw = str(event.row_key.value or "")
        symbol = raw.split("|", 1)[0]
        if symbol and symbol != "—":
            self.post_message(SymbolChosen(symbol))


def _hex_to_rgb(color: str) -> tuple[int, int, int] | str:
    """"#RRGGBB" → tuple RGB pour plotext ; autre valeur → inchangée."""
    text = color.strip()
    if text.startswith("#") and len(text) == 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            return text
    return text


def build_equity_chart(
    values: list[float],
    *,
    width: int = 40,
    height: int = 5,
    color: str = CASYS_ACCENT,
) -> RenderableType:
    """Courbe d'équité braille plotext (accent), fenêtre ~200 pts.

    Fallback sparkline si plotext indisponible ; message faint si <2 points.
    """
    series = [v for v in values if v == v][-200:]
    if len(series) < 2:
        return Text("needs two cycles to draw a curve", style=f"italic {CASYS_FAINT}")
    try:
        import plotext as plt

        plt.clear_figure()
        plt.theme("clear")
        plt.plotsize(width, height)
        plt.plot(list(range(len(series))), series, marker="braille", color=_hex_to_rgb(color))
        plt.xticks([])
        plt.yticks([])
        plt.frame(False)
        return Text.from_ansi(plt.build())
    except Exception:
        return Text(sparkline(series[-width:]), style=f"bold {color}")
