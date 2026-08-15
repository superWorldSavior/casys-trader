"""plans — page 4 « Plans » : ordres armés, plans de sortie, veilles.

Grille 1fr | ~46ch : colonne gauche (ARMED + EXIT PLANS) · colonne droite
(WATCHES + EXIT WATCHES + NEXT TO FIRE).

Builders PURS séparés du widget : (state, now) → renderable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.derive import next_to_fire
from trader.interfaces.cockpit.pages._shared import ResizeRefresh, PANEL_CSS, rows_available
from trader.interfaces.cockpit.projections.plans import (
    ActiveWatchesProjection,
    ArmedOrdersProjection,
    ExitPlansProjection,
    ExitWatchesProjection,
    exit_update_rejected_symbols as _exit_update_rejected_symbols,  # noqa: F401 - historical page export
    format_price as _price_fmt,  # noqa: F401 - historical page export
    project_active_watches,
    project_armed_orders,
    project_exit_plans,
    project_exit_watches,
    reviewed_status,
    stop_distance_sort_key as _stop_distance_sort_key,  # noqa: F401 - historical page export
    stop_pct as _stop_pct,  # noqa: F401 - historical page export
    take_profit_label as _tp_label,  # noqa: F401 - historical page export
)
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
)
UTC = timezone.utc
logger = logging.getLogger(__name__)

# Chips action (fonds teintés)
_BUY_CHIP = f"bold {CASYS_SUCCESS} on #272c20"
_SELL_CHIP = f"bold {CASYS_ERROR} on #2d1e19"


# ---------------------------------------------------------------------------
# Helpers purs locaux
# ---------------------------------------------------------------------------


def _reviewed_cell(plan: dict, exit_update_rejected: set[str]) -> tuple[str, str]:
    """(label, style) pour la colonne REVIEWED.

    Priorité : exit_update_rejected > last_llm_review > absent.
    """
    label, kind = reviewed_status(plan, exit_update_rejected)
    return label, _review_style(kind)


def _review_style(kind: str) -> str:
    return {
        "rejected": CASYS_WARNING,
        "reviewed": CASYS_SUCCESS,
        "none": CASYS_FAINT,
    }[kind]


# ---------------------------------------------------------------------------
# Builders purs — (state, now: datetime) → RenderableType
# ---------------------------------------------------------------------------


def build_armed(
    state: dict,
    *,
    now: datetime,
    limit: int | None = None,
    projection: ArmedOrdersProjection | None = None,
) -> RenderableType:
    """Panneau ARMED : 1 ligne par EXECUTE_ORDER watch + footnote.

    ``limit`` est calculé dans update_state via rows_available — adapter à la
    hauteur réelle du panneau.
    """
    footnote = Text(
        "armed orders execute without a new LLM call when their trigger fires"
        " — the risk gate still applies",
        style=CASYS_FAINT,
    )
    projected = projection or project_armed_orders(state, now=now)
    if not projected.rows:
        return Group(
            Text(
                "nothing armed — entries wait as EXECUTE_ORDER watches",
                style=f"italic {CASYS_FAINT}",
            ),
            Text(""),
            footnote,
        )

    total = len(projected.rows)
    shown = projected.rows[:limit] if limit is not None else projected.rows
    rows: list[RenderableType] = []
    for row in shown:
        chip_style = _BUY_CHIP if row.action == "BUY" else _SELL_CHIP
        line = Text()
        line.append(f"{row.symbol:<9}", style=f"bold {CASYS_FG}")
        qty_str = f" {row.quantity:g}" if row.quantity is not None else ""
        line.append(f" {row.action}{qty_str} ", style=chip_style)
        line.append("  ")

        if row.conditions:
            line.append("if ", style=CASYS_DIM)
            for index, condition in enumerate(row.conditions):
                if index > 0:
                    line.append(f" {row.logic} ", style=CASYS_FAINT)
                line.append(condition, style=CASYS_MUTED)
            if row.extra_condition_count:
                line.append(f" +{row.extra_condition_count}", style=CASYS_FAINT)

        if row.stop_price is not None:
            line.append("   stop attached ", style=CASYS_DIM)
            line.append(_price_fmt(row.stop_price), style=CASYS_DIM)
            if row.stop_distance_pct is not None:
                line.append(
                    f" ({row.stop_distance_pct:+.1f}%)",
                    style=CASYS_DIM,
                )

        if row.countdown not in ("—", "expired"):
            line.append("   expires ", style=CASYS_DIM)
            line.append(row.countdown, style=CASYS_ACCENT)

        rows.append(line)

    if limit is not None and total > limit:
        rows.append(Text(f"+ {total - limit} more", style=CASYS_FAINT))
    rows.append(Text(""))
    rows.append(footnote)
    return Group(*rows)


def build_exit_plans(
    state: dict,
    *,
    now: datetime,
    projection: ExitPlansProjection | None = None,
) -> RenderableType:
    """EXIT PLANS triés par distance au stop (la plus courte d'abord)."""
    del now
    projected = projection or project_exit_plans(state)

    if not projected.rows:
        return Text(
            "no exit plans — every position needs a stop",
            style=f"italic {CASYS_FAINT}",
        )

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)              # SYM
    grid.add_column(no_wrap=True, width=1)              # L/S
    grid.add_column(no_wrap=True, width=6, justify="right")   # QTY
    grid.add_column(no_wrap=True, width=8, justify="right")   # ENTRY
    grid.add_column(no_wrap=True, width=28)             # STOP (price + left + entry risk)
    grid.add_column(no_wrap=True, width=15)             # TAKE-PROFIT
    grid.add_column(no_wrap=True, width=11)             # PROTECT
    grid.add_column(no_wrap=True)                       # REVIEWED

    # En-tête colonnes
    grid.add_row(
        Text("SYM", style=CASYS_FAINT),
        Text("", style=CASYS_FAINT),
        Text("QTY", style=CASYS_FAINT, justify="right"),
        Text("ENTRY", style=CASYS_FAINT, justify="right"),
        Text("STOP", style=CASYS_FAINT),
        Text("TAKE-PROFIT", style=CASYS_FAINT),
        Text("PROTECT", style=CASYS_FAINT),
        Text("REVIEWED", style=CASYS_FAINT),
    )

    for row in projected.rows:
        side_style = CASYS_ERROR if row.side == "S" else CASYS_SUCCESS
        pct_style = CASYS_ERROR if row.exit_update_rejected else CASYS_DIM

        stop_text = Text()
        if row.stop_price is not None:
            stop_text.append(_price_fmt(row.stop_price), style=CASYS_MUTED)
            if row.stop_left_pct is not None:
                stop_text.append(f" left {row.stop_left_pct:.1f}%", style=pct_style)
            if row.stop_entry_risk_pct is not None:
                stop_text.append(
                    f" entry {row.stop_entry_risk_pct:.1f}%",
                    style=CASYS_FAINT,
                )
        else:
            stop_text.append("—", style=CASYS_DIM)

        protect_style = (
            CASYS_MUTED if row.protect_label != "—" else CASYS_DIM
        )
        review_style = _review_style(row.review_kind)

        grid.add_row(
            Text(row.symbol, style=f"bold {CASYS_FG}"),
            Text(row.side, style=side_style),
            Text(
                f"{row.quantity:g}" if row.quantity is not None else "—",
                style=CASYS_MUTED,
                justify="right",
            ),
            Text(_price_fmt(row.entry_price), style=CASYS_DIM, justify="right"),
            stop_text,
            Text(row.take_profit_label, style=CASYS_DIM),
            Text(row.protect_label, style=protect_style),
            Text(row.review_label, style=review_style),
        )

    footnote = Text()
    footnote.append("every open position carries a resolved hard stop · ", style=CASYS_FAINT)
    footnote.append("▲", style=CASYS_WARNING)
    footnote.append(" = last exit update attempt rejected by guardrails", style=CASYS_FAINT)
    return Group(grid, Text(""), footnote)


def build_exit_plans_compact(
    state: dict,
    *,
    now: datetime,
    limit: int | None = None,
    projection: ExitPlansProjection | None = None,
) -> RenderableType:
    """EXITS compact pour le playbook (colonne 46) : `sym side stop% · tp · badge`.

    Le détail complet (ENTRY/QTY/PROTECT/REVIEWED) reste dans le drill-down
    symbole (Enter). Trié par distance au stop, la plus courte d'abord.
    """
    del now
    projected = projection or project_exit_plans(state)
    if not projected.rows:
        return Text("no exit plans — every position needs a stop", style=f"italic {CASYS_FAINT}")

    shown = projected.rows if limit is None else projected.rows[:limit]

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)   # SYM
    grid.add_column(no_wrap=True, width=1)   # L/S
    grid.add_column(no_wrap=True, width=7, justify="right")  # stop %
    grid.add_column(no_wrap=True)            # tp · badge

    for row in shown:
        side_style = CASYS_ERROR if row.side == "S" else CASYS_SUCCESS
        stop_str = (
            f"{row.stop_left_pct:.1f}%"
            if row.stop_left_pct is not None
            else "—"
        )
        stop_style = (
            CASYS_ERROR
            if row.exit_update_rejected or row.stop_is_near
            else CASYS_DIM
        )
        review_style = _review_style(row.review_kind)
        tail = Text()
        tail.append(row.take_profit_label, style=CASYS_DIM)
        tail.append("  ", style=CASYS_DIM)
        tail.append(row.review_label, style=review_style)

        grid.add_row(
            Text(row.symbol, style=f"bold {CASYS_FG}"),
            Text(row.side, style=side_style),
            Text(stop_str, style=stop_style, justify="right"),
            tail,
        )

    parts: list[RenderableType] = [grid]
    if limit is not None and len(projected.rows) > limit:
        parts.append(
            Text(
                f"+ {len(projected.rows) - limit} more — enter for detail",
                style=CASYS_FAINT,
            )
        )
    return Group(*parts)


def build_watches(
    state: dict,
    *,
    now: datetime,
    limit: int | None = None,
    projection: ActiveWatchesProjection | None = None,
) -> RenderableType:
    """Panneau WATCHES : veilles non-armées avec barre TTL.

    ``limit`` adaptatif via rows_available depuis update_state (reserved=1 footnote).
    """
    projected = projection or project_active_watches(state, now=now)

    footnote = Text(
        "bar = time left on TTL · expired watches vanish silently", style=CASYS_FAINT
    )

    if not projected.rows:
        return Group(
            Text("no active watches", style=f"italic {CASYS_FAINT}"),
            footnote,
        )

    total = len(projected.rows)
    shown = projected.rows[:limit] if limit is not None else projected.rows

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)                    # SYM
    grid.add_column(no_wrap=True)                             # condition
    grid.add_column(no_wrap=True, width=10)                   # TTL bar
    grid.add_column(no_wrap=True, width=5, justify="right")   # countdown

    for row in shown:
        grid.add_row(
            Text(row.symbol, style=f"bold {CASYS_FG}"),
            Text(row.condition_label, style=CASYS_DIM),
            Text(f.ttl_bar(row.ttl_fraction, width=8), style=CASYS_ACCENT),
            Text(row.countdown, style=CASYS_ACCENT),
        )

    parts: list[RenderableType] = [grid]
    if limit is not None and total > limit:
        parts.append(Text(f"+ {total - limit} more", style=CASYS_FAINT))
    parts.append(footnote)
    return Group(*parts)


def build_exit_watches(
    state: dict,
    *,
    now: datetime,
    limit: int | None = None,
    projection: ExitWatchesProjection | None = None,
) -> RenderableType:
    """EXIT WATCHES : exit_watch de chaque trade_plan (non expiré).

    ``limit`` adaptatif via rows_available depuis update_state.
    """
    projected = projection or project_exit_watches(state, now=now)
    lines: list[RenderableType] = []
    for row in projected.rows:
        line = Text()
        line.append(f"{row.symbol:<9}", style=f"bold {CASYS_FG}")
        line.append(row.condition_label, style=CASYS_DIM)
        line.append(f"  {row.countdown}", style=CASYS_ACCENT)
        lines.append(line)

    if not lines:
        return Text("no exit watches", style=f"italic {CASYS_FAINT}")
    total = len(lines)
    shown = lines[:limit] if limit is not None else lines
    result: list[RenderableType] = list(shown)
    if limit is not None and total > limit:
        result.append(Text(f"+ {total - limit} more", style=CASYS_FAINT))
    return Group(*result)


def build_next_to_fire_plans(state: dict, *, now: datetime, limit: int = 8) -> RenderableType:
    """NEXT TO FIRE (dérivé de derive.next_to_fire) : 2 colonnes countdown | description.

    ``limit`` adaptatif via rows_available depuis update_state.
    """
    items = next_to_fire(state, now=now, limit=limit)
    if not items:
        return Text("nothing armed, nothing watched", style=f"italic {CASYS_FAINT}")

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=6, justify="right")
    grid.add_column(no_wrap=True)

    for item in items:
        if item.kind == "wake":
            display = item.detail  # "scheduler — N symbols due"
        elif item.kind == "exit":
            display = f"{item.label} exit watch"
        else:
            display = f"{item.label} {item.kind}"

        grid.add_row(
            Text(item.countdown, style=CASYS_ACCENT),
            Text(display, style=CASYS_MUTED),
        )
    return grid


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class PlansPage(ResizeRefresh, Static):
    """Page 4 — Plans : ordres armés · plans de sortie · veilles actives."""

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    PlansPage {
        layout: horizontal;
        height: 100%;
        padding: 1 2 0 2;
    }
    PlansPage #plans-left {
        width: 5fr;
        height: 100%;
        margin-right: 1;
    }
    PlansPage #plans-right {
        width: 3fr;
        min-width: 46;
        max-width: 68;
        height: 100%;
    }
    PlansPage #armed-panel    { height: auto; max-height: 14; margin-bottom: 1; }
    PlansPage #exit-plans-panel { height: 1fr; }
    PlansPage #watches-panel  { height: 1fr; margin-bottom: 1; }
    PlansPage #exit-watches-panel { height: auto; max-height: 10; margin-bottom: 1; }
    PlansPage #fire-panel     { height: 1fr; }
    PlansPage .casys-panel Static { height: auto; }
    """
    )

    def compose(self) -> ComposeResult:
        with Vertical(id="plans-left"):
            with VerticalScroll(id="armed-panel", classes="casys-panel") as armed:
                armed.border_title = "ARMED"
                yield Static(id="armed-body")
            with VerticalScroll(id="exit-plans-panel", classes="casys-panel") as ep:
                ep.border_title = "EXIT PLANS — sorted by stop distance"
                yield Static(id="exit-plans-body")
        with Vertical(id="plans-right"):
            with VerticalScroll(id="watches-panel", classes="casys-panel") as wt:
                wt.border_title = "WATCHES"
                yield Static(id="watches-body")
            with VerticalScroll(id="exit-watches-panel", classes="casys-panel") as ew:
                ew.border_title = "EXIT WATCHES"
                yield Static(id="exit-watches-body")
            with VerticalScroll(id="fire-panel", classes="casys-panel") as fire:
                fire.border_title = "NEXT TO FIRE"
                yield Static(id="fire-body")

    def update_state(self, state: dict) -> None:  # noqa: C901
        """Met à jour tous les panneaux depuis state — jamais d'exception.

        Les limites d'affichage sont calculées via rows_available(panneau) pour
        être adaptatives à la hauteur réelle du terminal. Le « + N more » n'est
        émis que quand il y a réellement plus d'éléments que de place.
        """
        now = datetime.now(UTC)
        try:
            armed_projection = project_armed_orders(state, now=now)
            panel = self.query_one("#armed-panel", VerticalScroll)
            panel.border_title = f"ARMED — {len(armed_projection.rows)}"
            armed_limit = rows_available(panel, reserved=2, minimum=3)
            self.query_one("#armed-body", Static).update(
                build_armed(
                    state,
                    now=now,
                    limit=armed_limit,
                    projection=armed_projection,
                )
            )
        except Exception:  # état partiel toléré
            logger.debug("%s update error", "plans armed", exc_info=True)

        try:
            exit_plans_projection = project_exit_plans(state)
            panel = self.query_one("#exit-plans-panel", VerticalScroll)
            panel.border_title = (
                f"EXIT PLANS — {len(exit_plans_projection.rows)} · sorted by stop distance"
                if exit_plans_projection.rows
                else "EXIT PLANS — sorted by stop distance"
            )
            self.query_one("#exit-plans-body", Static).update(
                build_exit_plans(
                    state,
                    now=now,
                    projection=exit_plans_projection,
                )
            )
        except Exception:
            logger.debug("%s update error", "plans exit plans", exc_info=True)

        try:
            watches_projection = project_active_watches(state, now=now)
            panel = self.query_one("#watches-panel", VerticalScroll)
            active_count = len(watches_projection.rows)
            panel.border_title = (
                f"WATCHES — {active_count} · wake the agent"
                if active_count
                else "WATCHES"
            )
            watches_limit = rows_available(panel, reserved=1, minimum=3)
            self.query_one("#watches-body", Static).update(
                build_watches(
                    state,
                    now=now,
                    limit=watches_limit,
                    projection=watches_projection,
                )
            )
        except Exception:
            logger.debug("%s update error", "plans watches", exc_info=True)

        try:
            ew_panel = self.query_one("#exit-watches-panel", VerticalScroll)
            ew_limit = rows_available(ew_panel, reserved=0, minimum=3)
            exit_watches_projection = project_exit_watches(state, now=now)
            self.query_one("#exit-watches-body", Static).update(
                build_exit_watches(
                    state,
                    now=now,
                    limit=ew_limit,
                    projection=exit_watches_projection,
                )
            )
        except Exception:
            logger.debug("%s update error", "plans exit watches", exc_info=True)

        try:
            fire_panel = self.query_one("#fire-panel", VerticalScroll)
            fire_limit = rows_available(fire_panel, reserved=0, minimum=3)
            self.query_one("#fire-body", Static).update(
                build_next_to_fire_plans(state, now=now, limit=fire_limit)
            )
        except Exception:
            logger.debug("%s update error", "plans next to fire", exc_info=True)
