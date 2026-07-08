"""Universe and market-selection Rich builders."""

from __future__ import annotations

import math

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.interfaces.ui.panels.common import _fmt_symbol_short

_ACTION_VENUES = ("TW", "EU", "US")
_MAX_HOTLIST_DISPLAY = 12


def build_selection_panel(
    venue_state: dict,
    open_venues_list: list[str],
    *,
    palette: Palette = PALETTE_DARK,
) -> "RenderableType":
    """Panneau « Sélection par marché » (PURE — ne lit aucun fichier).

    Pour chaque venue d'actions TW/EU/US :
    - badge OUVERT / fermé
    - hotlist triée par attractivité (scores) décroissante, tronquée à 12
    Tolère un état vide ou venue absente.
    """
    venues = venue_state.get("venues") if isinstance(venue_state.get("venues"), dict) else {}
    blocks: list["RenderableType"] = []

    for venue in _ACTION_VENUES:
        is_open = venue in open_venues_list
        badge_style = palette["pnl_positive"] if is_open else palette["dim"]
        badge_text = "OUVERT" if is_open else "fermé"

        venue_data = venues.get(venue) if isinstance(venues.get(venue), dict) else {}
        hotlist: list[str] = venue_data.get("hotlist") if isinstance(venue_data.get("hotlist"), list) else []  # type: ignore[assignment]
        scores: dict[str, float] = venue_data.get("scores") if isinstance(venue_data.get("scores"), dict) else {}  # type: ignore[assignment]

        # Tri par attractivité décroissante
        def _attractivity(sym: str) -> float:
            v = scores.get(sym)
            try:
                return -float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0.0

        sorted_hotlist = sorted(hotlist, key=_attractivity)[: _MAX_HOTLIST_DISPLAY]

        table = Table(box=None, show_header=True, expand=True, pad_edge=False)
        table.add_column("Symbole", style="bold", no_wrap=True)
        table.add_column("Attractivité", justify="right", no_wrap=True)

        if sorted_hotlist:
            for sym in sorted_hotlist:
                score_val = scores.get(sym)
                try:
                    score_str = f"{float(score_val):.4f}"  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    score_str = "—"
                table.add_row(sym, score_str)
        else:
            table.add_row("—", "—")

        title_text = Text.assemble(
            (venue, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (f"[{badge_text}]", badge_style),
        )
        blocks.append(Panel(table, title=title_text, border_style=palette["border_default"], expand=True))

    return Group(*blocks)


def build_universe_panel(
    universe_symbols: list[str],
    venue_state: dict,
    open_venues_list: list[str],
    company_map: dict[str, str],
    *,
    palette: Palette = PALETTE_DARK,
) -> RenderableType:
    """Panneau « Univers » regroupé par venue EU/TW/US (PURE — ne lit aucun fichier).

    Pour chaque venue non vide :
    - Titre coloré vif si ouvert, atténué si fermé.
    - Chaque symbole affiché avec son score si disponible.
    - Score coloré selon seuils : >= 0.7 positif, >= 0.4 neutre, < 0.4 atténué.

    FX ignoré — pas dans l'univers actif.
    Tolère les états vides.
    """
    from trader.market.rotation.wiring import venue_of  # import local pour éviter les cycles

    venues_data = venue_state.get("venues") if isinstance(venue_state.get("venues"), dict) else {}

    # Regrouper les symboles par venue
    by_venue: dict[str, list[str]] = {}
    for sym in universe_symbols:
        try:
            v = venue_of(sym)
        except Exception:
            v = "UNKNOWN"
        if v not in _ACTION_VENUES:
            # FX et venues inconnues ignorées
            continue
        by_venue.setdefault(v, []).append(sym)

    blocks: list[RenderableType] = []
    for venue in _ACTION_VENUES:
        syms = by_venue.get(venue)
        if not syms:
            continue

        is_open = venue in open_venues_list
        venue_style = palette["kpi_default"] if is_open else palette["dim"]
        badge = "OUVERT" if is_open else "fermé"

        # Scores disponibles dans venue_state
        venue_data = venues_data.get(venue) if isinstance(venues_data.get(venue), dict) else {}
        scores: dict = venue_data.get("scores") if isinstance(venue_data.get("scores"), dict) else {}  # type: ignore[assignment]

        table = Table(box=None, show_header=False, expand=True, pad_edge=False)
        table.add_column("sym", no_wrap=True)
        table.add_column("score", justify="right", no_wrap=True)

        for sym in syms:
            label = _fmt_symbol_short(sym, company_map)
            score_val = scores.get(sym)
            try:
                score_f = float(score_val)  # type: ignore[arg-type]
                if math.isnan(score_f):
                    raise ValueError("score NaN")
                score_str = f"{score_f:.4f}"
                if score_f >= 0.7:
                    score_style = palette["pnl_positive"]
                elif score_f >= 0.4:
                    score_style = palette["kpi_default"]
                else:
                    score_style = palette["dim"]
            except (TypeError, ValueError):
                score_str = "—"
                score_style = palette["dim"]

            table.add_row(
                Text(label, style="bold"),
                Text(score_str, style=score_style),
            )

        title_text = Text.assemble(
            (venue, f"bold {venue_style}"),
            ("  ", ""),
            (f"[{badge}]", venue_style),
        )
        blocks.append(
            Panel(table, title=title_text, border_style=palette["border_default"], expand=True)
        )

    if not blocks:
        return Panel(
            Text("univers vide", style=palette["dim"]),
            title="[bold]Univers[/bold]",
            border_style=palette["border_default"],
            expand=True,
        )

    return Group(*blocks)


__all__ = [
    "_ACTION_VENUES",
    "_MAX_HOTLIST_DISPLAY",
    "build_selection_panel",
    "build_universe_panel",
]
