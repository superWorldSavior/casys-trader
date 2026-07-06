"""universe — page 7 du cockpit casys : tableau de l'univers + write path pin/ban.

Disposition :
    ┌──────────────────────────────────┬──────────────────────────┐
    │  UNIVERSE TABLE (1fr)            │  ROTATION (46ch fixe)    │
    │  .casys-panel, SymbolTable       │  HOT-SET                 │
    │  groupée TW→TPE / EU / US        │  OVERRIDES               │
    │  SYM/NAME/STATE/POS/LAST/WAKE/DA │                          │
    └──────────────────────────────────┴──────────────────────────┘

Builders purs : (state, ..., now) → RenderableType — testables sans Textual.
Write path    : p pin · b ban · u undo override → user_overrides.py (atomique).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.pages._shared import PANEL_CSS, SymbolTable
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_METER_EMPTY,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
)
from trader.market.rotation.user_overrides import (
    UserOverrides,
    ban_symbol,
    clear_override,
    load_user_overrides,
    pin_symbol,
)
from trader.reporting.read_models.runtime_state import _safe_float, _safe_list_of_dicts

# ---------------------------------------------------------------------------
# Constantes locales
# ---------------------------------------------------------------------------

_VENUE_ORDER = ["TW", "EU", "US"]
_VENUE_DISPLAY = {"TW": "TPE", "EU": "EU", "US": "US", "FX": "FX"}
_HOTSET_SIZE = 5
_PREVIEW_ROWS = 8  # lignes visibles par venue avant "N more"


def _venue_of_safe(symbol: str) -> str:
    """Heuristique venue depuis le suffixe yfinance. Jamais d'exception."""
    try:
        from trader.market.rotation.wiring import venue_of

        return venue_of(symbol)
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Builders PURS — (state, ..., now) → RenderableType, jamais d'I/O
# ---------------------------------------------------------------------------


class SymbolRowData(NamedTuple):
    """Données d'une ligne du tableau (pure, testable sans Textual)."""

    symbol: str
    venue: str
    name: str
    state_label: str  # "hot" | "pinned" | "pool" | "banned"
    pos: str  # "L" | "S" | "—"
    last_decision: str
    last_decision_action: str  # "BUY" | "SELL" | "HOLD" | "deciding" | "excluded" | "—"
    wake_text: str
    wake_urgent: bool  # True si < 2h → accent
    data_text: str
    data_stale: bool


def build_symbol_rows(
    state: dict,
    overrides: UserOverrides,
    *,
    now: datetime,
) -> list[SymbolRowData]:
    """Construit les données tabulaires pour la universe table. PUR."""
    universe_symbols = list(state.get("universe_symbols") or [])
    venue_state_raw = f.safe_dict(state.get("venue_state"))
    venues_raw = f.safe_dict(venue_state_raw.get("venues"))
    company_map = f.safe_dict(state.get("company_map"))
    current_symbol = str(f.safe_dict(state.get("daemon_status")).get("current_symbol") or "")
    recent = _safe_list_of_dicts(state.get("recent_decisions"))
    symbol_wakes = f.safe_dict(state.get("symbol_wakes"))
    stale_data = f.safe_dict(state.get("stale_market_data"))

    holdings_by_sym = {
        f.holding_symbol(h): h
        for h in _safe_list_of_dicts(f.safe_dict(state.get("portfolio")).get("holdings"))
    }

    # Dernière décision par symbole (ordre chronologique desc)
    last_dec_by_sym: dict[str, dict] = {}
    for row in reversed(recent):
        sym_k = str(row.get("symbol") or "")
        if sym_k and sym_k not in last_dec_by_sym:
            last_dec_by_sym[sym_k] = row

    # Première expiration de watch par symbole
    watch_by_sym: dict[str, datetime] = {}
    for watch in _safe_list_of_dicts(state.get("indicator_watches")):
        sym_w = str(watch.get("symbol") or "")
        exp = f.parse_ts(watch.get("expires_at"))
        if sym_w and exp and (sym_w not in watch_by_sym or exp < watch_by_sym[sym_w]):
            watch_by_sym[sym_w] = exp

    # Groupement par venue
    sym_by_venue: dict[str, list[str]] = {v: [] for v in _VENUE_ORDER}
    for sym in universe_symbols:
        venue = _venue_of_safe(sym)
        sym_by_venue.setdefault(venue, []).append(sym)

    rows: list[SymbolRowData] = []
    for venue in _VENUE_ORDER:
        syms = sym_by_venue.get(venue, [])
        if not syms:
            continue
        venue_info = f.safe_dict(venues_raw.get(venue))
        hotlist = set(venue_info.get("hotlist") or [])
        scores = f.safe_dict(venue_info.get("scores"))

        def _sort(s: str, _h: set = hotlist, _sc: dict = scores) -> tuple:
            return (0 if s in _h else 1, -(_safe_float(_sc.get(s), default=0.0) or 0.0))

        for sym in sorted(syms, key=_sort):
            override_status = overrides.status_of(sym)
            name = str(company_map.get(sym) or "")
            if len(name) > 20:
                name = name[:19] + "…"

            if override_status == "pinned":
                state_label = "pinned"
            elif override_status == "banned":
                state_label = "banned"
            elif sym in hotlist:
                state_label = "hot"
            else:
                state_label = "pool"

            holding = holdings_by_sym.get(sym)
            if holding:
                qty = f.holding_quantity(holding)
                pos = "L" if qty > 0 else ("S" if qty < 0 else "—")
            else:
                pos = "—"

            if sym == current_symbol:
                dec_text, dec_action = "deciding now ▸", "deciding"
            elif override_status == "banned":
                dec_text, dec_action = "excluded", "excluded"
            else:
                dec = last_dec_by_sym.get(sym)
                if dec:
                    action = str(dec.get("action") or "").upper()
                    ts = f.hhmm(dec.get("cycle_ts") or dec.get("ts"))
                    conf = _safe_float(dec.get("confidence"), default=None)
                    conf_part = f" .{int(conf * 100):02d}" if conf is not None else ""
                    dec_text = f"{ts} {action}{conf_part}"
                    dec_action = action
                else:
                    dec_text, dec_action = "—", "—"

            wake_ts_raw = symbol_wakes.get(sym)
            wake_dt = f.parse_ts(wake_ts_raw) if wake_ts_raw else watch_by_sym.get(sym)
            if wake_dt and wake_dt > now:
                wake_text = f.countdown(wake_dt, now=now)
                wake_urgent = (wake_dt - now).total_seconds() < 7200
            else:
                wake_text, wake_urgent = "—", False

            stale_entry = f.safe_dict(stale_data.get(sym))
            if stale_entry:
                age_min = _safe_float(stale_entry.get("data_age_minutes"), default=None)
                data_text = f"▲ {f.age_m(age_min)}" if age_min is not None else "▲ ?"
                data_stale = True
            else:
                data_text, data_stale = "● fresh", False

            rows.append(
                SymbolRowData(
                    symbol=sym,
                    venue=venue,
                    name=name,
                    state_label=state_label,
                    pos=pos,
                    last_decision=dec_text,
                    last_decision_action=dec_action,
                    wake_text=wake_text,
                    wake_urgent=wake_urgent,
                    data_text=data_text,
                    data_stale=data_stale,
                )
            )
    return rows


def build_overrides_panel(overrides: UserOverrides) -> RenderableType:
    """Panneau OVERRIDES — yours. PUR."""
    if not overrides.pin and not overrides.ban:
        text = Text()
        text.append("no overrides — ", style=CASYS_FAINT)
        text.append("p", style=f"bold {CASYS_ACCENT}")
        text.append(" pin · ", style=CASYS_DIM)
        text.append("b", style=f"bold {CASYS_ACCENT}")
        text.append(" ban", style=CASYS_DIM)
        return text

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=2)
    grid.add_column(no_wrap=True, width=10)
    grid.add_column(no_wrap=True)
    for sym in overrides.pin:
        grid.add_row(
            Text("⚑", style=CASYS_ACCENT),
            Text(sym, style=f"bold {CASYS_FG}"),
            Text("pinned — always in hot-set", style=CASYS_DIM),
        )
    for sym in overrides.ban:
        grid.add_row(
            Text("✕", style=CASYS_ERROR),
            Text(sym, style=f"bold {CASYS_FG}"),
            Text("banned — never selected", style=CASYS_DIM),
        )

    note = Text(
        "\nwrites the overrides block of config/universe.yaml"
        " — the radar fills the remaining slots · ",
        style=CASYS_FAINT,
    )
    note.append("u", style=CASYS_DIM)
    note.append(" undo", style=CASYS_FAINT)
    return Group(grid, note)


def build_hot_set_panel(state: dict) -> RenderableType:
    """Panneau HOT-SET — 5 · radar score. PUR."""
    venue_state_raw = f.safe_dict(state.get("venue_state"))
    venues_raw = f.safe_dict(venue_state_raw.get("venues"))

    seen: set[str] = set()
    candidates: list[tuple[str, float]] = []
    for venue in _VENUE_ORDER:
        venue_info = f.safe_dict(venues_raw.get(venue))
        scores = f.safe_dict(venue_info.get("scores"))
        for sym in venue_info.get("hotlist") or []:
            if sym not in seen:
                seen.add(sym)
                score = _safe_float(scores.get(sym), default=0.0) or 0.0
                candidates.append((sym, score))

    candidates.sort(key=lambda x: -x[1])
    top = candidates[:_HOTSET_SIZE]

    if not top:
        return Text("no active hot-set", style=f"italic {CASYS_FAINT}")

    bar_width = 15
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=10)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True, justify="right", width=4)
    for sym, score in top:
        filled = max(0, min(bar_width, round(score * bar_width)))
        bar = Text()
        bar.append("█" * filled, style=CASYS_ACCENT)
        bar.append("░" * (bar_width - filled), style=CASYS_METER_EMPTY)
        grid.add_row(
            Text(sym, style=f"bold {CASYS_FG}"),
            bar,
            Text(f"{score:.2f}", style=CASYS_MUTED),
        )
    return grid


def _next_venue_close_utc(sessions: dict, now: datetime) -> datetime | None:
    """Prochain close de venue (prochaine rotation). PUR."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    closest: datetime | None = None
    for venue, sess in sessions.items():
        if venue == "FX" or not isinstance(sess, dict):
            continue
        close_str = sess.get("close") or sess.get("close_utc")
        if not close_str:
            continue
        try:
            h, m = (int(x) for x in str(close_str).split(":"))
        except Exception:
            continue
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        if closest is None or candidate < closest:
            closest = candidate
    return closest


def build_rotation_panel(
    state: dict,
    *,
    now: datetime,
    last_rotation: dict | None = None,
) -> RenderableType:
    """Panneau ROTATION — automatic. PUR (last_rotation injecté depuis update_state)."""
    explainer = Text()
    explainer.append(
        "the radar scores the pool at each venue open and fills the hot-set — ",
        style=CASYS_MUTED,
    )
    explainer.append(f"{_HOTSET_SIZE} slots", style=CASYS_FG)
    explainer.append(", minus pins", style=CASYS_MUTED)

    sessions = f.safe_dict(state.get("sessions"))
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=13)
    grid.add_column(no_wrap=True)

    next_close = _next_venue_close_utc(sessions, now) if sessions else None
    if next_close:
        time_str = next_close.strftime("%H:%M UTC")
        cd = f.countdown(next_close, now=now)
        refresh_val = Text()
        refresh_val.append(f"{time_str} · in ", style=CASYS_ACCENT)
        refresh_val.append(cd, style=CASYS_ACCENT)
    else:
        refresh_val = Text("—", style=CASYS_FAINT)
    grid.add_row(Text("next refresh", style=CASYS_FAINT), refresh_val)

    if last_rotation and isinstance(last_rotation, dict):
        final = set(last_rotation.get("final_hot_set") or [])
        default = set(last_rotation.get("default_hot_set") or [])
        added = sorted(final - default)
        removed = sorted(default - final)
        parts: list[str] = []
        if added:
            parts.append(f"in {', '.join(added[:2])}")
        if removed:
            parts.append(f"out {', '.join(removed[:2])}")
        last_val = Text(" · ".join(parts) if parts else "no changes", style=CASYS_MUTED)
    else:
        last_val = Text("—", style=CASYS_FAINT)
    grid.add_row(Text("last", style=CASYS_FAINT), last_val)

    return Group(explainer, grid)


# ---------------------------------------------------------------------------
# Aide à la population de la DataTable (non-pure : modifie en place)
# ---------------------------------------------------------------------------


def _read_last_rotation(ledger_path: Path) -> dict | None:
    """Dernière ligne de rotation_ledger.jsonl. Tolérant."""
    try:
        content = ledger_path.read_text(encoding="utf-8")
        for line in reversed(content.strip().splitlines()):
            line = line.strip()
            if line:
                return json.loads(line)
    except Exception:
        pass
    return None


def _populate_universe_table(
    table: SymbolTable,
    state: dict,
    *,
    overrides: UserOverrides,
    now: datetime,
) -> None:
    """Efface et repopule la DataTable depuis l'état courant."""
    table.clear()

    universe_symbols = list(state.get("universe_symbols") or [])
    venue_state_raw = f.safe_dict(state.get("venue_state"))
    venues_raw = f.safe_dict(venue_state_raw.get("venues"))
    company_map = f.safe_dict(state.get("company_map"))
    current_symbol = str(f.safe_dict(state.get("daemon_status")).get("current_symbol") or "")
    recent = _safe_list_of_dicts(state.get("recent_decisions"))
    symbol_wakes = f.safe_dict(state.get("symbol_wakes"))
    stale_data = f.safe_dict(state.get("stale_market_data"))
    open_list = list(state.get("open_venues_list") or [])
    sessions = f.safe_dict(state.get("sessions"))

    holdings_by_sym = {
        f.holding_symbol(h): h
        for h in _safe_list_of_dicts(f.safe_dict(state.get("portfolio")).get("holdings"))
    }

    last_dec_by_sym: dict[str, dict] = {}
    for row in reversed(recent):
        sym_k = str(row.get("symbol") or "")
        if sym_k and sym_k not in last_dec_by_sym:
            last_dec_by_sym[sym_k] = row

    watch_by_sym: dict[str, datetime] = {}
    for watch in _safe_list_of_dicts(state.get("indicator_watches")):
        sym_w = str(watch.get("symbol") or "")
        exp = f.parse_ts(watch.get("expires_at"))
        if sym_w and exp and (sym_w not in watch_by_sym or exp < watch_by_sym[sym_w]):
            watch_by_sym[sym_w] = exp

    sym_by_venue: dict[str, list[str]] = {v: [] for v in _VENUE_ORDER}
    for sym in universe_symbols:
        venue = _venue_of_safe(sym)
        sym_by_venue.setdefault(venue, []).append(sym)

    active_venues = [v for v in _VENUE_ORDER if sym_by_venue.get(v)]
    last_venue_idx = len(active_venues) - 1

    for venue_idx, venue in enumerate(active_venues):
        syms = sym_by_venue.get(venue, [])
        venue_info = f.safe_dict(venues_raw.get(venue))
        hotlist = set(venue_info.get("hotlist") or [])
        scores = f.safe_dict(venue_info.get("scores"))

        def _sk(s: str, _h: set = hotlist, _sc: dict = scores) -> tuple:
            return (0 if s in _h else 1, -(_safe_float(_sc.get(s), default=0.0) or 0.0))

        sorted_syms = sorted(syms, key=_sk)

        # Venue header row
        venue_display = _VENUE_DISPLAY.get(venue, venue)
        is_open = venue in open_list
        header = Text()
        header.append(f"{venue_display} — {len(syms)} · ", style=CASYS_FAINT)
        if is_open:
            header.append("● open", style=CASYS_SUCCESS)
        else:
            sess = f.safe_dict(sessions.get(venue))
            open_time = str(sess.get("open") or "?")
            header.append(f"○ opens {open_time}", style=CASYS_FAINT)

        table.add_row(
            header,
            Text(""), Text(""), Text(""), Text(""), Text(""), Text(""),
            key=f"—|header_{venue}",
        )

        preview = sorted_syms[:_PREVIEW_ROWS]
        remaining = sorted_syms[_PREVIEW_ROWS:]

        for sym in preview:
            override_status = overrides.status_of(sym)
            name = str(company_map.get(sym) or "")
            if len(name) > 20:
                name = name[:19] + "…"

            if override_status == "pinned":
                state_cell = Text("⚑ pinned", style=CASYS_ACCENT)
            elif override_status == "banned":
                state_cell = Text("✕ banned", style=CASYS_ERROR)
            elif sym in hotlist:
                state_cell = Text("hot ●", style=CASYS_ACCENT)
            else:
                state_cell = Text("pool", style=CASYS_DIM)

            holding = holdings_by_sym.get(sym)
            if holding:
                qty = f.holding_quantity(holding)
                if qty > 0:
                    pos_cell = Text("L", style=CASYS_SUCCESS)
                elif qty < 0:
                    pos_cell = Text("S", style=CASYS_ERROR)
                else:
                    pos_cell = Text("—", style=CASYS_FAINT)
            else:
                pos_cell = Text("—", style=CASYS_FAINT)

            if sym == current_symbol:
                dec_cell = Text("deciding now ▸", style=CASYS_ACCENT)
            elif override_status == "banned":
                dec_cell = Text("excluded", style=CASYS_FAINT)
            else:
                dec = last_dec_by_sym.get(sym)
                if dec:
                    action = str(dec.get("action") or "").upper()
                    ts = f.hhmm(dec.get("cycle_ts") or dec.get("ts"))
                    conf = _safe_float(dec.get("confidence"), default=None)
                    aStyle = CASYS_SUCCESS if action == "BUY" else (CASYS_ERROR if action == "SELL" else CASYS_DIM)
                    dec_cell = Text()
                    dec_cell.append(f"{ts} ", style=CASYS_DIM)
                    dec_cell.append(action, style=aStyle)
                    if conf is not None:
                        dec_cell.append(f" .{int(conf * 100):02d}", style=CASYS_DIM)
                else:
                    dec_cell = Text("—", style=CASYS_FAINT)

            wake_ts_raw = symbol_wakes.get(sym)
            wake_dt = f.parse_ts(wake_ts_raw) if wake_ts_raw else watch_by_sym.get(sym)
            if wake_dt and wake_dt > now:
                cd = f.countdown(wake_dt, now=now)
                urgent = (wake_dt - now).total_seconds() < 7200
                wake_cell = Text(cd, style=CASYS_ACCENT if urgent else CASYS_DIM)
            else:
                wake_cell = Text("—", style=CASYS_FAINT)

            stale_entry = f.safe_dict(stale_data.get(sym))
            if stale_entry:
                age_min = _safe_float(stale_entry.get("data_age_minutes"), default=None)
                data_str = f"▲ {f.age_m(age_min)}" if age_min is not None else "▲ ?"
                data_cell = Text(data_str, style=CASYS_WARNING)
            else:
                data_cell = Text("● fresh", style=CASYS_SUCCESS)

            table.add_row(
                Text(sym, style=f"bold {CASYS_FG}"),
                Text(name, style=CASYS_DIM),
                state_cell,
                pos_cell,
                dec_cell,
                wake_cell,
                data_cell,
                key=f"{sym}|{venue}",
            )

        if remaining:
            more = Text()
            more.append(f"+ {len(remaining)} more {venue_display}", style=CASYS_FAINT)
            if venue_idx == last_venue_idx:
                more.append(
                    " · positions are always decided on, even out of the hot-set",
                    style=CASYS_FAINT,
                )
            table.add_row(
                more,
                Text(""), Text(""), Text(""), Text(""), Text(""), Text(""),
                key=f"—|more_{venue}",
            )


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class UniversePage(Static):
    """Page 7 — Universe : tableau de l'univers avec write path pin/ban.

    Bindings p/b/u actifs sur le symbole sous le curseur de la DataTable.
    """

    BINDINGS = [
        Binding("p", "pin_selected", "pin", show=False),
        Binding("b", "ban_selected", "ban", show=False),
        Binding("u", "undo_selected", "undo override", show=False),
    ]

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    UniversePage {
        layout: horizontal;
        height: 100%;
        padding: 1 2 0 2;
    }
    UniversePage #universe-left {
        width: 1fr;
        height: 100%;
        margin-right: 1;
    }
    UniversePage #universe-table {
        width: 100%;
        height: 100%;
    }
    UniversePage #universe-right {
        width: 46;
        height: 100%;
    }
    UniversePage #rotation-panel { height: auto; margin-bottom: 1; }
    UniversePage #hotset-panel   { height: auto; margin-bottom: 1; }
    UniversePage #overrides-panel { height: auto; }
    UniversePage .casys-panel Static { height: auto; }
    """
    )

    _last_state: dict | None = None

    def _universe_path(self) -> Path:
        """Chemin vers config/universe.yaml. Patchable en test via l'app._root."""
        root = Path(getattr(self.app, "_root", Path.cwd()))
        return root / "config" / "universe.yaml"

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="universe-left", classes="casys-panel") as panel:
            panel.border_title = f"UNIVERSE — {0} symbols · 0 venues"
            yield SymbolTable(id="universe-table")
        with Vertical(id="universe-right"):
            with VerticalScroll(id="rotation-panel", classes="casys-panel") as rot:
                rot.border_title = "ROTATION — automatic"
                yield Static(id="rotation-body")
            with VerticalScroll(id="hotset-panel", classes="casys-panel") as hs:
                hs.border_title = f"HOT-SET — {_HOTSET_SIZE} · radar score"
                yield Static(id="hotset-body")
            with VerticalScroll(id="overrides-panel", classes="casys-panel") as ov:
                ov.border_title = "OVERRIDES — yours"
                yield Static(id="overrides-body")

    def on_mount(self) -> None:
        table = self.query_one("#universe-table", SymbolTable)
        table.add_column("SYM", width=10)
        table.add_column("NAME", width=20)
        table.add_column("STATE", width=10)
        table.add_column("POS", width=3)
        table.add_column("LAST DECISION", width=18)
        table.add_column("WAKE", width=7)
        table.add_column("DATA", width=8)

    def on_key(self, event: events.Key) -> None:
        """j/k : défilement clavier dans la table (en plus des flèches)."""
        if event.key == "j":
            try:
                self.query_one("#universe-table", SymbolTable).action_cursor_down()
            except Exception:
                pass
            event.prevent_default()
        elif event.key == "k":
            try:
                self.query_one("#universe-table", SymbolTable).action_cursor_up()
            except Exception:
                pass
            event.prevent_default()

    def update_state(self, state: dict) -> None:
        """Met à jour tous les panneaux. Jamais d'exception (état partiel toléré)."""
        now = datetime.now(UTC)
        self._last_state = state

        try:
            overrides = load_user_overrides(self._universe_path())
        except Exception:
            overrides = UserOverrides()

        # Titre panneau gauche
        n_sym = len(list(state.get("universe_symbols") or []))
        active_venues = [
            v for v in _VENUE_ORDER
            if f.safe_dict(f.safe_dict(state.get("venue_state")).get("venues")).get(v)
        ]
        try:
            left = self.query_one("#universe-left", VerticalScroll)
            left.border_title = f"UNIVERSE — {n_sym} symbols · {len(active_venues)} venues"
        except Exception:
            pass

        # Table
        try:
            _populate_universe_table(
                self.query_one("#universe-table", SymbolTable),
                state,
                overrides=overrides,
                now=now,
            )
        except Exception:
            pass

        # Ledger last rotation (I/O tolérant)
        try:
            root = Path(getattr(self.app, "_root", Path.cwd()))
            last_rotation = _read_last_rotation(root / "state" / "rotation_ledger.jsonl")
        except Exception:
            last_rotation = None

        # Panneaux droits
        try:
            self.query_one("#rotation-body", Static).update(
                build_rotation_panel(state, now=now, last_rotation=last_rotation)
            )
        except Exception:
            pass
        try:
            self.query_one("#hotset-body", Static).update(build_hot_set_panel(state))
        except Exception:
            pass
        try:
            self.query_one("#overrides-body", Static).update(build_overrides_panel(overrides))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Write path — p / b / u
    # ------------------------------------------------------------------

    def _selected_symbol(self) -> str | None:
        """Symbole sous le curseur de la DataTable, ou None (header/more rows)."""
        try:
            table = self.query_one("#universe-table", SymbolTable)
            row_key, _ = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
            raw = str(row_key.value or "")
            sym = raw.split("|")[0]
            return sym if sym and sym != "—" else None
        except Exception:
            return None

    def _has_open_position(self, symbol: str) -> bool:
        """True si le symbole a une position ouverte dans le dernier état connu."""
        if not self._last_state:
            return False
        for holding in _safe_list_of_dicts(
            f.safe_dict(self._last_state.get("portfolio")).get("holdings")
        ):
            if f.holding_symbol(holding) == symbol:
                return abs(f.holding_quantity(holding)) > 0
        return False

    def _refresh_overrides_panel(self) -> None:
        """Recharge les overrides depuis le disque et re-rend le panneau."""
        try:
            overrides = load_user_overrides(self._universe_path())
        except Exception:
            overrides = UserOverrides()
        try:
            self.query_one("#overrides-body", Static).update(build_overrides_panel(overrides))
        except Exception:
            pass
        # Re-render la table pour refléter le nouvel état pin/ban
        if self._last_state is not None:
            try:
                now = datetime.now(UTC)
                _populate_universe_table(
                    self.query_one("#universe-table", SymbolTable),
                    self._last_state,
                    overrides=overrides,
                    now=now,
                )
            except Exception:
                pass

    def _apply_override(self, verb: str, write_fn, symbol: str) -> None:
        """Écrit l'override et donne un feedback EXPLICITE (succès comme échec)."""
        try:
            write_fn(self._universe_path(), symbol)
        except Exception as exc:  # noqa: BLE001 — l'échec doit être VISIBLE
            try:
                self.app.notify(f"{verb} {symbol} failed: {exc}", severity="warning")
            except Exception:
                pass
            return
        self._refresh_overrides_panel()
        try:
            self.app.notify(f"{symbol} {verb} — written to config/universe.yaml")
        except Exception:
            pass

    def action_pin_selected(self) -> None:
        """p — épingle le symbole sous le curseur."""
        symbol = self._selected_symbol()
        if not symbol:
            return
        self._apply_override("pinned", pin_symbol, symbol)

    def action_ban_selected(self) -> None:
        """b — bannit le symbole sous le curseur (modal si position ouverte)."""
        symbol = self._selected_symbol()
        if not symbol:
            return
        if self._has_open_position(symbol):
            from trader.interfaces.cockpit.modals import ConfirmBanHeld

            def _on_confirm(confirmed: bool) -> None:
                if confirmed:
                    self._apply_override("banned", ban_symbol, symbol)

            self.app.push_screen(ConfirmBanHeld(symbol), _on_confirm)
        else:
            self._apply_override("banned", ban_symbol, symbol)

    def action_undo_selected(self) -> None:
        """u — supprime l'override (pin ou ban) du symbole sous le curseur."""
        symbol = self._selected_symbol()
        if not symbol:
            return
        self._apply_override("cleared", clear_override, symbol)
