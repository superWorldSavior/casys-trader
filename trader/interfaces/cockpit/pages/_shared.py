"""_shared — primitives communes aux pages du cockpit casys.

Uniquement ce que plusieurs pages consomment : message SymbolChosen,
DataTable de base (Enter → drill-down), courbe d'équité braille, CSS panel.
"""

from __future__ import annotations

from contextlib import contextmanager

from rich.console import RenderableType
from rich.text import Text
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable

from trader.interfaces.ui.palette import CASYS_ACCENT, CASYS_FAINT
from trader.support.coercion import finite_float as _safe_float

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Mini-courbe unicode à 8 niveaux. Retourne "" si aucune valeur valide."""
    clean = [_safe_float(value, default=None) for value in values]
    clean_values = [value for value in clean if value is not None]
    if not clean_values:
        return ""

    low = min(clean_values)
    high = max(clean_values)
    if high == low:
        return "▄" * len(clean_values)

    span = high - low
    last_index = len(_SPARK_BLOCKS) - 1
    blocks: list[str] = []
    for value in clean_values:
        index = int(round((value - low) / span * last_index))
        index = max(0, min(last_index, index))
        blocks.append(_SPARK_BLOCKS[index])
    return "".join(blocks)


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


class ResizeRefresh:
    """Mixin pages : re-rend depuis le dernier état connu quand la taille change.

    Débouncé (150 ms) — Textual émet un resize par étape de drag. À mixer
    AVANT Static : ``class XPage(ResizeRefresh, Static)``.
    """

    _resize_timer = None

    def on_resize(self) -> None:
        if self._resize_timer is not None:
            try:
                self._resize_timer.stop()
            except Exception:
                pass

        def _rerender() -> None:
            self._resize_timer = None
            state = getattr(self.app, "_last_state", None)
            if state is not None:
                try:
                    self.update_state(state)  # type: ignore[attr-defined]
                except Exception:
                    pass

        self._resize_timer = self.set_timer(0.15, _rerender)  # type: ignore[attr-defined]


@contextmanager
def preserve_cursor(table: DataTable):
    """Préserve le curseur (et le scroll) d'un DataTable à travers un repopulate.

    Le refresh périodique fait ``clear()`` + ``add_row`` : sans ça, la ligne
    sélectionnée saute en tête toutes les 2 s. La ligne est retrouvée par sa
    row key (stable : "SYMBOL|…") ; si elle a disparu, curseur inchangé.
    """
    key = None
    scroll_y = 0.0
    try:
        if table.row_count and table.cursor_row is not None:
            key, _ = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
            scroll_y = table.scroll_y
    except Exception:
        key = None
    yield
    if key is None:
        return
    try:
        index = table.get_row_index(key)
        table.move_cursor(row=index, animate=False)
        table.scroll_y = min(scroll_y, table.max_scroll_y)
    except Exception:
        pass  # ligne disparue (position fermée…) — curseur par défaut


def rows_available(widget, *, reserved: int = 0, minimum: int = 3) -> int:
    """Nombre de lignes de contenu qui tiennent dans la hauteur du widget.

    ``reserved`` : lignes déjà consommées (bordures/padding sont retirés par
    Textual dans content_size ; réserver headers/footnotes internes).
    Retourne au moins ``minimum`` (taille inconnue au premier rendu → défaut).
    """
    try:
        height = widget.content_size.height
    except Exception:
        height = 0
    if height <= 0:
        return minimum
    return max(minimum, height - reserved)


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
