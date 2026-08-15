"""health — page 5 du cockpit casys : santé des données, sources LLM, learnings.

Grille 3 colonnes égales (1fr · 1fr · 1fr) :
  Col 1 : DATA FRESHNESS · FX RATES
  Col 2 : SOURCES · LLM
  Col 3 : LEARNINGS · UNIVERSE

Builders purs : (state, *, now: datetime) → RenderableType.
Le widget HealthPage appelle tous les builders dans update_state().
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.pages._shared import PANEL_CSS, ResizeRefresh, rows_available
# RISK GATE + MODEL ont déménagé ici (Rév. 3) — builders réutilisés depuis decisions
from trader.interfaces.cockpit.pages.decisions import build_model_panel, build_risk_gate
from trader.interfaces.cockpit.projections.health import (
    project_freshness,
    project_fx_rates,
    project_learnings,
    project_llm_health,
    project_sources,
    project_universe_health,
    symbols_by_venue as _symbols_by_venue,  # noqa: F401 - historical page export
)
from trader.interfaces.ui.palette import (
    CASYS_DIM,
    CASYS_FAINT,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
)
UTC = timezone.utc
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Builders purs
# ---------------------------------------------------------------------------


def build_freshness(
    state: dict, *, now: datetime, stale_limit: int | None = None
) -> RenderableType:
    """DATA FRESHNESS : un rang par venue, ● live ou ▲ stale + âge.

    Quand ``stale_limit`` (calculé par rows_available dans update_state) laisse
    de la place après les lignes de venue + la footnote, chaque symbole stale
    est listé individuellement trié par âge décroissant.
    """
    del now
    projection = project_freshness(state)

    if not projection.venues:
        return Text("no symbols in universe", style=f"italic {CASYS_FAINT}")

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=5)  # venue name
    grid.add_column(no_wrap=True, width=8)  # badge
    grid.add_column(no_wrap=True)           # detail

    for row in projection.venues:
        if row.is_stale:
            badge = Text("▲ stale", style=CASYS_WARNING)
            age_str = (
                f.age_m(row.max_age_minutes)
                if row.max_age_minutes is not None
                else "?"
            )
            detail = f"{row.symbol_count} symbols · {age_str} old"
            venue_style = CASYS_DIM
        else:
            badge = Text("● live", style=CASYS_SUCCESS)
            detail = f"{row.symbol_count} symbols · fresh"
            venue_style = CASYS_SUCCESS

        grid.add_row(
            Text(row.display_name, style=venue_style),
            badge,
            Text(detail, style=CASYS_DIM),
        )

    parts: list[RenderableType] = [grid]

    # Liste individuelle des symboles stales quand la hauteur le permet
    if projection.has_stale and stale_limit is not None:
        used = len(projection.venues) + 1  # lignes venue + footnote
        sym_budget = max(0, stale_limit - used)
        if sym_budget >= 1:
            shown_stale = projection.stale_symbols[:sym_budget]

            sym_grid = Table.grid(padding=(0, 1))
            sym_grid.add_column(no_wrap=True, width=5)   # indent
            sym_grid.add_column(no_wrap=True, width=12)  # symbol
            sym_grid.add_column(no_wrap=True)            # age
            for stale_row in shown_stale:
                age_str = (
                    f.age_m(stale_row.age_minutes)
                    if stale_row.age_minutes is not None
                    else "?"
                )
                sym_grid.add_row(
                    Text(""),
                    Text(stale_row.symbol, style=CASYS_FAINT),
                    Text(age_str, style=CASYS_WARNING),
                )
            parts.append(sym_grid)

    if projection.has_stale:
        parts.append(
            Text(
                "stale symbols are excluded from decisions and retry at session open."
                " Not an error.",
                style=f"italic {CASYS_FAINT}",
            )
        )
    return Group(*parts)


def build_fx_rates(state: dict, *, now: datetime) -> RenderableType:
    """FX RATES : EUR / CHF / TWD sur une ligne."""
    del now
    projection = project_fx_rates(state)
    if not projection.source_available:
        return Text("fx rates unavailable", style=f"italic {CASYS_FAINT}")

    if not projection.rows:
        return Text("no fx rates", style=f"italic {CASYS_FAINT}")

    row = Text()
    for index, rate_row in enumerate(projection.rows):
        if index:
            row.append("    ")
        row.append(f"{rate_row.currency} ", style=CASYS_FAINT)
        row.append(f"{rate_row.rate:.4f}", style=CASYS_MUTED)
    return row


def build_sources(state: dict, *, now: datetime) -> RenderableType:
    """SOURCES : IB gateway, yfinance, news, macro (si présente dans state).

    IB : lit CASYS_IB_HOST / CASYS_IB_PORT / CASYS_IB_CLIENT_ID (défauts 127.0.0.1 / 4002 / 17).
    macro : ligne affichée uniquement si state contient une clé macro.
    """
    del now
    projection = project_sources(
        state,
        ib_host=os.getenv("CASYS_IB_HOST", "127.0.0.1"),
        ib_port=os.getenv("CASYS_IB_PORT", "4002"),
        ib_client_id=os.getenv("CASYS_IB_CLIENT_ID", "17"),
    )

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=2)   # checkmark
    grid.add_column(no_wrap=True, width=11)  # nom source
    grid.add_column(no_wrap=True)            # détail

    for row in projection.rows:
        # puce NEUTRE : la source est CONFIGURÉE — le cockpit n'a pas de
        # preuve de santé live (fast fail : ne pas afficher un ✓ non prouvé)
        grid.add_row(
            Text("·", style=CASYS_DIM),
            Text(row.name, style=CASYS_MUTED),
            Text(row.detail, style=CASYS_DIM),
        )

    return grid


def build_llm(state: dict, *, now: datetime) -> RenderableType:
    """LLM : calls this cycle / total, fallbacks → on error → HOLD."""
    del now
    projection = project_llm_health(state)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=11)
    grid.add_column(no_wrap=True)

    grid.add_row(
        Text("calls", style=CASYS_FAINT),
        Text(projection.calls_label, style=CASYS_MUTED),
    )
    grid.add_row(
        Text("fallbacks", style=CASYS_FAINT),
        Text(projection.fallbacks_label, style=CASYS_MUTED),
    )
    return grid


def build_learnings(state: dict, *, now: datetime, limit: int = 3) -> RenderableType:
    """LEARNINGS : pending count · consolidation status · N dernières notes.

    ``limit`` est calculé dans update_state via rows_available (reserved=1 pour
    la headline). Minimum 3 par défaut.
    """
    del now
    projection = project_learnings(state)

    headline = Text()
    headline.append(
        f"{projection.pending_label} raw · {projection.consolidation_label}",
        style=CASYS_MUTED,
    )
    if projection.last_run_label:
        headline.append(
            f"   (last {projection.last_run_label})",
            style=CASYS_FAINT,
        )

    parts: list[RenderableType] = [headline]
    for row in projection.notes[:limit]:
        prefix = f"· {row.symbol}: " if row.symbol else "· "
        line = Text()
        line.append(prefix, style=CASYS_DIM)
        line.append(f.clip(row.note, limit=52), style=CASYS_DIM)
        parts.append(line)

    if len(parts) == 1:
        # headline seul : aucune note utilisable
        parts.append(Text("no recent learnings", style=f"italic {CASYS_FAINT}"))

    return Group(*parts)


def build_universe(state: dict, *, now: datetime) -> RenderableType:
    """UNIVERSE : total symbols · répartition par venue · hot-set."""
    del now
    projection = project_universe_health(state)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=11)
    grid.add_column(no_wrap=True)

    grid.add_row(
        Text("symbols", style=CASYS_FAINT),
        Text(projection.symbols_label, style=CASYS_MUTED),
    )
    grid.add_row(
        Text("hot-set", style=CASYS_FAINT),
        Text(projection.hotset_label, style=CASYS_MUTED),
    )

    return grid


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class HealthPage(ResizeRefresh, Static):
    """Page 5 — Health : fraîcheur des données, sources, LLM, learnings, univers."""

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    HealthPage {
        layout: horizontal;
        height: 100%;
        padding: 1 2 0 2;
    }
    HealthPage .health-col {
        width: 1fr;
        height: 100%;
    }
    HealthPage .health-col-mid {
        width: 1fr;
        height: 100%;
        margin: 0 1;
    }
    HealthPage .casys-panel { height: auto; margin-bottom: 1; }
    HealthPage .casys-panel Static { height: auto; }
    HealthPage #freshness-panel { height: 1fr; }
    HealthPage #learnings-h-panel { height: 1fr; }
    """
    )

    def compose(self) -> ComposeResult:
        # Colonne 1 — DATA FRESHNESS · FX RATES · RISK GATE
        with Vertical(classes="health-col"):
            with VerticalScroll(id="freshness-panel", classes="casys-panel") as p:
                p.border_title = "DATA FRESHNESS"
                yield Static(id="freshness-body")
            with VerticalScroll(id="fx-panel", classes="casys-panel") as p:
                p.border_title = "FX RATES"
                yield Static(id="fx-body")
            with VerticalScroll(id="risk-panel", classes="casys-panel") as p:
                p.border_title = "RISK GATE"
                yield Static(id="risk-body")
        # Colonne 2 — SOURCES · LLM · MODEL
        with Vertical(classes="health-col-mid"):
            with VerticalScroll(id="sources-panel", classes="casys-panel") as p:
                p.border_title = "SOURCES"
                yield Static(id="sources-body")
            with VerticalScroll(id="llm-panel", classes="casys-panel") as p:
                p.border_title = "LLM"
                yield Static(id="llm-body")
            with VerticalScroll(id="model-panel", classes="casys-panel") as p:
                p.border_title = "MODEL"
                yield Static(id="model-body")
        # Colonne 3 — LEARNINGS · UNIVERSE
        with Vertical(classes="health-col"):
            with VerticalScroll(id="learnings-h-panel", classes="casys-panel") as p:
                p.border_title = "LEARNINGS"
                yield Static(id="learnings-h-body")
            with VerticalScroll(id="universe-h-panel", classes="casys-panel") as p:
                p.border_title = "UNIVERSE"
                yield Static(id="universe-h-body")

    def update_state(self, state: dict) -> None:  # noqa: PLR0912
        """Met à jour tous les panneaux. Les limites adaptatives sont calculées
        via rows_available pour FRESHNESS (1fr → liste les stales individuels
        quand il y a de la place) et LEARNINGS (1fr → N notes selon hauteur).
        """
        now = datetime.now(UTC)
        try:
            freshness_panel = self.query_one("#freshness-panel", VerticalScroll)
            freshness_limit = rows_available(freshness_panel, reserved=0, minimum=3)
            self.query_one("#freshness-body", Static).update(
                build_freshness(state, now=now, stale_limit=freshness_limit)
            )
        except Exception:
            logger.debug("%s update error", "health freshness", exc_info=True)
        try:
            self.query_one("#fx-body", Static).update(build_fx_rates(state, now=now))
        except Exception:
            logger.debug("%s update error", "health fx", exc_info=True)
        try:
            self.query_one("#sources-body", Static).update(
                build_sources(state, now=now)
            )
        except Exception:
            logger.debug("%s update error", "health sources", exc_info=True)
        try:
            self.query_one("#llm-body", Static).update(build_llm(state, now=now))
        except Exception:
            logger.debug("%s update error", "health llm", exc_info=True)
        try:
            self.query_one("#risk-body", Static).update(build_risk_gate(state))
        except Exception:
            logger.debug("%s update error", "health risk", exc_info=True)
        try:
            self.query_one("#model-body", Static).update(build_model_panel(state))
        except Exception:
            logger.debug("%s update error", "health model", exc_info=True)
        try:
            learnings_panel = self.query_one("#learnings-h-panel", VerticalScroll)
            learnings_limit = rows_available(learnings_panel, reserved=1, minimum=3)
            self.query_one("#learnings-h-body", Static).update(
                build_learnings(state, now=now, limit=learnings_limit)
            )
        except Exception:
            logger.debug("%s update error", "health learnings", exc_info=True)
        try:
            self.query_one("#universe-h-body", Static).update(
                build_universe(state, now=now)
            )
        except Exception:
            logger.debug("%s update error", "health universe", exc_info=True)
