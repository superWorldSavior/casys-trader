"""health — page 5 du cockpit casys : santé des données, sources LLM, learnings.

Grille 3 colonnes égales (1fr · 1fr · 1fr) :
  Col 1 : DATA FRESHNESS · FX RATES
  Col 2 : SOURCES · LLM
  Col 3 : LEARNINGS · UNIVERSE

Builders purs : (state, *, now: datetime) → RenderableType.
Le widget HealthPage appelle tous les builders dans update_state().
"""

from __future__ import annotations

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
from trader.interfaces.ui.palette import (
    CASYS_DIM,
    CASYS_FAINT,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
)
from trader.market.rotation.wiring import venue_of
from trader.support.coercion import (
    dict_list as _safe_list_of_dicts,
    finite_float as _safe_float,
)

UTC = timezone.utc

_VENUE_DISPLAY: dict[str, str] = {"TW": "TPE", "EU": "EU", "US": "US"}
_VENUE_ORDER: tuple[str, ...] = ("TW", "EU", "US")


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------


def _symbols_by_venue(state: dict) -> dict[str, list[str]]:
    """Regroupe universe_symbols par venue (hors paires FX)."""
    by_venue: dict[str, list[str]] = {}
    for sym in state.get("universe_symbols") or []:
        try:
            v = venue_of(str(sym))
        except Exception:
            v = "US"
        if v != "FX":
            by_venue.setdefault(v, []).append(str(sym))
    return by_venue


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
    stale_data = f.safe_dict(state.get("stale_market_data"))
    by_venue = _symbols_by_venue(state)

    sorted_venues: list[str] = [v for v in _VENUE_ORDER if v in by_venue]
    sorted_venues += sorted(v for v in by_venue if v not in _VENUE_ORDER)

    if not sorted_venues:
        return Text("no symbols in universe", style=f"italic {CASYS_FAINT}")

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=5)  # venue name
    grid.add_column(no_wrap=True, width=8)  # badge
    grid.add_column(no_wrap=True)           # detail

    has_stale = False
    stale_by_venue: dict[str, list[tuple[str, float | None]]] = {}

    for venue in sorted_venues:
        syms = by_venue.get(venue, [])
        stale_syms = [s for s in syms if s in stale_data]
        total = len(syms)
        is_stale = bool(stale_syms)
        display = _VENUE_DISPLAY.get(venue, venue)

        if is_stale:
            has_stale = True
            badge = Text("▲ stale", style=CASYS_WARNING)
            ages = [f.staleness_age_m(state, s) for s in stale_syms]
            max_age = max((a for a in ages if a is not None), default=None)
            age_str = f.age_m(max_age) if max_age is not None else "?"
            detail = f"{total} symbols · {age_str} old"
            venue_style = CASYS_DIM
            stale_by_venue[venue] = [
                (s, f.staleness_age_m(state, s)) for s in stale_syms
            ]
        else:
            badge = Text("● live", style=CASYS_SUCCESS)
            detail = f"{total} symbols · fresh"
            venue_style = CASYS_SUCCESS

        grid.add_row(
            Text(display, style=venue_style),
            badge,
            Text(detail, style=CASYS_DIM),
        )

    parts: list[RenderableType] = [grid]

    # Liste individuelle des symboles stales quand la hauteur le permet
    if has_stale and stale_limit is not None:
        used = len(sorted_venues) + 1  # lignes venue + footnote
        sym_budget = max(0, stale_limit - used)
        if sym_budget >= 1:
            all_stale: list[tuple[str, float | None]] = []
            for venue in sorted_venues:
                all_stale.extend(stale_by_venue.get(venue, []))
            all_stale.sort(key=lambda x: x[1] if x[1] is not None else 0.0, reverse=True)
            shown_stale = all_stale[:sym_budget]

            sym_grid = Table.grid(padding=(0, 1))
            sym_grid.add_column(no_wrap=True, width=5)   # indent
            sym_grid.add_column(no_wrap=True, width=12)  # symbol
            sym_grid.add_column(no_wrap=True)            # age
            for sym, age in shown_stale:
                age_str = f.age_m(age) if age is not None else "?"
                sym_grid.add_row(
                    Text(""),
                    Text(sym, style=CASYS_FAINT),
                    Text(age_str, style=CASYS_WARNING),
                )
            parts.append(sym_grid)

    if has_stale:
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
    fx = f.safe_dict(state.get("fx_rates"))
    if not fx:
        return Text("fx rates unavailable", style=f"italic {CASYS_FAINT}")

    _SHOW = ("EUR", "CHF", "TWD")
    row = Text()
    first = True
    for key in _SHOW:
        rate = _safe_float(fx.get(key), default=None)
        if rate is None:
            continue
        if not first:
            row.append("    ")
        row.append(f"{key} ", style=CASYS_FAINT)
        row.append(f"{rate:.4f}", style=CASYS_MUTED)
        first = False

    if first:
        return Text("no fx rates", style=f"italic {CASYS_FAINT}")
    return row


def build_sources(state: dict, *, now: datetime) -> RenderableType:
    """SOURCES : IB gateway, yfinance, news, macro (si présente dans state).

    IB : lit CASYS_IB_HOST / CASYS_IB_PORT / CASYS_IB_CLIENT_ID (défauts 127.0.0.1 / 4002 / 17).
    macro : ligne affichée uniquement si state contient une clé macro.
    """
    ib_host = os.getenv("CASYS_IB_HOST", "127.0.0.1")
    ib_port = os.getenv("CASYS_IB_PORT", "4002")
    ib_client = os.getenv("CASYS_IB_CLIENT_ID", "17")

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=2)   # checkmark
    grid.add_column(no_wrap=True, width=11)  # nom source
    grid.add_column(no_wrap=True)            # détail

    def _add(name: str, detail: str) -> None:
        # puce NEUTRE : la source est CONFIGURÉE — le cockpit n'a pas de
        # preuve de santé live (fast fail : ne pas afficher un ✓ non prouvé)
        grid.add_row(
            Text("·", style=CASYS_DIM),
            Text(name, style=CASYS_MUTED),
            Text(detail, style=CASYS_DIM),
        )

    _add("IB gateway", f"{ib_host}:{ib_port} · id {ib_client}")
    _add("yfinance", "fallback")
    _add("news", "yahoo")

    # macro — affiché uniquement si la donnée est présente dans le state
    macro = (
        state.get("macro")
        or state.get("macro_data")
        or state.get("macro_calendar")
    )
    if macro:
        if isinstance(macro, dict):
            next_fomc = macro.get("next_fomc") or macro.get("fomc_next")
            detail = f"next FOMC {next_fomc}" if next_fomc else "available"
        else:
            detail = "available"
        _add("macro", detail)

    return grid


def build_llm(state: dict, *, now: datetime) -> RenderableType:
    """LLM : calls this cycle / total, fallbacks → on error → HOLD."""
    status = f.safe_dict(state.get("daemon_status"))
    kpis = f.safe_dict(state.get("kpis"))
    model_perf = _safe_list_of_dicts(kpis.get("model_performance"))

    calls_cycle = _safe_float(status.get("model_calls_used"), default=None)
    total_fills = sum(int(mp.get("fills") or 0) for mp in model_perf)
    total_fallbacks = sum(int(mp.get("fallbacks") or 0) for mp in model_perf)

    if calls_cycle is not None and total_fills:
        calls_str = f"{int(calls_cycle)} this cycle · {total_fills} total"
    elif calls_cycle is not None:
        calls_str = f"{int(calls_cycle)} this cycle"
    else:
        calls_str = "—"

    fallbacks_str = f"{total_fallbacks} · on error → HOLD"

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=11)
    grid.add_column(no_wrap=True)

    grid.add_row(Text("calls", style=CASYS_FAINT), Text(calls_str, style=CASYS_MUTED))
    grid.add_row(
        Text("fallbacks", style=CASYS_FAINT),
        Text(fallbacks_str, style=CASYS_MUTED),
    )
    return grid


def build_learnings(state: dict, *, now: datetime, limit: int = 3) -> RenderableType:
    """LEARNINGS : pending count · consolidation status · N dernières notes.

    ``limit`` est calculé dans update_state via rows_available (reserved=1 pour
    la headline). Minimum 3 par défaut.
    """
    pending = state.get("learnings_pending_count")
    consolidation = f.safe_dict(state.get("consolidation_status"))
    notes = _safe_list_of_dicts(state.get("learnings"))

    pending_str = str(pending) if pending is not None else "?"

    if consolidation:
        phase = str(
            consolidation.get("phase")
            or consolidation.get("status")
            or "idle"
        )
        last_ts = consolidation.get("last_run_ts") or consolidation.get("ts")
        last_str = f"   (last {f.hhmm(last_ts)})" if last_ts else ""
        status_str = f"consolidation {phase}"
    else:
        status_str = "consolidation idle"
        last_str = ""

    headline = Text()
    headline.append(f"{pending_str} raw · {status_str}", style=CASYS_MUTED)
    headline.append(last_str, style=CASYS_FAINT)

    parts: list[RenderableType] = [headline]
    for entry in notes[:limit]:
        sym = str(entry.get("symbol") or "").strip()
        note = str(entry.get("note") or "").strip()
        if not note:
            continue
        prefix = f"· {sym}: " if sym else "· "
        line = Text()
        line.append(prefix, style=CASYS_DIM)
        line.append(f.clip(note, limit=52), style=CASYS_DIM)
        parts.append(line)

    if len(parts) == 1:
        # headline seul : aucune note utilisable
        parts.append(Text("no recent learnings", style=f"italic {CASYS_FAINT}"))

    return Group(*parts)


def build_universe(state: dict, *, now: datetime) -> RenderableType:
    """UNIVERSE : total symbols · répartition par venue · hot-set."""
    by_venue = _symbols_by_venue(state)
    venue_state = f.safe_dict(state.get("venue_state"))
    venues_data = f.safe_dict(venue_state.get("venues"))

    total = sum(len(v) for v in by_venue.values())
    venue_parts: list[str] = []
    for v in _VENUE_ORDER:
        n = len(by_venue.get(v, []))
        if n:
            venue_parts.append(f"{_VENUE_DISPLAY.get(v, v)} {n}")
    for v in sorted(k for k in by_venue if k not in _VENUE_ORDER):
        venue_parts.append(f"{v} {len(by_venue[v])}")

    symbols_str = (f"{total} · " + " / ".join(venue_parts)) if venue_parts else str(total)

    hot_total = 0
    for vdata in venues_data.values():
        if isinstance(vdata, dict):
            hot_total += len(vdata.get("hotlist") or [])

    hotset_str = f"{hot_total} rotating" if hot_total else "—"

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=11)
    grid.add_column(no_wrap=True)

    grid.add_row(Text("symbols", style=CASYS_FAINT), Text(symbols_str, style=CASYS_MUTED))
    grid.add_row(Text("hot-set", style=CASYS_FAINT), Text(hotset_str, style=CASYS_MUTED))

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
            pass
        try:
            self.query_one("#fx-body", Static).update(build_fx_rates(state, now=now))
        except Exception:
            pass
        try:
            self.query_one("#sources-body", Static).update(
                build_sources(state, now=now)
            )
        except Exception:
            pass
        try:
            self.query_one("#llm-body", Static).update(build_llm(state, now=now))
        except Exception:
            pass
        try:
            self.query_one("#risk-body", Static).update(build_risk_gate(state))
        except Exception:
            pass
        try:
            self.query_one("#model-body", Static).update(build_model_panel(state))
        except Exception:
            pass
        try:
            learnings_panel = self.query_one("#learnings-h-panel", VerticalScroll)
            learnings_limit = rows_available(learnings_panel, reserved=1, minimum=3)
            self.query_one("#learnings-h-body", Static).update(
                build_learnings(state, now=now, limit=learnings_limit)
            )
        except Exception:
            pass
        try:
            self.query_one("#universe-h-body", Static).update(
                build_universe(state, now=now)
            )
        except Exception:
            pass
