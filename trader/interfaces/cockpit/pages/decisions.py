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
from trader.interfaces.cockpit.pages._shared import PANEL_CSS
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
# Helpers locaux purs
# ---------------------------------------------------------------------------


def _safe_str(value: object) -> str:
    return str(value or "")


def _is_risk_row(row: dict) -> bool:
    reason = _safe_str(row.get("reason"))
    return reason.startswith("risk:") or reason.startswith("blocked_")


def _is_stale_row(state: dict, row: dict) -> bool:
    symbol = _safe_str(row.get("symbol"))
    stale = f.safe_dict(state.get("stale_market_data"))
    return symbol in stale


def _is_batch_row(row: dict) -> bool:
    """Ligne infra_hold sans appel LLM — candidate au regroupement batch."""
    return (
        _safe_str(row.get("decision_source")) == "infra_hold"
        and not row.get("model_called", True)
    )


def _filter_rows(rows: list[dict], active_filter: str, state: dict) -> list[dict]:
    if active_filter == "buy":
        return [r for r in rows if _safe_str(r.get("action")).upper() == "BUY"]
    if active_filter == "sell":
        return [r for r in rows if _safe_str(r.get("action")).upper() == "SELL"]
    if active_filter == "hold":
        return [r for r in rows if _safe_str(r.get("action")).upper() == "HOLD"]
    if active_filter == "risk":
        return [r for r in rows if _is_risk_row(r)]
    if active_filter == "stale":
        return [r for r in rows if _is_stale_row(state, r)]
    return rows  # "all"


def _count_filters(rows: list[dict], state: dict) -> dict[str, int]:
    return {
        "all": len(rows),
        "buy": sum(1 for r in rows if _safe_str(r.get("action")).upper() == "BUY"),
        "sell": sum(1 for r in rows if _safe_str(r.get("action")).upper() == "SELL"),
        "hold": sum(1 for r in rows if _safe_str(r.get("action")).upper() == "HOLD"),
        "risk": sum(1 for r in rows if _is_risk_row(r)),
        "stale": sum(1 for r in rows if _is_stale_row(state, r)),
    }


def _group_into_ledger_rows(rows: list[dict]) -> list[dict]:
    """Regroupe les séquences infra_hold ≥ 3 d'un même cycle en une batch row."""
    if not rows:
        return []
    result: list[dict] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if not _is_batch_row(row):
            result.append(row)
            i += 1
            continue
        # Collecter les lignes batch consécutives du même cycle
        cycle_ts = row.get("cycle_ts") or row.get("ts", "")
        batch: list[dict] = [row]
        j = i + 1
        while j < len(rows):
            nxt = rows[j]
            nxt_ts = nxt.get("cycle_ts") or nxt.get("ts", "")
            if _is_batch_row(nxt) and nxt_ts == cycle_ts:
                batch.append(nxt)
                j += 1
            else:
                break
        if len(batch) >= 3:
            llm_n = sum(1 for r in batch if r.get("model_called", False))
            quiet_n = len(batch) - llm_n
            result.append(
                {
                    "_is_batch_summary": True,
                    "cycle_ts": cycle_ts,
                    "symbol": "— batch",
                    "action": "HOLD",
                    "decision_source": "heuristic",
                    "reason": (
                        f"{len(batch)} due — {llm_n} LLM calls, {quiet_n} quiet holds"
                    ),
                }
            )
        else:
            result.extend(batch)
        i = j
    return result


def _fmt_conf(conf: object) -> str:
    """0.72 → ".72" · None → "—" · 1.0 → "1.0"."""
    value = _safe_float(conf, default=None)
    if value is None:
        return "—"
    if value >= 1.0:
        return "1.0"
    return f"{value:.2f}".lstrip("0") or ".00"


def _has_detail(row: dict) -> bool:
    """La ligne a un rationale ou des tool_calls à afficher."""
    if str(row.get("rationale") or "").strip():
        return True
    runtime = f.safe_dict(row.get("runtime"))
    decision_obj = f.safe_dict(row.get("decision"))
    calls = _safe_list_of_dicts(
        runtime.get("tool_calls") or decision_obj.get("tool_calls")
    )
    return bool(calls)


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


def build_mix_24h(state: dict) -> RenderableType:
    """Barres buy/sell/hold des 50 dernières décisions."""
    rows = _safe_list_of_dicts(state.get("recent_decisions"))
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
    """Rejets récents + caps lus de config/risk.yaml."""
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
    cap_fragments: list[str] = []
    if caps.get("max_gross_exposure"):
        cap_fragments.append(f"gross cap ${caps['max_gross_exposure']:,.0f}")
    if caps.get("max_position_value"):
        cap_fragments.append(f"per-symbol ${caps['max_position_value']:,.0f}")
    if caps.get("max_order_value"):
        cap_fragments.append(f"order ${caps['max_order_value']:,.0f}")
    if caps.get("max_risk_per_trade_pct") is not None:
        pct = (_safe_float(caps["max_risk_per_trade_pct"], default=0.0) or 0.0) * 100
        cap_fragments.append(f"risk/trade {pct:.1f}%")

    cap_line = Text()
    if cap_fragments:
        cap_line.append("active caps — ", style=CASYS_FAINT)
        cap_line.append(", ".join(cap_fragments), style=CASYS_DIM)
        cap_line.append(" (config/risk.yaml)", style=CASYS_FAINT)
    else:
        cap_line.append(
            "active caps — max gross exposure · per-symbol cap"
            " · session gate · order size vs equity (config/risk.yaml)",
            style=CASYS_FAINT,
        )
    parts.append(cap_line)
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
) -> dict[str, dict]:
    """Peuple le DataTable et retourne {row_key → row_dict}.

    Gestion jour-limite : quand la date change, l'heure UTC porte le préfixe
    du jour (ex. "Sat 22:31").
    """
    table.clear()
    row_map: dict[str, dict] = {}
    prev_date: str | None = None

    for idx, row in enumerate(rows):
        is_batch = bool(row.get("_is_batch_summary"))

        cycle_ts = row.get("cycle_ts") or row.get("ts")
        parsed = f.parse_ts(cycle_ts)

        # Colonne UTC
        if parsed:
            date_str = parsed.strftime("%Y-%m-%d")
            time_str = parsed.strftime("%H:%M")
            if prev_date and date_str != prev_date:
                utc_text = Text(f"{parsed.strftime('%a')} {time_str}", style=CASYS_DIM)
            else:
                utc_text = Text(time_str, style=CASYS_DIM)
            prev_date = date_str
        else:
            utc_text = Text("—", style=CASYS_FAINT)

        # Colonne SYM
        symbol = str(row.get("symbol") or "—")
        sym_text = (
            Text(symbol, style=CASYS_FAINT) if is_batch
            else Text(symbol, style=f"bold {CASYS_FG}")
        )

        # Colonne ACT
        action = str(row.get("action") or "").upper()
        if is_batch:
            act_text = Text("···", style=CASYS_FAINT)
        elif action == "BUY":
            act_text = Text("BUY", style=f"bold {CASYS_SUCCESS}")
        elif action == "SELL":
            act_text = Text("SELL", style=f"bold {CASYS_ERROR}")
        elif action:
            act_text = Text(action, style=CASYS_MUTED)
        else:
            act_text = Text("—", style=CASYS_FAINT)

        # Colonne CONF
        if is_batch:
            conf_text = Text("", style=CASYS_FAINT)
        else:
            conf_text = Text(_fmt_conf(row.get("confidence")), style=CASYS_MUTED)

        # Colonne SOURCE
        if is_batch:
            source_text = Text("heuristic", style=CASYS_FAINT)
        else:
            src = f.decision_source_label(row)
            source_text = Text(src or "—", style=CASYS_DIM)

        # Colonne EFFECT
        if is_batch:
            effect_text = Text(str(row.get("reason") or ""), style=CASYS_FAINT)
        else:
            effect, kind = f.decision_effect(row)
            if kind == "fill":
                effect_style = CASYS_ERROR if action == "SELL" else CASYS_SUCCESS
            elif kind in ("watch", "plan", "wake"):
                effect_style = CASYS_ACCENT
            elif kind == "risk":
                effect_style = CASYS_WARNING
            else:
                effect_style = CASYS_DIM
            has_det = _has_detail(row)
            clipped = f.clip(effect, limit=44)
            effect_text = Text(
                f"{clipped} ▾" if has_det else clipped,
                style=effect_style,
            )

        row_key = f"{cycle_ts or ''}|{symbol}|{idx}"
        table.add_row(
            utc_text, sym_text, act_text, conf_text, source_text, effect_text,
            key=row_key,
        )
        row_map[row_key] = row

    return row_map


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class DecisionsPage(Static):
    """Page 3 — Decisions ledger + MIX / RISK GATE / MODEL."""

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
        max-height: 15;
        margin-top: 1;
        display: none;
    }
    DecisionsPage #ledger-footer {
        height: auto;
        padding: 0 1;
    }
    DecisionsPage #decisions-right {
        width: 40;
        height: 100%;
    }
    DecisionsPage #mix-panel { height: auto; margin-bottom: 1; }
    DecisionsPage #risk-panel { height: auto; margin-bottom: 1; }
    DecisionsPage #model-panel { height: 1fr; }
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
        # Colonne droite : 3 panneaux empilés
        with Vertical(id="decisions-right"):
            with VerticalScroll(id="mix-panel", classes="casys-panel") as mix:
                mix.border_title = "MIX — 24h"
                yield Static("", id="mix-body")
            with VerticalScroll(id="risk-panel", classes="casys-panel") as risk:
                risk.border_title = "RISK GATE"
                yield Static("", id="risk-body")
            with VerticalScroll(id="model-panel", classes="casys-panel") as model:
                model.border_title = "MODEL"
                yield Static("", id="model-body")

    def on_mount(self) -> None:
        """Configure les colonnes du DataTable après le montage."""
        table = self.query_one("#ledger-table", DataTable)
        table.add_column(Text("UTC", style=CASYS_FAINT), width=8)
        table.add_column(Text("SYM", style=CASYS_FAINT), width=10)
        table.add_column(Text("ACT", style=CASYS_FAINT), width=5)
        table.add_column(Text("CONF", style=CASYS_FAINT), width=5)
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
            all_rows = _safe_list_of_dicts(state.get("recent_decisions"))
            counts = _count_filters(all_rows, state)
            filtered = _filter_rows(all_rows, self._active_filter, state)
            grouped = _group_into_ledger_rows(filtered)

            self.query_one("#filter-chips-bar", Static).update(
                build_filter_chips(counts, self._active_filter)
            )

            table = self.query_one("#ledger-table", DataTable)
            self._row_map = populate_ledger_table(table, grouped, state, now=now)

            total = len(all_rows)
            self.query_one("#ledger-panel").border_title = (
                f"LEDGER — last 50 · {total} decisions"
            )
            self.query_one("#ledger-footer", Static).update(
                build_ledger_footer(total, len(filtered), self._active_filter)
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
        """Met à jour tous les panneaux avec le nouvel état. Ne lève jamais d'exception."""
        try:
            self._last_state = state
            self._refresh_ledger()
            self.query_one("#mix-body", Static).update(build_mix_24h(state))
            self.query_one("#risk-body", Static).update(build_risk_gate(state))
            self.query_one("#model-body", Static).update(build_model_panel(state))
        except Exception:
            pass
