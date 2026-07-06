"""plans — page 4 « Plans » : ordres armés, plans de sortie, veilles.

Grille 1fr | ~46ch : colonne gauche (ARMED + EXIT PLANS) · colonne droite
(WATCHES + EXIT WATCHES + NEXT TO FIRE).

Builders PURS séparés du widget : (state, now) → renderable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.derive import (
    armed_watches,
    next_to_fire,
    plain_watches,
)
from trader.interfaces.cockpit.pages._shared import ResizeRefresh, PANEL_CSS, rows_available
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
from trader.reporting.read_models.runtime_state import _safe_float, _safe_list_of_dicts

UTC = timezone.utc

# Chips action (fonds teintés)
_BUY_CHIP = f"bold {CASYS_SUCCESS} on #272c20"
_SELL_CHIP = f"bold {CASYS_ERROR} on #2d1e19"


# ---------------------------------------------------------------------------
# Helpers purs locaux
# ---------------------------------------------------------------------------


def _price_fmt(value: float | None) -> str:
    """Format prix : entier si valeur ronde, 2 décimales sinon."""
    if value is None:
        return "—"
    if value == round(value):
        return f.fmt_compact(value, decimals=0)
    return f.fmt_compact(value, decimals=2)


def _stop_pct(plan: dict, ref: float | None) -> float | None:
    """Position du stop vs prix en % brut — délègue à format.stop_distance_pct."""
    return f.stop_distance_pct(plan, ref)


def _tp_label(plan: dict) -> str:
    """TAKE-PROFIT : 'p1 → p2' / 'p1' / '—' selon les TPs du plan."""
    tps = [
        _safe_float(tp.get("price"), default=None)
        for tp in _safe_list_of_dicts(plan.get("take_profits"))
    ]
    prices = [p for p in tps if p is not None]
    if not prices:
        return "—"
    if len(prices) == 1:
        return _price_fmt(prices[0])
    return f"{_price_fmt(prices[0])} → {_price_fmt(prices[1])}"


def _amend_rejected_symbols(state: dict) -> set[str]:
    """Symboles dont la dernière tentative amend_exit enregistrée a été rejetée.

    Lit state["recent_decisions"] et cherche le tool_call le plus récent
    (par cycle_ts) avec tool=="amend_exit" par symbole.
    """
    latest: dict[str, tuple[str, str]] = {}  # sym → (cycle_ts, outcome)
    for row in _safe_list_of_dicts(state.get("recent_decisions")):
        sym = str(row.get("symbol") or "")
        if not sym:
            continue
        cycle_ts = str(row.get("cycle_ts") or row.get("ts") or "")
        runtime = f.safe_dict(row.get("runtime"))
        for call in _safe_list_of_dicts(runtime.get("tool_calls")):
            if str(call.get("tool") or "") != "amend_exit":
                continue
            outcome = str(call.get("outcome") or "")
            existing = latest.get(sym)
            if existing is None or cycle_ts >= existing[0]:
                latest[sym] = (cycle_ts, outcome)
    return {sym for sym, (_, outcome) in latest.items() if outcome == "rejected"}


def _reviewed_cell(plan: dict, amend_rejected: set[str]) -> tuple[str, str]:
    """(label, style) pour la colonne REVIEWED.

    Priorité : amend_rejected > last_llm_review > absent.
    """
    sym = str(plan.get("symbol") or "")
    if sym in amend_rejected:
        return "▲ amend rejected", CASYS_WARNING
    review = f.safe_dict(plan.get("last_llm_review"))
    ts = review.get("ts")
    if ts:
        return f"✓ {f.hhmm(ts)}", CASYS_SUCCESS
    return "—", CASYS_FAINT


def _stop_distance_sort_key(plan: dict, state: dict) -> float:
    """Clé de tri : distance absolue au stop (la plus courte = la plus vulnérable en premier)."""
    sym = str(plan.get("symbol") or "")
    ref = f.price_for_symbol(state, sym) or _safe_float(plan.get("entry_price"), default=None)
    pct = _stop_pct(plan, ref)
    return abs(pct) if pct is not None else 999.0


# ---------------------------------------------------------------------------
# Builders purs — (state, now: datetime) → RenderableType
# ---------------------------------------------------------------------------


def build_armed(state: dict, *, now: datetime, limit: int | None = None) -> RenderableType:
    """Panneau ARMED : 1 ligne par EXECUTE_ORDER watch + footnote.

    ``limit`` est calculé dans update_state via rows_available — adapter à la
    hauteur réelle du panneau.
    """
    footnote = Text(
        "armed orders execute without a new LLM call when their trigger fires"
        " — the risk gate still applies",
        style=CASYS_FAINT,
    )
    armed = armed_watches(state)
    if not armed:
        return Group(
            Text(
                "nothing armed — entries wait as EXECUTE_ORDER watches",
                style=f"italic {CASYS_FAINT}",
            ),
            Text(""),
            footnote,
        )

    total = len(armed)
    shown = armed[:limit] if limit is not None else armed
    rows: list[RenderableType] = []
    for watch in shown:
        sym = str(watch.get("symbol") or "—")
        order = f.safe_dict(watch.get("order"))
        action = str(order.get("action") or order.get("intent") or "ORDER").upper()
        qty = _safe_float(order.get("qty"), default=None)
        chip_style = _BUY_CHIP if action == "BUY" else _SELL_CHIP

        conditions = _safe_list_of_dicts(watch.get("conditions"))
        logic = str(watch.get("logic") or "and")
        stop_price = f.armed_stop_price(watch)
        ref = f.price_for_symbol(state, sym)
        cd = f.countdown(watch.get("expires_at"), now=now)

        line = Text()
        line.append(f"{sym:<9}", style=f"bold {CASYS_FG}")
        qty_str = f" {qty:g}" if qty is not None else ""
        line.append(f" {action}{qty_str} ", style=chip_style)
        line.append("  ")

        # Conditions
        if conditions:
            line.append("if ", style=CASYS_DIM)
            for i, cond in enumerate(conditions[:3]):  # max 3 conditions affichées
                if i > 0:
                    line.append(f" {logic} ", style=CASYS_FAINT)
                indicator = str(cond.get("indicator") or "?")
                op_str = str(cond.get("op") or "?")
                value = cond.get("value")
                timeframe = str(cond.get("timeframe") or cond.get("interval") or "?")
                line.append(f"{indicator} {op_str} {value} @{timeframe}", style=CASYS_MUTED)
            if len(conditions) > 3:
                line.append(f" +{len(conditions) - 3}", style=CASYS_FAINT)

        # Stop attaché
        if stop_price is not None:
            line.append("   stop attached ", style=CASYS_DIM)
            line.append(_price_fmt(stop_price), style=CASYS_DIM)
            if ref:
                pct_val = (stop_price - ref) / ref * 100.0
                line.append(f" ({pct_val:+.1f}%)", style=CASYS_DIM)

        # Expiration
        if cd not in ("—", "expired"):
            line.append("   expires ", style=CASYS_DIM)
            line.append(cd, style=CASYS_ACCENT)

        rows.append(line)

    if limit is not None and total > limit:
        rows.append(Text(f"+ {total - limit} more", style=CASYS_FAINT))
    rows.append(Text(""))
    rows.append(footnote)
    return Group(*rows)


def build_exit_plans(state: dict, *, now: datetime) -> RenderableType:
    """EXIT PLANS triés par distance au stop (la plus courte d'abord)."""
    plans = _safe_list_of_dicts(state.get("trade_plans"))
    amend_rejected = _amend_rejected_symbols(state)

    if not plans:
        return Text(
            "no exit plans — every position needs a stop",
            style=f"italic {CASYS_FAINT}",
        )

    plans_sorted = sorted(plans, key=lambda p: _stop_distance_sort_key(p, state))

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

    for plan in plans_sorted:
        sym = str(plan.get("symbol") or "—")
        side = str(plan.get("side") or "LONG").upper()
        side_char = "S" if side == "SHORT" else "L"
        side_style = CASYS_ERROR if side_char == "S" else CASYS_SUCCESS

        qty = _safe_float(
            plan.get("remaining_quantity") or plan.get("quantity"), default=None
        )
        entry = _safe_float(plan.get("entry_price"), default=None)

        # STOP : prix, distance restante, puis risque initial depuis l'entrée.
        ref = f.price_for_symbol(state, sym) or entry
        left_pct = f.stop_left_pct(plan, ref)
        entry_risk_pct = f.stop_entry_risk_pct(plan)
        rejected = sym in amend_rejected
        pct_style = CASYS_ERROR if rejected else CASYS_DIM

        stop_text = Text()
        stop_raw = _safe_float(plan.get("hard_stop_price"), default=None)
        if stop_raw is not None:
            stop_text.append(_price_fmt(stop_raw), style=CASYS_MUTED)
            if left_pct is not None:
                stop_text.append(f" left {left_pct:.1f}%", style=pct_style)
            if entry_risk_pct is not None:
                stop_text.append(f" entry {entry_risk_pct:.1f}%", style=CASYS_FAINT)
        else:
            stop_text.append("—", style=CASYS_DIM)

        # TAKE-PROFIT
        tp = _tp_label(plan)
        tp_style = CASYS_DIM

        # PROTECT
        protect = f.protect_label(plan)
        protect_style = CASYS_MUTED if protect != "—" else CASYS_DIM

        # REVIEWED
        rev_label, rev_style = _reviewed_cell(plan, amend_rejected)

        grid.add_row(
            Text(sym, style=f"bold {CASYS_FG}"),
            Text(side_char, style=side_style),
            Text(f"{qty:g}" if qty is not None else "—", style=CASYS_MUTED, justify="right"),
            Text(_price_fmt(entry), style=CASYS_DIM, justify="right"),
            stop_text,
            Text(tp, style=tp_style),
            Text(protect, style=protect_style),
            Text(rev_label, style=rev_style),
        )

    footnote = Text()
    footnote.append("every open position carries a resolved hard stop · ", style=CASYS_FAINT)
    footnote.append("▲", style=CASYS_WARNING)
    footnote.append(" = last amend attempt rejected by guardrails", style=CASYS_FAINT)
    return Group(grid, Text(""), footnote)


def build_exit_plans_compact(
    state: dict, *, now: datetime, limit: int | None = None
) -> RenderableType:
    """EXITS compact pour le playbook (colonne 46) : `sym side stop% · tp · badge`.

    Le détail complet (ENTRY/QTY/PROTECT/REVIEWED) reste dans le drill-down
    symbole (Enter). Trié par distance au stop, la plus courte d'abord.
    """
    plans = _safe_list_of_dicts(state.get("trade_plans"))
    if not plans:
        return Text("no exit plans — every position needs a stop", style=f"italic {CASYS_FAINT}")

    amend_rejected = _amend_rejected_symbols(state)
    plans_sorted = sorted(plans, key=lambda p: _stop_distance_sort_key(p, state))
    shown = plans_sorted if limit is None else plans_sorted[:limit]

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)   # SYM
    grid.add_column(no_wrap=True, width=1)   # L/S
    grid.add_column(no_wrap=True, width=7, justify="right")  # stop %
    grid.add_column(no_wrap=True)            # tp · badge

    for plan in shown:
        sym = str(plan.get("symbol") or "—")
        side = str(plan.get("side") or "LONG").upper()
        side_char = "S" if side == "SHORT" else "L"
        side_style = CASYS_ERROR if side_char == "S" else CASYS_SUCCESS

        ref = f.price_for_symbol(state, sym) or _safe_float(plan.get("entry_price"), default=None)
        left_pct = f.stop_left_pct(plan, ref)
        rejected = sym in amend_rejected
        stop_str = f"{left_pct:.1f}%" if left_pct is not None else "—"
        stop_style = CASYS_ERROR if (rejected or (left_pct is not None and abs(left_pct) <= 3.0)) else CASYS_DIM

        rev_label, rev_style = _reviewed_cell(plan, amend_rejected)
        tail = Text()
        tail.append(_tp_label(plan), style=CASYS_DIM)
        tail.append("  ", style=CASYS_DIM)
        tail.append(rev_label, style=rev_style)

        grid.add_row(
            Text(sym, style=f"bold {CASYS_FG}"),
            Text(side_char, style=side_style),
            Text(stop_str, style=stop_style, justify="right"),
            tail,
        )

    parts: list[RenderableType] = [grid]
    if limit is not None and len(plans_sorted) > limit:
        parts.append(Text(f"+ {len(plans_sorted) - limit} more — enter for detail", style=CASYS_FAINT))
    return Group(*parts)


def build_watches(state: dict, *, now: datetime, limit: int | None = None) -> RenderableType:
    """Panneau WATCHES : veilles non-armées avec barre TTL.

    ``limit`` adaptatif via rows_available depuis update_state (reserved=1 footnote).
    """
    watches = plain_watches(state)
    active = [
        w for w in watches
        if (exp := f.parse_ts(w.get("expires_at"))) is not None and exp > now
    ]

    footnote = Text(
        "bar = time left on TTL · expired watches vanish silently", style=CASYS_FAINT
    )

    if not active:
        return Group(
            Text("no active watches", style=f"italic {CASYS_FAINT}"),
            footnote,
        )

    total = len(active)
    shown = active[:limit] if limit is not None else active

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=9)                    # SYM
    grid.add_column(no_wrap=True)                             # condition
    grid.add_column(no_wrap=True, width=10)                   # TTL bar
    grid.add_column(no_wrap=True, width=5, justify="right")   # countdown

    for watch in shown:
        sym = str(watch.get("symbol") or "—")
        cond = f.condition_summary(
            watch.get("conditions"), watch.get("logic"), max_items=2, limit=32
        )
        fraction = f.ttl_fraction(watch, now=now)
        bar = f.ttl_bar(fraction, width=8)
        cd = f.countdown(watch.get("expires_at"), now=now)

        grid.add_row(
            Text(sym, style=f"bold {CASYS_FG}"),
            Text(cond, style=CASYS_DIM),
            Text(bar, style=CASYS_ACCENT),
            Text(cd, style=CASYS_ACCENT),
        )

    parts: list[RenderableType] = [grid]
    if limit is not None and total > limit:
        parts.append(Text(f"+ {total - limit} more", style=CASYS_FAINT))
    parts.append(footnote)
    return Group(*parts)


def build_exit_watches(state: dict, *, now: datetime, limit: int | None = None) -> RenderableType:
    """EXIT WATCHES : exit_watch de chaque trade_plan (non expiré).

    ``limit`` adaptatif via rows_available depuis update_state.
    """
    plans = _safe_list_of_dicts(state.get("trade_plans"))
    lines: list[RenderableType] = []

    for plan in plans:
        exit_watch = f.safe_dict(plan.get("exit_watch"))
        if not exit_watch:
            continue
        exp = f.parse_ts(exit_watch.get("expires_at"))
        if exp is not None and exp <= now:
            continue
        sym = str(plan.get("symbol") or exit_watch.get("symbol") or "—")
        cond = f.condition_summary(
            exit_watch.get("conditions"), exit_watch.get("logic"), max_items=3, limit=38
        )
        cd = f.countdown(exit_watch.get("expires_at"), now=now)

        line = Text()
        line.append(f"{sym:<9}", style=f"bold {CASYS_FG}")
        line.append(f"{cond}", style=CASYS_DIM)
        line.append(f"  {cd}", style=CASYS_ACCENT)
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
            armed = armed_watches(state)
            panel = self.query_one("#armed-panel", VerticalScroll)
            panel.border_title = f"ARMED — {len(armed)}" if armed else "ARMED — 0"
            armed_limit = rows_available(panel, reserved=2, minimum=3)
            self.query_one("#armed-body", Static).update(
                build_armed(state, now=now, limit=armed_limit)
            )
        except Exception:  # état partiel toléré
            pass

        try:
            plans = _safe_list_of_dicts(state.get("trade_plans"))
            panel = self.query_one("#exit-plans-panel", VerticalScroll)
            panel.border_title = (
                f"EXIT PLANS — {len(plans)} · sorted by stop distance"
                if plans
                else "EXIT PLANS — sorted by stop distance"
            )
            self.query_one("#exit-plans-body", Static).update(
                build_exit_plans(state, now=now)
            )
        except Exception:
            pass

        try:
            watches = plain_watches(state)
            panel = self.query_one("#watches-panel", VerticalScroll)
            active_count = sum(
                1 for w in watches
                if (exp := f.parse_ts(w.get("expires_at"))) is not None and exp > now
            )
            panel.border_title = (
                f"WATCHES — {active_count} · wake the agent"
                if active_count
                else "WATCHES"
            )
            watches_limit = rows_available(panel, reserved=1, minimum=3)
            self.query_one("#watches-body", Static).update(
                build_watches(state, now=now, limit=watches_limit)
            )
        except Exception:
            pass

        try:
            ew_panel = self.query_one("#exit-watches-panel", VerticalScroll)
            ew_limit = rows_available(ew_panel, reserved=0, minimum=3)
            self.query_one("#exit-watches-body", Static).update(
                build_exit_watches(state, now=now, limit=ew_limit)
            )
        except Exception:
            pass

        try:
            fire_panel = self.query_one("#fire-panel", VerticalScroll)
            fire_limit = rows_available(fire_panel, reserved=0, minimum=3)
            self.query_one("#fire-body", Static).update(
                build_next_to_fire_plans(state, now=now, limit=fire_limit)
            )
        except Exception:
            pass
