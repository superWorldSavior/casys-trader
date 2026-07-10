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
from trader.interfaces.cockpit.pages._shared import preserve_cursor, rows_available, ResizeRefresh, PANEL_CSS, SymbolTable
from trader.interfaces.cockpit.projections.universe import (
    SymbolRowData,
    build_symbol_rows,
    venue_of_safe as _venue_of_safe,
)
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
from trader.domain.universe.user_overrides import UserOverrides
from trader.infrastructure.files.universe_config import (
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
_HOTSET_PREVIEW_LIMIT = 5
_PREVIEW_ROWS = 8  # fallback quand la hauteur est inconnue (premier rendu)

# Colonnes normales (largeur ≤ 110)
_UNIVERSE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("SYM", 10),
    ("NAME", 20),
    ("STATE", 10),
    ("POS", 3),
    ("LAST DECISION", 18),
    ("WAKE", 7),
    ("DATA", 8),
)

# Colonnes larges (largeur > 110) : NAME et LAST DECISION élargis
_UNIVERSE_COLUMNS_WIDE: tuple[tuple[str, int], ...] = (
    ("SYM", 10),
    ("NAME", 28),
    ("STATE", 10),
    ("POS", 3),
    ("LAST DECISION", 22),
    ("WAKE", 7),
    ("DATA", 8),
)


def _columns_for_width(width: int) -> tuple[tuple[str, int], ...]:
    """Retourne la définition des colonnes selon la largeur de la table."""
    return _UNIVERSE_COLUMNS_WIDE if width > 110 else _UNIVERSE_COLUMNS


def _distribute_rows(total: int, counts: list[int], minimum: int = 3) -> list[int]:
    """Distribue ``total`` lignes entre N venues proportionnellement.

    Chaque venue obtient au moins ``min(minimum, count)`` lignes. L'excédent
    est réparti proportionnellement à la demande résiduelle. Résultat plafonné
    au count réel de chaque venue (zéro gaspillage).
    """
    n = len(counts)
    if n == 0:
        return []
    # Allocation de base : min(minimum, count) pour chaque venue
    allocs = [min(minimum, c) for c in counts]
    remaining = total - sum(allocs)
    if remaining <= 0:
        return allocs
    demands = [max(0, counts[i] - allocs[i]) for i in range(n)]
    total_demand = sum(demands)
    if total_demand == 0:
        return allocs  # toutes les venues sont déjà satisfaites
    # Distribution proportionnelle
    extras = [round(demands[i] / total_demand * remaining) for i in range(n)]
    # Correction de la dérive d'arrondi sur la venue la plus grande
    diff = remaining - sum(extras)
    if diff != 0:
        idx = max(range(n), key=lambda i: counts[i])
        extras[idx] = max(0, extras[idx] + diff)
    return [allocs[i] + min(extras[i], demands[i]) for i in range(n)]


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
    """Panneau HOT-SET — aperçu borné, pas capacité métier. PUR."""
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
    top = candidates[:_HOTSET_PREVIEW_LIMIT]

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


def _pipeline_status_style(status: str) -> str:
    if status in {"ready", "success", "activated", "available"}:
        return CASYS_SUCCESS
    if status in {"fallback", "scope_mismatch", "error", "invalid", "unavailable"}:
        return CASYS_ERROR
    if status in {"waiting_brief", "degraded", "pending"}:
        return CASYS_WARNING
    return CASYS_MUTED


def _pipeline_id(component: dict, key: str = "id_short") -> str:
    value = str(component.get(key) or "").strip()
    return f" #{value}" if value else ""


def _pipeline_count(component: dict, key: str) -> int:
    return max(0, int(_safe_float(component.get(key), default=0.0) or 0.0))


def _pipeline_coverage_label(coverage: dict) -> str:
    status = str(coverage.get("status") or "—")
    covered = _safe_float(coverage.get("candidates_with_news"), default=None)
    total = _safe_float(coverage.get("candidate_count"), default=None)
    if covered is not None and total is not None:
        return f"{status} {max(0, int(covered))}/{max(0, int(total))}"
    return status


def build_universe_pipeline_panel(state: dict) -> RenderableType:
    """Current per-venue scope → brief → agent → activation lineage. PUR."""

    pipeline = f.safe_dict(state.get("universe_pipeline"))
    score_audit = f.safe_dict(state.get("radar_score_audit"))
    score_bench = f.safe_dict(state.get("radar_score_bench"))
    family_board = f.safe_dict(state.get("global_family_board"))
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=4)
    grid.add_column()

    if family_board:
        board_status = str(family_board.get("status") or "unavailable")
        coverage = f.safe_dict(family_board.get("coverage"))
        scope_count = len(coverage.get("scope_venues") or [])
        brief_count = len(coverage.get("active_brief_venues") or [])
        board_line = Text()
        board_line.append(board_status, style=_pipeline_status_style(board_status))
        board_id = str(family_board.get("board_id") or "").strip()
        board_id_short = str(family_board.get("board_id_short") or "").strip()
        if not board_id_short and board_id:
            board_id_short = board_id[-8:]
        if board_id_short:
            board_line.append(f" #{board_id_short}", style=CASYS_DIM)
        board_line.append(
            f" · scopes {scope_count}/3 · briefs {brief_count}/3",
            style=CASYS_MUTED,
        )
        board_line.append(" · context only", style=CASYS_SUCCESS)
        grid.add_row(Text("GFB", style=f"bold {CASYS_FG}"), board_line)
        grid.add_row(Text(""), Text("·", style=CASYS_FAINT))

    component_balance = f.safe_dict(score_audit.get("global_component_balance"))
    if score_audit:
        trend_share = _safe_float(component_balance.get("median_trend_share"), default=None)
        short_offsets = int(
            _safe_float(component_balance.get("short_relative_strength_offset_count"), default=0)
            or 0
        )
        short_count = int(_safe_float(component_balance.get("short_count"), default=0) or 0)
        unique_snapshots = int(
            _safe_float(
                f.safe_dict(score_bench.get("coverage")).get("unique_snapshot_count"),
                default=0,
            )
            or 0
        )
        radar_line = Text()
        radar_line.append("shadow only", style=CASYS_WARNING)
        if trend_share is not None:
            radar_line.append(f" · trend {trend_share:.0%}", style=CASYS_MUTED)
        if short_count:
            radar_line.append(f" · short offset {short_offsets}/{short_count}", style=CASYS_MUTED)
        if unique_snapshots:
            radar_line.append(f" · bench {unique_snapshots}/60", style=CASYS_DIM)
        radar_line.append(" · effect none", style=CASYS_SUCCESS)
        grid.add_row(Text("RAD", style=f"bold {CASYS_FG}"), radar_line)
        grid.add_row(Text(""), Text("·", style=CASYS_FAINT))

    for venue_index, venue in enumerate(_VENUE_ORDER):
        entry = f.safe_dict(pipeline.get(venue))
        scope = f.safe_dict(entry.get("scope"))
        scout = f.safe_dict(entry.get("scout"))
        brief = f.safe_dict(entry.get("brief"))
        agent = f.safe_dict(entry.get("agent"))
        activation = f.safe_dict(entry.get("activation"))

        scope_status = str(scope.get("status") or "pending")
        scout_status = str(scout.get("status") or "pending")
        brief_status = str(brief.get("status") or "pending")
        agent_status = str(agent.get("status") or "pending")
        activation_status = str(activation.get("status") or "pending")
        statuses = (scope_status, scout_status, brief_status, agent_status, activation_status)
        label = Text(_VENUE_DISPLAY.get(venue, venue), style=f"bold {CASYS_FG}")
        if all(status == "pending" for status in statuses):
            grid.add_row(label, Text("pipeline pending", style=CASYS_WARNING))
            if venue_index < len(_VENUE_ORDER) - 1:
                grid.add_row(Text(""), Text("·", style=CASYS_FAINT))
            continue

        scope_line = Text()
        scope_line.append("scope ", style=CASYS_FAINT)
        scope_line.append(scope_status, style=_pipeline_status_style(scope_status))
        if scope_status == "ready":
            scope_line.append(_pipeline_id(scope), style=CASYS_DIM)
            phase = str(scope.get("phase") or "legacy")
            scope_line.append(f" · {phase}", style=CASYS_MUTED)
            if phase == "preopen":
                scope_line.append(
                    _pipeline_id(scope, "parent_scope_id_short"),
                    style=CASYS_DIM,
                )
        if _pipeline_count(scope, "candidate_count"):
            scope_line.append(
                f" · {_pipeline_count(scope, 'candidate_count')} cand"
                f" / {_pipeline_count(scope, 'challenger_count')} ch",
                style=CASYS_MUTED,
            )

        scout_coverage = f.safe_dict(scout.get("coverage"))
        scout_line = Text()
        scout_line.append("scout ", style=CASYS_FAINT)
        scout_line.append(scout_status, style=_pipeline_status_style(scout_status))
        if scout_status not in {"pending", "unavailable"}:
            scout_line.append(_pipeline_id(scout), style=CASYS_DIM)
            scout_line.append(
                f" · {_pipeline_count(scout, 'challenger_count')} ch"
                f" · {_pipeline_count(scout_coverage, 'eligible_items')} elig",
                style=CASYS_MUTED,
            )

        brief_line = Text()
        brief_line.append("brief ", style=CASYS_FAINT)
        brief_line.append(brief_status, style=_pipeline_status_style(brief_status))
        brief_line.append(_pipeline_id(brief), style=CASYS_DIM)
        if brief_status not in {"pending", "unavailable"}:
            exact = brief.get("scope_match")
            exact_label = "exact" if exact is True else ("mismatch" if exact is False else "unlinked")
            coverage_status = _pipeline_coverage_label(f.safe_dict(brief.get("coverage")))
            brief_line.append(
                f" · {exact_label} · {_pipeline_count(brief, 'point_count')} pts · cov {coverage_status}",
                style=CASYS_MUTED,
            )

        agent_line = Text()
        agent_line.append("agent ", style=CASYS_FAINT)
        agent_line.append(agent_status, style=_pipeline_status_style(agent_status))
        agent_line.append(_pipeline_id(agent), style=CASYS_DIM)
        agent_line.append(
            f" · {_pipeline_count(agent, 'hotlist_count')} hot / {_pipeline_count(agent, 'challenger_count')} ch",
            style=CASYS_MUTED,
        )
        provider = str(agent.get("provider") or "").strip()
        model = str(agent.get("model") or "").strip()
        if provider or model:
            agent_line.append(f" · {provider or '—'}/{model or '—'}", style=CASYS_DIM)
        provider_fallback = str(agent.get("provider_fallback_reason") or "").strip()
        if provider_fallback:
            agent_line.append(f" · backend fallback {provider_fallback}", style=CASYS_WARNING)
        error_code = str(agent.get("error_code") or "").strip()
        if error_code:
            agent_line.append(f" · {error_code}", style=CASYS_WARNING)

        activation_line = Text()
        activation_line.append("active ", style=CASYS_FAINT)
        activation_line.append(activation_status, style=_pipeline_status_style(activation_status))
        activation_line.append(
            f" · {_pipeline_count(activation, 'hotlist_count')} hot"
            f" / {_pipeline_count(activation, 'challenger_count')} ch",
            style=CASYS_MUTED,
        )
        activation_line.append(_pipeline_id(activation, "agent_run_id_short"), style=CASYS_DIM)
        fallback_reason = str(activation.get("fallback_reason") or "").strip()
        if fallback_reason:
            activation_line.append(f" · {fallback_reason}", style=CASYS_ERROR)

        grid.add_row(label, scope_line)
        grid.add_row(Text(""), scout_line)
        grid.add_row(Text(""), brief_line)
        grid.add_row(Text(""), agent_line)
        grid.add_row(Text(""), activation_line)
        if venue_index < len(_VENUE_ORDER) - 1:
            grid.add_row(Text(""), Text("·", style=CASYS_FAINT))

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
        "the heuristic prepares candidates; the universe agent selects the hot-set — ",
        style=CASYS_MUTED,
    )
    explainer.append(f"preview shows up to {_HOTSET_PREVIEW_LIMIT}", style=CASYS_FG)

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


def _decision_cell(row: SymbolRowData) -> Text:
    if row.last_decision_action == "deciding":
        return Text(row.last_decision, style=CASYS_ACCENT)
    if row.last_decision_action == "excluded":
        return Text(row.last_decision, style=CASYS_FAINT)
    if row.last_decision == "—":
        return Text("—", style=CASYS_FAINT)

    action = row.last_decision_action
    action_style = (
        CASYS_SUCCESS
        if action == "BUY"
        else (CASYS_ERROR if action == "SELL" else CASYS_DIM)
    )
    marker = f" {action}"
    timestamp, separator, suffix = row.last_decision.partition(marker)
    if not separator:
        return Text(row.last_decision, style=action_style)

    cell = Text()
    cell.append(f"{timestamp} ", style=CASYS_DIM)
    cell.append(action, style=action_style)
    if suffix:
        cell.append(suffix, style=CASYS_DIM)
    return cell


def _symbol_row_cells(row: SymbolRowData) -> dict[str, Text]:
    state_cell = {
        "pinned": Text("⚑ pinned", style=CASYS_ACCENT),
        "banned": Text("✕ banned", style=CASYS_ERROR),
        "hot": Text("hot ●", style=CASYS_ACCENT),
        "pool": Text("pool", style=CASYS_DIM),
    }.get(row.state_label, Text(row.state_label, style=CASYS_DIM))
    pos_cell = Text(
        row.pos,
        style=(
            CASYS_SUCCESS
            if row.pos == "L"
            else (CASYS_ERROR if row.pos == "S" else CASYS_FAINT)
        ),
    )
    return {
        "SYM": Text(row.symbol, style=f"bold {CASYS_FG}"),
        "NAME": Text(row.name, style=CASYS_DIM),
        "STATE": state_cell,
        "POS": pos_cell,
        "LAST DECISION": _decision_cell(row),
        "WAKE": Text(
            row.wake_text,
            style=(
                CASYS_ACCENT
                if row.wake_urgent
                else (CASYS_DIM if row.wake_text != "—" else CASYS_FAINT)
            ),
        ),
        "DATA": Text(
            row.data_text,
            style=CASYS_WARNING if row.data_stale else CASYS_SUCCESS,
        ),
    }


def _populate_universe_table(
    table: SymbolTable,
    state: dict,
    *,
    overrides: UserOverrides,
    now: datetime,
    drops: frozenset[str] = frozenset(),
    limits_per_venue: dict[str, int] | None = None,
    name_col_width: int = 20,
) -> None:
    """Efface et repopule la DataTable depuis l'état courant.

    ``limits_per_venue`` : nombre de lignes à afficher par venue (calculé dans
    update_state via _distribute_rows). Si None, repli sur _PREVIEW_ROWS.
    ``name_col_width`` : largeur max du nom avant ellipsis (20 ou 28 selon palier).
    """
    n_cols = len(_UNIVERSE_COLUMNS) - len(drops)
    table.clear()

    rows = build_symbol_rows(
        state,
        overrides,
        now=now,
        name_col_width=name_col_width,
    )
    rows_by_venue: dict[str, list[SymbolRowData]] = {
        venue: [] for venue in _VENUE_ORDER
    }
    for row in rows:
        rows_by_venue.setdefault(row.venue, []).append(row)

    active_venues = [venue for venue in _VENUE_ORDER if rows_by_venue.get(venue)]
    open_venues = set(state.get("open_venues_list") or [])
    sessions = f.safe_dict(state.get("sessions"))
    last_venue_index = len(active_venues) - 1

    for venue_index, venue in enumerate(active_venues):
        venue_rows = rows_by_venue[venue]
        venue_display = _VENUE_DISPLAY.get(venue, venue)
        header = Text()
        header.append(f"{venue_display} — {len(venue_rows)} · ", style=CASYS_FAINT)
        if venue in open_venues:
            header.append("● open", style=CASYS_SUCCESS)
        else:
            session = f.safe_dict(sessions.get(venue))
            header.append(
                f"○ opens {str(session.get('open') or '?')}",
                style=CASYS_FAINT,
            )
        table.add_row(
            header,
            *(Text("") for _ in range(n_cols - 1)),
            key=f"—|header_{venue}",
        )

        limit = (limits_per_venue or {}).get(venue, _PREVIEW_ROWS)
        preview = venue_rows[:limit]
        remaining = venue_rows[limit:]
        for row in preview:
            cells = _symbol_row_cells(row)
            table.add_row(
                *(cell for column, cell in cells.items() if column not in drops),
                key=f"{row.symbol}|{venue}",
            )

        if remaining:
            more = Text()
            more.append(f"+ {len(remaining)} more {venue_display}", style=CASYS_FAINT)
            if venue_index == last_venue_index:
                more.append(
                    " · positions are always decided on, even out of the hot-set",
                    style=CASYS_FAINT,
                )
            table.add_row(
                more,
                *(Text("") for _ in range(n_cols - 1)),
                key=f"—|more_{venue}",
            )


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class UniversePage(ResizeRefresh, Static):
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
        height: 100%;
    }
    UniversePage #pipeline-panel { height: auto; margin-bottom: 1; }
    UniversePage #rotation-panel { height: auto; margin-bottom: 1; }
    UniversePage #hotset-panel   { height: auto; margin-bottom: 1; }
    UniversePage #overrides-panel { height: auto; }
    UniversePage .casys-panel Static { height: auto; }
    """
    )

    _last_state: dict | None = None
    _active_drops: frozenset[str] | None = None
    _active_columns: tuple | None = None
    _active_limits: dict | None = None
    _active_name_col_width: int = 20

    def _universe_path(self) -> Path:
        """Chemin vers config/universe.yaml. Patchable en test via l'app._root."""
        root = Path(getattr(self.app, "_root", Path.cwd()))
        return root / "config" / "universe.yaml"

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="universe-left", classes="casys-panel") as panel:
            panel.border_title = f"UNIVERSE — {0} symbols · 0 venues"
            yield SymbolTable(id="universe-table")
        with Vertical(id="universe-right", classes="right-col"):
            with VerticalScroll(id="pipeline-panel", classes="casys-panel") as pipeline:
                pipeline.border_title = "PIPELINE — scope › brief › agent › active"
                yield Static(id="pipeline-body")
            with VerticalScroll(id="rotation-panel", classes="casys-panel") as rot:
                rot.border_title = "ROTATION — automatic"
                yield Static(id="rotation-body")
            with VerticalScroll(id="hotset-panel", classes="casys-panel") as hs:
                hs.border_title = f"HOT-SET — preview up to {_HOTSET_PREVIEW_LIMIT}"
                yield Static(id="hotset-body")
            with VerticalScroll(id="overrides-panel", classes="casys-panel") as ov:
                ov.border_title = "OVERRIDES — yours"
                yield Static(id="overrides-body")

    # Drop en largeur décroissante : NAME (déco) puis LAST DECISION.
    # Le palier wide (>110) ne droppe rien — les colonnes sont élargies via
    # _columns_for_width ; les drops s'appliquent aux largeurs inférieures.
    _COLUMN_DROPS: tuple[tuple[int, frozenset[str]], ...] = (
        (92, frozenset()),
        (70, frozenset({"NAME"})),
        (0, frozenset({"NAME", "LAST DECISION"})),
    )
    # _active_drops et _active_columns déclarés en classe ci-dessus.

    def on_mount(self) -> None:
        self._rebuild_columns(force=True)

    def _drops_for_width(self) -> frozenset[str]:
        width = self.query_one("#universe-table", SymbolTable).size.width or 0
        if width <= 0:
            return frozenset()
        for threshold, drops in self._COLUMN_DROPS:
            if width >= threshold:
                return drops
        return self._COLUMN_DROPS[-1][1]

    def _rebuild_columns(self, *, force: bool = False) -> None:
        """Reconstruit les colonnes si le palier largeur a changé."""
        drops = self._drops_for_width()
        table = self.query_one("#universe-table", SymbolTable)
        width = table.size.width or 0
        columns = _columns_for_width(width)
        if not force and drops == self._active_drops and columns == self._active_columns:
            return
        self._active_drops = drops
        self._active_columns = columns
        table.clear(columns=True)
        for name, w in columns:
            if name not in drops:
                table.add_column(name, width=w)

    def _compute_adaptive_params(
        self, state: dict, table: SymbolTable
    ) -> tuple[dict[str, int], int]:
        """Calcule les limites par venue et la largeur du nom adaptées à la hauteur courante.

        Appelé dans update_state après _rebuild_columns — self._active_columns est à jour.
        """
        # Compter les symboles par venue
        sym_by_venue_count: dict[str, int] = {}
        for sym in list(state.get("universe_symbols") or []):
            v = _venue_of_safe(sym)
            sym_by_venue_count[v] = sym_by_venue_count.get(v, 0) + 1

        active = [v for v in _VENUE_ORDER if sym_by_venue_count.get(v, 0) > 0]
        n_headers = len(active)

        # reserved = 1 (header colonnes DataTable) + 1 par groupe venue
        total_data = rows_available(
            table,
            reserved=1 + n_headers,
            minimum=max(3, n_headers * 3),
        )
        counts_list = [sym_by_venue_count.get(v, 0) for v in active]
        allocs = _distribute_rows(total_data, counts_list, minimum=3)
        limits = dict(zip(active, allocs))

        # Largeur de la colonne NAME depuis les colonnes actives
        name_col_width = next(
            (w for nm, w in (self._active_columns or _UNIVERSE_COLUMNS) if nm == "NAME"),
            20,
        )
        return limits, name_col_width

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

        # Table — colonnes + limites adaptatives
        try:
            self._rebuild_columns()
            table = self.query_one("#universe-table", SymbolTable)
            limits, name_col_width = self._compute_adaptive_params(state, table)
            self._active_limits = limits
            self._active_name_col_width = name_col_width
            with preserve_cursor(table):
                _populate_universe_table(
                    table,
                    state,
                    overrides=overrides,
                    now=now,
                    drops=self._active_drops or frozenset(),
                    limits_per_venue=limits,
                    name_col_width=name_col_width,
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
            self.query_one("#pipeline-body", Static).update(build_universe_pipeline_panel(state))
        except Exception:
            pass
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
                self._rebuild_columns()
                table = self.query_one("#universe-table", SymbolTable)
                with preserve_cursor(table):
                    _populate_universe_table(
                        table,
                        self._last_state,
                        overrides=overrides,
                        now=now,
                        drops=self._active_drops or frozenset(),
                        limits_per_venue=self._active_limits,
                        name_col_width=self._active_name_col_width,
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
