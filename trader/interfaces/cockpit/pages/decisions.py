"""decisions — page 3 du cockpit casys.

LEDGER — table des 50 dernières décisions, filtrées (a/b/s/h) et groupées
par batch infra_hold. Panneau DETAIL sous la table (Enter toggle).
Droite — MIX 24h · RISK GATE · MODEL.

Adaptations vs mockup §3.2 :
- Expansion en place → panneau #detail-scroll (VerticalScroll + casys-panel)
  sous la table. Plus robuste que l'insertion de lignes dans le DataTable
  Textual, qui ne supporte pas l'insertion en position arbitraire.
- Filtres a/b/s/h : BINDINGS sur DecisionsPage ; les touches non consommées
  par le DataTable remontent à ce widget via la chaîne de focus Textual.
- Latence LLM absente du pipeline acpx (§1.12 map-read-model) → non affichée.
  Panneau MODEL : "avg rounds" (nb moyen de tours d'outils) à la place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.pages._shared import PANEL_CSS, ResizeRefresh, preserve_cursor
from trader.interfaces.cockpit.pages.plans import (
    build_armed,
    build_exit_plans_compact,
    build_exit_watches,
    build_next_to_fire_plans,
    build_watches,
)
from trader.interfaces.cockpit.projections.decisions import (
    build_ledger_rows,
    format_confidence as _fmt_conf,
    has_decision_detail as _has_detail,  # noqa: F401 - historical page helper
    project_decision_ledger,
)
from trader.interfaces.cockpit.projections.plans import (
    project_active_watches,
    project_armed_orders,
    project_exit_plans,
    project_exit_watches,
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
from trader.reporting.read_models.decision_filters import is_risk_row as _is_risk_row
from trader.support.coercion import (
    dict_list as _safe_list_of_dicts,
    finite_float as _safe_float,
)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Risk caps — lecture tolérante de config/risk.yaml
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RISK_YAML = _REPO_ROOT / "config" / "risk.yaml"


def _load_risk_caps() -> dict:
    """Lit config/risk.yaml de manière tolérante ; retourne {} en cas d'erreur."""
    try:
        data = yaml.safe_load(_RISK_YAML.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Helpers UI locaux
# ---------------------------------------------------------------------------


def _safe_str(value: object) -> str:
    return str(value or "")


# ---------------------------------------------------------------------------
# Builders purs (state, now → RenderableType)
# ---------------------------------------------------------------------------


def build_filter_chips(counts: dict[str, int], active_filter: str) -> Text:
    """Ligne chips de filtre : all/buy/sell/hold/risk/stale + hint regex."""
    text = Text()

    def _chip(key: str, color: str) -> None:
        count = counts.get(key, 0)
        label = f" {key} {count} "
        if active_filter == key:
            text.append(label, style=f"bold {CASYS_ACCENT}")
        else:
            text.append(label, style=color)
        text.append("  ")

    _chip("all", CASYS_MUTED)
    _chip("buy", CASYS_SUCCESS)
    _chip("sell", CASYS_ERROR)
    _chip("hold", CASYS_DIM)
    _chip("risk", CASYS_DIM)
    _chip("stale", CASYS_DIM)
    text.append("/ regex — press /", style=f"italic {CASYS_FAINT}")
    return text


def build_mix_24h(state: dict, *, now: datetime | None = None) -> RenderableType:
    """Barres buy/sell/hold des décisions des dernières 24 h."""
    from datetime import timedelta

    rows = _safe_list_of_dicts(state.get("recent_decisions"))
    if now is not None:
        floor = now - timedelta(hours=24)
        rows = [
            r
            for r in rows
            if (ts := f.parse_ts(r.get("cycle_ts") or r.get("ts"))) is not None and ts >= floor
        ]
    total = max(len(rows), 1)
    buy = sum(1 for r in rows if _safe_str(r.get("action")).upper() == "BUY")
    sell = sum(1 for r in rows if _safe_str(r.get("action")).upper() == "SELL")
    hold = sum(1 for r in rows if _safe_str(r.get("action")).upper() == "HOLD")

    bar_w = 20
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=5)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True, justify="right", width=4)

    def _bar(label: str, count: int, style: str) -> None:
        filled = max(0, min(bar_w, round(count / total * bar_w)))
        bar = "█" * filled + "░" * (bar_w - filled)
        grid.add_row(
            Text(label, style=style),
            Text(bar, style=style),
            Text(str(count), style=CASYS_MUTED),
        )

    _bar("buy", buy, CASYS_SUCCESS)
    _bar("sell", sell, CASYS_ERROR)
    _bar("hold", hold, CASYS_DIM)

    note = Text(
        "\nholding is a decision — most cycles end quiet by design",
        style=CASYS_FAINT,
    )
    return Group(grid, note)


def build_risk_gate(state: dict) -> RenderableType:
    """Rejets récents + TOUTES les caps de config/risk.yaml (pas de troncature).

    Chaque clé de risk.yaml est affichée avec un label lisible et un formatage
    adapté ($ pour les notionnels, % pour les seuils, on/off pour les booleans).
    """
    rows = _safe_list_of_dicts(state.get("recent_decisions"))
    risk_n = sum(1 for r in rows if _is_risk_row(r))

    parts: list[RenderableType] = []
    if risk_n == 0:
        parts.append(Text("✓ no rejects in last 50 decisions", style=CASYS_SUCCESS))
    else:
        parts.append(
            Text(f"▲ {risk_n} risk reject{'s' if risk_n > 1 else ''}", style=CASYS_WARNING)
        )

    caps = _load_risk_caps()

    _NICE_LABELS: dict[str, str] = {
        "max_gross_exposure": "gross cap",
        "max_position_value": "per-symbol cap",
        "max_order_value": "order max",
        "max_risk_per_trade_pct": "risk/trade",
        "min_equity": "min equity",
        "confidence_gate_enabled": "conf gate",
        "require_hard_stop": "hard stop req",
        "min_trade_confidence": "min conf",
        "full_risk_confidence": "full-risk conf",
    }
    _DOLLAR_KEYS = {"max_gross_exposure", "max_position_value", "max_order_value", "min_equity"}
    _PCT_KEYS = {"max_risk_per_trade_pct", "min_trade_confidence", "full_risk_confidence"}

    if caps:
        parts.append(Text("active caps  (config/risk.yaml)", style=CASYS_FAINT))
        cap_grid = Table.grid(padding=(0, 1))
        cap_grid.add_column(no_wrap=True, width=16)
        cap_grid.add_column(no_wrap=True)
        for key, value in caps.items():
            label = _NICE_LABELS.get(key, key.replace("_", " "))
            if isinstance(value, bool):
                val_str = "on" if value else "off"
                val_style = CASYS_SUCCESS if value else CASYS_DIM
            elif key in _DOLLAR_KEYS:
                val_str = f"${(_safe_float(value, default=0.0) or 0.0):,.0f}"
                val_style = CASYS_MUTED
            elif key in _PCT_KEYS:
                val_str = f"{(_safe_float(value, default=0.0) or 0.0) * 100:.1f}%"
                val_style = CASYS_MUTED
            else:
                val_str = str(value)
                val_style = CASYS_MUTED
            cap_grid.add_row(
                Text(label, style=CASYS_FAINT),
                Text(val_str, style=val_style),
            )
        parts.append(cap_grid)
    else:
        parts.append(
            Text(
                "active caps — max gross exposure · per-symbol cap"
                " · session gate · order size vs equity (config/risk.yaml)",
                style=CASYS_FAINT,
            )
        )
    return Group(*parts)


def build_model_panel(state: dict) -> RenderableType:
    """Provider / fallbacks / avg conf depuis kpis.model_performance.

    "avg latency" absent du pipeline → remplacée par avg tool rounds.
    """
    kpis = f.safe_dict(state.get("kpis"))
    perfs = _safe_list_of_dicts(kpis.get("model_performance"))

    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, width=12)
    grid.add_column(no_wrap=True)

    if not perfs:
        # Fallback depuis recent_decisions
        decs = _safe_list_of_dicts(state.get("recent_decisions"))
        seen: dict[str, int] = {}
        for row in decs:
            p = _safe_str(row.get("llm_provider"))
            m = _safe_str(row.get("llm_model"))
            if p or m:
                key = f"{p}·{m}" if (p and m) else (p or m)
                seen[key] = seen.get(key, 0) + 1
        provider_str = " · ".join(seen.keys()) if seen else "—"
        grid.add_row(Text("provider", style=CASYS_FAINT), Text(provider_str, style=CASYS_MUTED))
        return grid

    # Agrégation
    labels: list[str] = []
    total_fb = 0
    conf_vals: list[float] = []
    for perf in perfs:
        p = _safe_str(perf.get("provider"))
        m = _safe_str(perf.get("model"))
        if p or m:
            labels.append(f"{p}·{m}" if (p and m) else (p or m))
        fb = _safe_float(perf.get("fallbacks"), default=0.0) or 0.0
        total_fb += int(fb)
        avg_c = _safe_float(perf.get("avg_confidence"), default=None)
        if avg_c is not None:
            conf_vals.append(avg_c)

    provider_str = " · ".join(labels) if labels else "—"
    grid.add_row(Text("provider", style=CASYS_FAINT), Text(provider_str, style=CASYS_MUTED))

    fb_text = Text()
    fb_text.append(str(total_fb), style=CASYS_MUTED)
    fb_text.append(" (on error → HOLD)", style=CASYS_FAINT)
    grid.add_row(Text("fallbacks", style=CASYS_FAINT), fb_text)

    if conf_vals:
        avg_conf = sum(conf_vals) / len(conf_vals)
        grid.add_row(
            Text("avg conf", style=CASYS_FAINT),
            Text(_fmt_conf(avg_conf), style=CASYS_MUTED),
        )

    # Avg tool rounds (latence absente du pipeline — §1.12 map-read-model)
    decs = _safe_list_of_dicts(state.get("recent_decisions"))
    rounds_list: list[float] = []
    for row in decs:
        rt = f.safe_dict(row.get("runtime"))
        dec = f.safe_dict(row.get("decision"))
        val = _safe_float(rt.get("tool_rounds") or dec.get("tool_rounds"), default=None)
        if val is not None:
            rounds_list.append(val)
    if rounds_list:
        avg_rounds = sum(rounds_list) / len(rounds_list)
        grid.add_row(
            Text("avg rounds", style=CASYS_FAINT),
            Text(f"{avg_rounds:.1f}", style=CASYS_MUTED),
        )

    return grid


def build_ledger_footer(total: int, filtered: int, active_filter: str) -> Text:
    text = Text()
    if active_filter != "all":
        text.append(f"showing {filtered} / {total}", style=CASYS_DIM)
        text.append(" · ", style=CASYS_FAINT)
    else:
        text.append(f"↓ {total} decisions", style=CASYS_FAINT)
        text.append(" · ", style=CASYS_FAINT)
    text.append("enter expand", style=CASYS_MUTED)
    text.append(" · full ledger in state/decisions.jsonl", style=CASYS_FAINT)
    return text


def build_detail_panel(row: dict | None, *, now: datetime) -> RenderableType:
    """Détail de la ligne curseur : rationale + tool outcomes + meta."""
    if row is None:
        return Text("↑ navigate to a row", style=f"italic {CASYS_FAINT}")
    if row.get("_is_batch_summary"):
        reason = _safe_str(row.get("reason"))
        return Text(reason, style=CASYS_FAINT) if reason else Text("batch row — no detail", style=CASYS_FAINT)

    parts: list[RenderableType] = []

    # Rationale avec bordure accent gauche
    rationale = str(row.get("rationale") or "").strip()
    if rationale:
        rat_text = Text()
        rat_text.append("│ ", style=CASYS_ACCENT)
        rat_text.append(rationale, style=CASYS_MUTED)
        parts.append(rat_text)

    # Tool calls
    runtime = f.safe_dict(row.get("runtime"))
    decision_obj = f.safe_dict(row.get("decision"))
    tool_calls = _safe_list_of_dicts(
        runtime.get("tool_calls") or decision_obj.get("tool_calls")
    )
    if tool_calls:
        tc_grid = Table.grid(padding=(0, 1))
        tc_grid.add_column(no_wrap=True, width=2)
        tc_grid.add_column(no_wrap=True, width=18)
        tc_grid.add_column(no_wrap=True)
        for tc in tool_calls:
            outcome = _safe_str(tc.get("outcome"))
            if outcome in ("applied", "created"):
                icon = Text("✓", style=CASYS_SUCCESS)
            elif outcome == "rejected":
                icon = Text("✗", style=CASYS_ERROR)
            else:
                icon = Text("·", style=CASYS_DIM)
            tool_name = Text(f.clip(_safe_str(tc.get("tool")), limit=18), style=CASYS_MUTED)
            detail_dict = f.safe_dict(tc.get("detail"))
            detail_str = outcome
            warns = _safe_list_of_dicts(detail_dict.get("warnings"))
            if warns:
                codes = [_safe_str(w.get("code")) for w in warns[:2] if w.get("code")]
                if codes:
                    detail_str += f" — {', '.join(codes)}"
            tc_grid.add_row(icon, tool_name, Text(detail_str, style=CASYS_FAINT))
        parts.append(tc_grid)

    # Meta : data source · price · earnings · tool rounds · reason code
    meta: list[str] = []
    ds = _safe_str(runtime.get("data_source") or decision_obj.get("data_source"))
    if ds:
        meta.append(f"data {ds}")
    price = _safe_float(row.get("price"), default=None)
    if price is not None:
        meta.append(f"price {f.fmt_compact(price, decimals=2)}")
    news = f.safe_dict(row.get("news"))
    earn_h = _safe_float(news.get("earnings_in_h"), default=None)
    if earn_h is not None:
        meta.append(f"earnings in {int(earn_h / 24)}d")
    rounds_val = _safe_float(
        runtime.get("tool_rounds") or decision_obj.get("tool_rounds"), default=None
    )
    if rounds_val is not None:
        meta.append(f"{int(rounds_val)} tool rounds")
    code = _safe_str(row.get("decision_reason_code"))
    if code:
        meta.append(f.clip(code.lower().replace("_", " "), limit=28))
    if meta:
        parts.append(Text("  ".join(meta), style=CASYS_FAINT))

    if not parts:
        return Text("no details available", style=f"italic {CASYS_FAINT}")
    return Group(*parts)


# ---------------------------------------------------------------------------
# Remplissage du DataTable
# ---------------------------------------------------------------------------


def populate_ledger_table(
    table: DataTable,
    rows: list[dict],
    state: dict,
    *,
    now: datetime,
    drop_source: bool = False,
) -> dict[str, dict]:
    """Peuple le DataTable et retourne {row_key → row_dict}.

    Gestion jour-limite : quand la date change, l'heure UTC porte le préfixe
    du jour (ex. "Sat 22:31").
    """
    del state, now  # compatibility parameters; projection is state-independent here
    table.clear()
    row_map: dict[str, dict] = {}

    for row in build_ledger_rows(rows):
        utc_text = Text(
            row.utc_text,
            style=CASYS_FAINT if row.utc_text == "—" else CASYS_DIM,
        )
        symbol_text = Text(
            row.symbol,
            style=CASYS_FAINT if row.is_batch else f"bold {CASYS_FG}",
        )
        if row.is_batch:
            action_text = Text(row.action_text, style=CASYS_FAINT)
            confidence_text = Text("", style=CASYS_FAINT)
            source_text = Text(row.source_text, style=CASYS_FAINT)
            effect_text = Text(row.effect_text, style=CASYS_FAINT)
        else:
            if row.action == "BUY":
                action_style = f"bold {CASYS_SUCCESS}"
            elif row.action == "SELL":
                action_style = f"bold {CASYS_ERROR}"
            elif row.action:
                action_style = CASYS_MUTED
            else:
                action_style = CASYS_FAINT
            action_text = Text(row.action_text, style=action_style)
            confidence_text = Text(row.confidence_text, style=CASYS_MUTED)
            source_text = Text(row.source_text, style=CASYS_DIM)
            if row.effect_kind == "fill":
                effect_style = (
                    CASYS_ERROR if row.action == "SELL" else CASYS_SUCCESS
                )
            elif row.effect_kind in ("watch", "plan", "wake"):
                effect_style = CASYS_ACCENT
            elif row.effect_kind == "risk":
                effect_style = CASYS_WARNING
            else:
                effect_style = CASYS_DIM
            effect_text = Text(row.effect_text, style=effect_style)

        cells = [
            utc_text,
            symbol_text,
            action_text,
            confidence_text,
            source_text,
            effect_text,
        ]
        if drop_source:
            cells.pop(4)
        table.add_row(*cells, key=row.row_key)
        row_map[row.row_key] = row.source_row

    return row_map


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class DecisionsPage(ResizeRefresh, Static):
    """Page 3 — Decisions : ledger (passé) à gauche · playbook (futur) à droite.

    Révision 3 : fusion de l'ancienne page Plans. Le playbook empile ARMED ·
    EXITS (compact) · WATCHES · NEXT TO FIRE — le détail complet d'un plan
    reste dans le drill-down symbole. RISK GATE + MODEL vivent sur Health.
    """

    BINDINGS = [
        Binding("a", "filter_all", show=False),
        Binding("b", "filter_buy", show=False),
        Binding("s", "filter_sell", show=False),
        Binding("h", "filter_hold", show=False),
    ]

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    DecisionsPage {
        layout: horizontal;
        height: 100%;
        padding: 0 1;
    }
    DecisionsPage #ledger-section {
        width: 1fr;
        height: 100%;
        margin-right: 1;
    }
    DecisionsPage #filter-chips-bar {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
    }
    DecisionsPage #ledger-panel {
        height: 1fr;
    }
    DecisionsPage #ledger-table {
        height: 1fr;
        min-height: 5;
    }
    DecisionsPage #detail-scroll {
        height: auto;
        max-height: 40%;
        margin-top: 1;
        display: none;
    }
    DecisionsPage #ledger-footer {
        height: auto;
        padding: 0 1;
    }
    DecisionsPage #decisions-right {
        height: 100%;
    }
    /* Playbook : chaque section bornée + scroll interne, EXITS = table
       principale (1fr). Sans plafond, 20 ordres armés cacheraient le reste. */
    DecisionsPage #armed-panel { height: auto; max-height: 35%; min-height: 4; margin-bottom: 1; }
    DecisionsPage #exits-panel { height: 1fr; min-height: 5; margin-bottom: 1; }
    DecisionsPage #playbook-watches-panel { height: auto; max-height: 30%; min-height: 4; margin-bottom: 1; }
    DecisionsPage #playbook-fire-panel { height: auto; max-height: 25%; min-height: 4; }
    DecisionsPage .casys-panel Static { height: auto; }
    """
    )

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._active_filter: str = "all"
        self._show_detail: bool = False
        self._expanded_key: str | None = None
        self._last_state: dict = {}
        self._row_map: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        # Colonne gauche : chips + ledger panel + footer
        with Vertical(id="ledger-section"):
            yield Static("", id="filter-chips-bar")
            with Vertical(id="ledger-panel", classes="casys-panel") as panel:
                panel.border_title = "LEDGER"
                yield DataTable(id="ledger-table", cursor_type="row", zebra_stripes=False)
                with VerticalScroll(id="detail-scroll", classes="casys-panel") as ds:
                    ds.border_title = "DETAIL"
                    yield Static("", id="ledger-detail")
            yield Static("", id="ledger-footer")
        # Colonne droite : PLAYBOOK (futur) — armed · exits · watches · next to fire
        with Vertical(id="decisions-right", classes="right-col"):
            with VerticalScroll(id="armed-panel", classes="casys-panel") as armed:
                armed.border_title = "ARMED"
                yield Static("", id="armed-body")
            with VerticalScroll(id="exits-panel", classes="casys-panel") as exits:
                exits.border_title = "EXITS — by stop distance"
                yield Static("", id="exits-body")
            with VerticalScroll(id="playbook-watches-panel", classes="casys-panel") as wt:
                wt.border_title = "WATCHES"
                yield Static("", id="playbook-watches-body")
            with VerticalScroll(id="playbook-fire-panel", classes="casys-panel") as fire:
                fire.border_title = "NEXT TO FIRE"
                yield Static("", id="playbook-fire-body")

    _drop_source: bool | None = None

    def on_mount(self) -> None:
        """Configure les colonnes du DataTable après le montage."""
        self._rebuild_columns(force=True)

    def _rebuild_columns(self, *, force: bool = False) -> None:
        """SOURCE saute sous ~80 cols (EFFECT garde la place de respirer)."""
        table = self.query_one("#ledger-table", DataTable)
        width = table.size.width or 0
        drop_source = 0 < width < 80
        if not force and drop_source == self._drop_source:
            return
        self._drop_source = drop_source
        table.clear(columns=True)
        table.add_column(Text("UTC", style=CASYS_FAINT), width=8)
        table.add_column(Text("SYM", style=CASYS_FAINT), width=10)
        table.add_column(Text("ACT", style=CASYS_FAINT), width=5)
        table.add_column(Text("CONF", style=CASYS_FAINT), width=5)
        if not drop_source:
            table.add_column(Text("SOURCE", style=CASYS_FAINT), width=15)
        table.add_column(Text("EFFECT", style=CASYS_FAINT))

    # ------------------------------------------------------------------
    # Événements DataTable
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter : bascule le panneau DETAIL sur la ligne curseur."""
        row_key = str(event.row_key.value or "")
        detail_scroll = self.query_one("#detail-scroll")
        if self._show_detail and self._expanded_key == row_key:
            # Même ligne → collapse
            detail_scroll.display = False
            self._show_detail = False
            self._expanded_key = None
        else:
            self._expanded_key = row_key
            self._show_detail = True
            detail_scroll.display = True
            self._render_detail(row_key)
        event.stop()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Cursor move : met à jour le détail si le panneau est ouvert."""
        if not self._show_detail:
            return
        row_key = str(event.row_key.value or "")
        self._expanded_key = row_key
        self._render_detail(row_key)

    def _render_detail(self, row_key: str) -> None:
        row = self._row_map.get(row_key)
        now = datetime.now(UTC)
        try:
            self.query_one("#ledger-detail", Static).update(
                build_detail_panel(row, now=now)
            )
            sym = _safe_str((row or {}).get("symbol"))
            title = f"DETAIL — {sym}" if sym and sym != "—" else "DETAIL"
            self.query_one("#detail-scroll").border_title = title
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Actions filtre
    # ------------------------------------------------------------------

    def action_filter_all(self) -> None:
        self._active_filter = "all"
        self._refresh_ledger()

    def action_filter_buy(self) -> None:
        self._active_filter = "buy"
        self._refresh_ledger()

    def action_filter_sell(self) -> None:
        self._active_filter = "sell"
        self._refresh_ledger()

    def action_filter_hold(self) -> None:
        self._active_filter = "hold"
        self._refresh_ledger()

    # ------------------------------------------------------------------
    # Refresh interne
    # ------------------------------------------------------------------

    def _refresh_ledger(self) -> None:
        """Reconstruit la table et les chips sans toucher aux panneaux droits."""
        try:
            state = self._last_state
            now = datetime.now(UTC)
            ledger = project_decision_ledger(state, self._active_filter)

            self.query_one("#filter-chips-bar", Static).update(
                build_filter_chips(ledger.filter_counts, self._active_filter)
            )

            table = self.query_one("#ledger-table", DataTable)
            self._rebuild_columns()
            with preserve_cursor(table):
                self._row_map = populate_ledger_table(
                    table,
                    ledger.grouped_rows,
                    state,
                    now=now,
                    drop_source=bool(self._drop_source),
                )

            total = len(ledger.all_rows)
            self.query_one("#ledger-panel").border_title = (
                f"LEDGER — last 50 · {total} decisions"
            )
            self.query_one("#ledger-footer", Static).update(
                build_ledger_footer(
                    total,
                    len(ledger.filtered_rows),
                    self._active_filter,
                )
            )

            # Collapse le détail si le filtre change
            self._show_detail = False
            self._expanded_key = None
            self.query_one("#detail-scroll").display = False
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Contrat public
    # ------------------------------------------------------------------

    def update_state(self, state: dict) -> None:
        """Met à jour ledger + playbook. Ne lève jamais d'exception."""
        try:
            self._last_state = state
            self._refresh_ledger()
        except Exception:
            pass
        now = datetime.now(UTC)

        try:
            armed_projection = project_armed_orders(state, now=now)
            self.query_one("#armed-body", Static).update(
                build_armed(
                    state,
                    now=now,
                    projection=armed_projection,
                )
            )
            self.query_one("#armed-panel").border_title = (
                f"ARMED — {len(armed_projection.rows)}"
            )
        except Exception:
            pass

        try:
            exit_plans_projection = project_exit_plans(state)
            self.query_one("#exits-body", Static).update(
                build_exit_plans_compact(
                    state,
                    now=now,
                    limit=None,
                    projection=exit_plans_projection,
                )
            )
            self.query_one("#exits-panel").border_title = (
                f"EXITS — {len(exit_plans_projection.rows)} · by stop distance"
            )
        except Exception:
            pass

        try:
            active_watches_projection = project_active_watches(state, now=now)
            exit_watches_projection = project_exit_watches(state, now=now)
            watches = build_watches(
                state,
                now=now,
                projection=active_watches_projection,
            )
            exit_watches = build_exit_watches(
                state,
                now=now,
                projection=exit_watches_projection,
            )
            self.query_one("#playbook-watches-body", Static).update(
                Group(watches, Text(""), exit_watches)
            )
            active_count = len(active_watches_projection.rows)
            exit_count = len(exit_watches_projection.rows)
            self.query_one("#playbook-watches-panel").border_title = (
                f"WATCHES — {active_count} + {exit_count} exits"
            )
        except Exception:
            pass

        try:
            self.query_one("#playbook-fire-body", Static).update(
                build_next_to_fire_plans(state, now=now)
            )
        except Exception:
            pass
