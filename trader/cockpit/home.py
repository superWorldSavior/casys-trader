"""Home « mode Gonzo » du cockpit : builders purs + widgets Textual.

Spec : docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md.
Les builders sont purs (state dict → renderable) ; seuls les widgets
touchent Textual. Aucune I/O ici.
"""
from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from trader.cockpit.aggregates import (  # noqa: F401
    ACTIVITY_STATES,
    activity_buckets,
    attention_items,
    decision_status,
    select_decision_rows,
    venue_clock,
)
from trader.cockpit.overview import (
    _armed_order_label,
    _armed_stop_label,
    _bar,
    _condition_summary,
    _decision_effect_label,
    _decision_time_label,
    _exit_plan_risk_label,
    _extract_equity_curve,
    _fmt_compact_float,
    _fmt_signed_compact_float,
    _holding_currency,
    _holding_native_price,
    _holding_notional,
    _holding_pnl,
    _holding_symbol,
    _metric_cell,
    _next_tp_label,
    _plan_for_symbol,
    _plan_qty_label,
    _plan_state_label,
    _price_for_symbol,
    _relative_expiry,
    _signed_bar,
    _stop_risk_for_holding,
    _symbol_is_stale,
)
from trader.planning.indicator_watch import is_armed_plan as _is_armed_plan
from trader.read_models.runtime_state import _safe_float, _safe_list_of_dicts
from trader.ui.palette import PALETTE_LIGHT, Palette
from trader.ui.rich_panels import _build_exit_plans_enriched, _build_learnings_panel, sparkline

UTC = timezone.utc


def build_attention_text(
    state: dict,
    *,
    kill_active: bool,
    palette: Palette,
    now: datetime | None = None,
) -> Text:
    """Ligne « à surveiller » : anomalies uniquement, sinon RAS discret. PURE."""
    now = now or datetime.now(UTC)
    items = attention_items(state, kill_active=kill_active, now=now)
    if not items:
        return Text("✓ RAS", style=palette["status_nominal"])
    text = Text(" ⚠ ", style=f"bold {palette['kpi_vol_warn']}")
    for index, item in enumerate(items):
        if index:
            text.append("  ·  ", style=palette["dim"])
        style = "bold white on red" if item.severity == "crit" else palette["kpi_vol_warn"]
        text.append(item.label, style=style)
    return text


class AttentionLine(Static):
    """Ligne « à surveiller » — widget mince sur build_attention_text."""

    DEFAULT_CSS = """
    AttentionLine {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    """

    def update_state(
        self,
        state: dict,
        kill_active: bool,
        *,
        palette: Palette,
        now: datetime | None = None,
    ) -> None:
        self.update(build_attention_text(state, kill_active=kill_active, palette=palette, now=now))


# Ordre de drop quand la largeur manque — les 4 segments critiques
# (vital, équité, mode, kill) ne figurent volontairement pas ici.
_STATUS_DROP_ORDER = ("spark", "venues", "pnl_usd", "cycle", "utc", "llm", "pnl_pct")


def _vital_text(vital, palette: Palette) -> Text:
    if vital.status == "alive":
        if vital.battement_old:
            mins = int(vital.since_seconds or 0) // 60
            secs = int(vital.since_seconds or 0) % 60
            return Text.assemble(("● VIVANT", "bold yellow"),
                                 (f" {mins:02d}:{secs:02d}", palette["dim"]))
        return Text("● VIVANT", style="bold green")
    if vital.status == "stopped":
        return Text("● ARRÊTÉ", style="bold red")
    return Text("● jamais démarré", style=palette["dim"])


def build_status_line(
    state: dict,
    *,
    kill_active: bool,
    palette: Palette,
    width: int,
    now: datetime,
    vital,
) -> Text:
    """Barre de statut distillée. Pur : vital et now sont injectés."""
    state = state if isinstance(state, dict) else {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    daemon_status = (
        state.get("daemon_status") if isinstance(state.get("daemon_status"), dict) else {}
    )

    equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
    cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
    starting = _safe_float(state.get("starting_cash"), default=cash) or cash
    pnl = equity - starting
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        ret_pct = (_safe_float(kpis.get("total_return"), default=0.0) or 0.0) * 100.0
    pnl_style = palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]

    curve = [
        v for v in (_safe_float(x, default=None) for x in (state.get("equity_curve") or []))
        if v is not None
    ]
    spark = sparkline(curve[-24:]) if len(curve) >= 2 else ""

    used = daemon_status.get("model_calls_used")
    max_calls = daemon_status.get("max_model_calls_per_cycle")
    llm = f"LLM {used}/{max_calls}" if used is not None and max_calls is not None else "LLM —"

    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    cycle_running = (
        isinstance(total, int) and total > 0 and isinstance(done, int) and done < total
    )

    clock = venue_clock(state.get("sessions") or {}, now)
    venue_bits: list[tuple[str, str]] = []
    for venue in clock.open_now:
        venue_bits.append((f"{venue}●", palette["status_nominal"]))
        venue_bits.append((" ", ""))
    if clock.next_at is not None and clock.next_venue not in clock.open_now:
        venue_bits.append(
            (f"{clock.next_venue} {clock.next_at.strftime('%H:%M')}", palette["dim"])
        )
    venues = Text.assemble(*venue_bits) if venue_bits else None

    mode = Text("LIVE", style="bold red") if not state.get("dry_run", True) else Text(
        "DRY", style="bold yellow"
    )
    kill = (
        Text(" !! KILL !! ", style="bold white on red")
        if kill_active
        else Text.assemble(("kill:", palette["dim"]), ("nominal", palette["status_nominal"]))
    )

    segments: list[tuple[str, Text | None]] = [
        ("vital", _vital_text(vital, palette)),
        ("equity", Text(f"{equity:,.0f}$", style=f"bold {palette['status_equity']}")),
        ("spark", Text(spark, style=palette["equity_line"]) if spark else None),
        ("pnl_pct", Text(f"{ret_pct:+.2f}%", style=pnl_style) if equity > 0 else None),
        ("pnl_usd", Text(f"({pnl:+,.0f})", style=pnl_style) if equity > 0 else None),
        ("mode", mode),
        ("kill", kill),
        ("llm", Text(llm, style=palette["status_accent"])),
        ("cycle", Text(f"cycle {done}/{total}", style=palette["status_accent"])
         if cycle_running else None),
        ("venues", venues),
        ("utc", Text(now.strftime("%H:%M:%SZ"), style=palette["dim"])),
    ]
    kept = [(name, text) for name, text in segments if text is not None]

    def _assemble(parts: list[tuple[str, Text]]) -> Text:
        out = Text("  ")
        for index, (_, piece) in enumerate(parts):
            if index:
                out.append(" · ", style=palette["dim"])
            out.append_text(piece)
        return out

    line = _assemble(kept)
    for drop in _STATUS_DROP_ORDER:
        if line.cell_len <= width:
            break
        kept = [(name, text) for name, text in kept if name != drop]
        line = _assemble(kept)
    return line


def _hex_to_rgb(color: str) -> tuple[int, int, int] | str:
    """"#RRGGBB" → tuple RGB pour plotext ; nom de couleur → inchangé."""
    text = color.strip()
    if text.startswith("#") and len(text) == 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            return text
    return text


def styled_bar(
    value: float, total: float, *, width: int, style: str, signed: bool = False
) -> Text:
    """Barre unicode STYLÉE (fix des barres noires : _bar retourne une str nue)."""
    raw = _signed_bar(value, total, width=width) if signed else _bar(value, total, width=width)
    return Text(raw, style=style)


def build_equity_chart(
    values: list[float], *, palette: Palette, width: int = 58, height: int = 9
) -> RenderableType:
    """Courbe d'équité en braille plotext, couleur palette, fenêtre ~200 pts."""
    series = [v for v in values if v == v][-200:]
    if len(series) < 2:
        return Text("courbe indisponible (<2 points)", style=palette["dim"])
    try:
        import plotext as plt

        plt.clear_figure()
        plt.theme("clear")
        plt.plotsize(width, height)
        plt.plot(list(range(len(series))), series, marker="braille",
                 color=_hex_to_rgb(palette["equity_line"]))
        return Text.from_ansi(plt.build())
    except Exception:
        return Text(sparkline(series[-width:]), style=f"bold {palette['equity_line']}")


def build_portfolio_summary(state: dict, *, palette: Palette) -> RenderableType:
    """Métriques portefeuille + courbe équité + mini-allocation/contrib. Sans table positions."""
    state = state if isinstance(state, dict) else {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    attribution = state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
    holdings = sorted(
        _safe_list_of_dicts(portfolio.get("holdings")),
        key=lambda item: max(abs(_holding_pnl(item)), _holding_notional(item)),
        reverse=True,
    )

    cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
    equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
    starting = _safe_float(state.get("starting_cash"), default=cash) or cash
    pnl = equity - starting
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        ret_pct = (_safe_float(kpis.get("total_return"), default=0.0) or 0.0) * 100.0
    unrealized = sum(_holding_pnl(h) for h in holdings)
    realized = _safe_float(attribution.get("realized_pnl"), default=None)
    fees = _safe_float(attribution.get("total_commissions"), default=None)

    # 1. Metrics header (2 rows)
    header = Table.grid(expand=True)
    for _ in range(4):
        header.add_column(ratio=1)
    ret_style = palette["pnl_positive"] if ret_pct >= 0 else palette["pnl_negative"]
    header.add_row(
        _metric_cell("Équité", f"{equity:,.0f}", value_style=f"bold {palette['status_equity']}", palette=palette),
        _metric_cell("Cash", f"{cash:,.0f} ({cash / equity * 100.0 if equity else 0.0:.0f}%)", value_style=palette["kpi_default"], palette=palette),
        _metric_cell("P&L total", f"{ret_pct:+.2f}% {pnl:+,.0f}", value_style=ret_style, palette=palette),
        _metric_cell(
            "Latent / Réalisé / Frais",
            f"{_fmt_signed_compact_float(unrealized, decimals=0)} / "
            f"{_fmt_signed_compact_float(realized, decimals=0)} / "
            f"{_fmt_compact_float(fees, decimals=0)}",
            value_style=palette["kpi_default"],
            palette=palette,
        ),
    )

    # 2. Equity chart — pleine largeur, ligne dédiée
    chart = build_equity_chart(_extract_equity_curve(state), palette=palette, width=64, height=8)

    # 3. Allocation + Contrib côte à côte — cellules single-line (no_wrap + overflow="crop")
    total_notional = sum(_holding_notional(h) for h in holdings)
    max_abs_pnl = max((abs(_holding_pnl(h)) for h in holdings[:4]), default=0.0)

    alloc = Table(title="Allocation", show_header=False, expand=True, box=None, pad_edge=False)
    alloc.add_column("sym", no_wrap=True, overflow="crop")
    alloc.add_column("bar", no_wrap=True, overflow="crop")
    for holding in holdings[:5]:
        notional = _holding_notional(holding)
        pct = notional / total_notional * 100.0 if total_notional else 0.0
        alloc.add_row(
            _holding_symbol(holding),
            Text.assemble(
                styled_bar(notional, total_notional, width=8, style=palette["status_accent"]),
                (f" {pct:>4.1f}%", palette["dim"]),
            ),
        )

    contrib = Table(title="Contrib PnL", show_header=False, expand=True, box=None, pad_edge=False)
    contrib.add_column("sym", no_wrap=True, overflow="crop")
    contrib.add_column("bar", no_wrap=True, overflow="crop")
    for holding in sorted(holdings, key=lambda h: abs(_holding_pnl(h)), reverse=True)[:4]:
        value = _holding_pnl(holding)
        style = palette["pnl_positive"] if value >= 0 else palette["pnl_negative"]
        contrib.add_row(
            _holding_symbol(holding),
            Text.assemble(
                styled_bar(value, max_abs_pnl, width=8, style=style, signed=True),
                (f" {_fmt_signed_compact_float(value, decimals=0)}", style),
            ),
        )

    side_by_side = Table.grid(expand=True)
    side_by_side.add_column(ratio=1)
    side_by_side.add_column(ratio=1)
    side_by_side.add_row(alloc, contrib)

    return Group(header, chart, side_by_side)


_SERIES_STYLE_KEYS: dict[str, str] = {
    "exec": "pnl_positive",
    "veille": "status_accent",
    "plan": "status_accent",
    "risk": "pnl_negative",
    "stale": "kpi_vol_warn",
    "hold": "dim",
}


def build_activity_tile(buckets: dict[str, list[int]], *, palette: Palette) -> RenderableType:
    """Une sparkline par état + compteur — la vue temporelle façon Gonzo."""
    table = Table(show_header=False, expand=True, box=None, pad_edge=False)
    table.add_column("état", no_wrap=True)
    table.add_column("60min", no_wrap=True)
    table.add_column("n", justify="right", no_wrap=True)
    for state_name in ACTIVITY_STATES:
        counts = buckets.get(state_name) or []
        total = sum(counts)
        style = palette[_SERIES_STYLE_KEYS.get(state_name, "dim")]
        if total > 0:
            spark = sparkline([float(c) for c in counts])
            row_style = style
        else:
            spark = "·" * len(counts)
            row_style = palette["dim"]
        table.add_row(
            Text(state_name, style=row_style),
            Text(spark, style=row_style),
            Text(f"n={total}", style=row_style),
        )
    return table


def build_plans_tile(
    state: dict, *, palette: Palette, now: datetime | None = None
) -> RenderableType:
    """Table unifiée armés → sorties (par risque) → veilles (par expiration)."""
    now = now or datetime.now(UTC)
    state = state if isinstance(state, dict) else {}
    armed = _safe_list_of_dicts(state.get("armed_plans"))
    trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
    watches = [
        w for w in _safe_list_of_dicts(state.get("indicator_watches"))
        if not _is_armed_plan(w)
    ]

    table = Table(show_header=True, expand=True, box=None, pad_edge=False)
    for column in ("Type", "Sym", "Détail", "Déclencheur / Risque", "Exp."):
        table.add_column(column, no_wrap=(column != "Déclencheur / Risque"),
                         overflow="fold" if column == "Déclencheur / Risque" else "ellipsis")

    for watch in armed:
        table.add_row(
            Text("armé", style=f"bold {palette['status_accent']}"),
            Text(str(watch.get("symbol") or "—"), style="bold"),
            f"{_armed_order_label(watch)} {_armed_stop_label(watch)}",
            _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
            _relative_expiry(watch.get("expires_at"), now=now),
        )

    exit_rows = []
    for plan in trade_plans:
        symbol = str(plan.get("symbol") or "—")
        price = _price_for_symbol(state, symbol)
        stale = _symbol_is_stale(state, symbol)
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        reference = price or _safe_float(plan.get("entry_price"), default=None)
        side = str(plan.get("side") or "LONG").upper()
        direction = -1.0 if side == "SHORT" else 1.0
        if stop is not None and reference:
            stop_distance = abs((stop - reference) / reference * direction)
        else:
            stop_distance = float("inf")  # sans stop/prix → en queue des non-stale
        exit_rows.append((0 if stale else 1, stop_distance, symbol, plan, price, stale))
    for _, _, symbol, plan, price, stale in sorted(exit_rows, key=lambda r: (r[0], r[1], r[2]))[:8]:
        risk_label = _exit_plan_risk_label(plan, price, stale)
        table.add_row(
            Text("sortie", style=palette["kpi_default"]),
            Text(symbol, style="bold"),
            f"{_plan_qty_label(plan)} · {_plan_state_label(plan)}",
            Text(f"{risk_label} · {_next_tp_label(plan, price)}",
                 style=palette["kpi_vol_warn"] if stale else palette["dim"]),
            "—",
        )
    if len(exit_rows) > 8:
        table.add_row(Text("sortie", style=palette["dim"]), Text(f"+{len(exit_rows) - 8}",
                      style=palette["dim"]), "…", "", "")

    for watch in sorted(watches, key=lambda w: str(w.get("expires_at") or ""))[:5]:
        table.add_row(
            Text("veille", style=palette["dim"]),
            Text(str(watch.get("symbol") or "—"), style="bold"),
            "WAKE",
            _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
            _relative_expiry(watch.get("expires_at"), now=now),
        )
    if len(watches) > 5:
        table.add_row(Text("veille", style=palette["dim"]),
                      Text(f"+{len(watches) - 5}", style=palette["dim"]), "…", "", "")
    if not (armed or exit_rows or watches):
        table.add_row("—", "—", "aucun plan", "", "")
    return table


def build_decisions_tile(
    decisions: list[dict], recent_decisions: list[dict], *, palette: Palette
) -> RenderableType:
    """Triage statique (remplacé par DataTable en Task 10)."""
    table = Table(show_header=True, expand=True, box=None, pad_edge=False)
    for column in ("UTC", "Sym", "Act", "État", "Conf", "Suite"):
        table.add_column(column, no_wrap=(column != "Suite"),
                         overflow="fold" if column == "Suite" else "ellipsis")
    for row in select_decision_rows(decisions, recent_decisions, limit=8):
        action = str(row.get("action") or "—").upper()
        status = decision_status(row)
        confidence = _safe_float(row.get("confidence"), default=None)
        status_style = {
            "exec": palette["status_nominal"], "risk": palette["pnl_negative"],
            "stale": palette["kpi_vol_warn"], "armé": palette["status_accent"],
            "plan": palette["status_accent"], "veille": palette["status_accent"],
            "quiet": palette["dim"],
        }.get(status, palette["kpi_default"])
        action_style = (palette["action_buy"] if action == "BUY"
                        else palette["action_sell"] if action == "SELL" else palette["dim"])
        table.add_row(
            _decision_time_label(row),
            Text(str(row.get("symbol") or "—"), style="bold"),
            Text(action, style=action_style),
            Text(status, style=status_style),
            f"{confidence:.2f}" if confidence is not None else "—",
            _decision_effect_label(row),
        )
    if not table.rows:
        table.add_row("—", "—", "—", "—", "—", "—")
    return table


class SymbolChosen(Message):
    """Une ligne portant un symbole a été validée (Enter)."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__()


class _SymbolTable(DataTable):
    """Base : cursor row + Enter → SymbolChosen(symbol extrait de la row key)."""

    def on_mount(self) -> None:
        self.cursor_type = "row"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        raw = str(event.row_key.value or "")
        symbol = raw.split("|", 1)[0]
        if symbol and symbol != "—":
            self.post_message(SymbolChosen(symbol))


class DecisionsTable(_SymbolTable):
    def on_mount(self) -> None:
        super().on_mount()
        self.add_columns("UTC", "Sym", "Act", "État", "Conf", "Suite")

    def refresh_rows(
        self, decisions: list[dict], recent_decisions: list[dict], *, palette: Palette
    ) -> None:
        from trader.cockpit.aggregates import decision_status

        self.clear()
        rows = select_decision_rows(decisions, recent_decisions, limit=8)
        for index, row in enumerate(rows):
            symbol = str(row.get("symbol") or "—")
            action = str(row.get("action") or "—").upper()
            status = decision_status(row)
            confidence = _safe_float(row.get("confidence"), default=None)
            action_style = (palette["action_buy"] if action == "BUY"
                            else palette["action_sell"] if action == "SELL" else palette["dim"])
            status_style = {
                "exec": palette["status_nominal"], "risk": palette["pnl_negative"],
                "stale": palette["kpi_vol_warn"], "quiet": palette["dim"],
            }.get(status, palette["status_accent"])
            self.add_row(
                _decision_time_label(row),
                Text(symbol, style="bold"),
                Text(action, style=action_style),
                Text(status, style=status_style),
                f"{confidence:.2f}" if confidence is not None else "—",
                _decision_effect_label(row),
                key=f"{symbol}|{row.get('cycle_ts') or row.get('ts') or ''}|{index}",
            )
        if not rows:
            self.add_row("—", "—", "—", "—", "—", "—", key="—")


class PlansTable(_SymbolTable):
    def on_mount(self) -> None:
        super().on_mount()
        self.add_columns("Type", "Sym", "Détail", "Déclencheur / Risque", "Exp.")

    def refresh_rows(self, state: dict, *, palette: Palette, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.clear()
        armed = _safe_list_of_dicts(state.get("armed_plans"))
        trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
        watches = [w for w in _safe_list_of_dicts(state.get("indicator_watches"))
                   if not _is_armed_plan(w)]
        index = 0
        for watch in armed:
            symbol = str(watch.get("symbol") or "—")
            self.add_row(
                Text("armé", style=f"bold {palette['status_accent']}"),
                Text(symbol, style="bold"),
                f"{_armed_order_label(watch)} {_armed_stop_label(watch)}",
                _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
                _relative_expiry(watch.get("expires_at"), now=now),
                key=f"{symbol}|armed|{index}",
            )
            index += 1
        exit_rows = []
        for plan in trade_plans:
            symbol = str(plan.get("symbol") or "—")
            price = _price_for_symbol(state, symbol)
            stale = _symbol_is_stale(state, symbol)
            exit_rows.append((0 if stale else 1, symbol, plan, price, stale))
        for _, symbol, plan, price, stale in sorted(exit_rows, key=lambda r: (r[0], r[1]))[:8]:
            self.add_row(
                Text("sortie", style=palette["kpi_default"]),
                Text(symbol, style="bold"),
                f"{_plan_qty_label(plan)} · {_plan_state_label(plan)}",
                Text(f"{_exit_plan_risk_label(plan, price, stale)} · {_next_tp_label(plan, price)}",
                     style=palette["kpi_vol_warn"] if stale else palette["dim"]),
                "—",
                key=f"{symbol}|exit|{index}",
            )
            index += 1
        for watch in sorted(watches, key=lambda w: str(w.get("expires_at") or ""))[:5]:
            symbol = str(watch.get("symbol") or "—")
            self.add_row(
                Text("veille", style=palette["dim"]),
                Text(symbol, style="bold"),
                "WAKE",
                _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
                _relative_expiry(watch.get("expires_at"), now=now),
                key=f"{symbol}|watch|{index}",
            )
            index += 1
        if not (armed or exit_rows or watches):
            self.add_row("—", "—", "aucun plan", "", "", key="—")


class PositionsTable(_SymbolTable):
    def on_mount(self) -> None:
        super().on_mount()
        self.add_columns("Sym", "Dev.", "Qté", "Dernier", "Expo USD", "PnL USD",
                         "Stop", "Perte@stop")

    def refresh_rows(self, state: dict, *, palette: Palette) -> None:
        self.clear()
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
        holdings = sorted(
            _safe_list_of_dicts(portfolio.get("holdings")),
            key=lambda item: max(abs(_holding_pnl(item)), _holding_notional(item)),
            reverse=True,
        )
        for index, holding in enumerate(holdings[:5]):
            symbol = _holding_symbol(holding)
            pnl_value = _holding_pnl(holding)
            pnl_style = palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"]
            stop_label, dist_label, loss_label, state_label = _stop_risk_for_holding(
                holding, _plan_for_symbol(trade_plans, symbol)
            )
            self.add_row(
                Text(symbol, style="bold"),
                _holding_currency(holding),
                _fmt_compact_float(holding.get("quantity"), decimals=2),
                _fmt_compact_float(_holding_native_price(holding), decimals=2),
                _fmt_compact_float(_holding_notional(holding), decimals=0),
                Text(_fmt_signed_compact_float(pnl_value, decimals=0), style=pnl_style),
                f"{stop_label} {dist_label}",
                Text(loss_label, style=palette["pnl_negative"]
                     if state_label == "risque" else palette["dim"]),
                key=f"{symbol}|pos|{index}",
            )
        if not holdings:
            self.add_row("—", "—", "—", "—", "—", "—", "—", "—", key="—")


class HomePane(Static):
    """Home dense 1 écran — grammaire Gonzo (spec §4)."""

    DEFAULT_CSS = """
    HomePane {
        height: 100%;
        width: 100%;
        overflow-y: hidden;
        layout: vertical;
        padding: 0 1;
    }
    #home-top-row { height: 3fr; width: 100%; layout: horizontal; }
    #home-bottom-row { height: 2fr; width: 100%; layout: horizontal; }
    #home-portfolio { width: 2fr; height: 100%; border: solid $primary; padding: 0 1; }
    #home-portfolio Static { height: auto; }
    #home-positions { height: 1fr; }
    #home-activity { width: 1fr; height: 100%; border: solid $primary; padding: 0 1; }
    #home-decisions { width: 2fr; height: 100%; border: solid $primary; }
    #home-plans { width: 2fr; height: 100%; border: solid $primary; }
    #home-flux { width: 3fr; height: 100%; }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        # Import tardif OBLIGATOIRE : app.py importe home.py en tête de module,
        # un import module-level de app ici créerait un cycle.
        from trader.cockpit.app import FluxPane

        with Horizontal(id="home-top-row"):
            with Vertical(id="home-portfolio"):
                yield Static(id="home-portfolio-summary")
                yield PositionsTable(id="home-positions")
            yield Static(id="home-activity")
            yield DecisionsTable(id="home-decisions")
        with Horizontal(id="home-bottom-row"):
            yield PlansTable(id="home-plans")
            yield FluxPane(id="home-flux")

    def update_state(self, state: dict, kill_active: bool) -> None:  # kill_active : réservé (modal/kill overlay à venir) — compat interface _apply_state
        state = state if isinstance(state, dict) else {}
        palette = self._current_palette
        now = datetime.now(UTC)
        recent = state.get("recent_decisions") if isinstance(state.get("recent_decisions"), list) else []
        decisions = _safe_list_of_dicts(state.get("decisions"))
        self.query_one("#home-portfolio-summary", Static).update(
            build_portfolio_summary(state, palette=palette)
        )
        self.query_one("#home-activity", Static).update(
            build_activity_tile(activity_buckets(recent, now), palette=palette)
        )
        self.query_one("#home-decisions", DecisionsTable).refresh_rows(
            decisions, recent, palette=palette
        )
        self.query_one("#home-plans", PlansTable).refresh_rows(
            state, palette=palette, now=now
        )
        self.query_one("#home-positions", PositionsTable).refresh_rows(
            state, palette=palette
        )


def build_symbol_detail(state: dict, symbol: str, *, palette: Palette) -> RenderableType:
    """Contexte complet d'un symbole — pur filtrage du read model en mémoire."""
    state = state if isinstance(state, dict) else {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    holdings = [h for h in _safe_list_of_dicts(portfolio.get("holdings"))
                if _holding_symbol(h) == symbol]
    plans = [p for p in _safe_list_of_dicts(state.get("trade_plans"))
             if str(p.get("symbol") or "") == symbol]
    armed = [w for w in _safe_list_of_dicts(state.get("armed_plans"))
             if str(w.get("symbol") or "") == symbol]
    decisions = [d for d in _safe_list_of_dicts(state.get("recent_decisions"))
                 if str(d.get("symbol") or "") == symbol][-8:]
    learnings = [l for l in _safe_list_of_dicts(state.get("learnings"))
                 if str(l.get("symbol") or "") == symbol]
    streak = (state.get("stale_streaks") or {}).get(symbol, 0)

    parts: list[RenderableType] = [Text(symbol, style=f"bold {palette['status_equity']}")]

    if holdings:
        holding = holdings[0]
        pnl_value = _holding_pnl(holding)
        pnl_style = palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"]
        parts.append(Text.assemble(
            ("Position : ", palette["dim"]),
            (f"{_fmt_compact_float(holding.get('quantity'), decimals=2)} @ "
             f"{_fmt_compact_float(_holding_native_price(holding), decimals=2)} "
             f"{_holding_currency(holding)}", "bold"),
            ("  PnL ", palette["dim"]),
            (_fmt_signed_compact_float(pnl_value, decimals=0), pnl_style),
        ))
    else:
        parts.append(Text("Aucune position ouverte", style=palette["dim"]))

    if plans:
        parts.append(_build_exit_plans_enriched(plans, palette=palette))
    if armed:
        parts.append(Text(f"{len(armed)} plan(s) armé(s) : "
                          + ", ".join(_armed_order_label(w) for w in armed),
                          style=palette["status_accent"]))

    table = Table(title="Décisions récentes", show_header=True, expand=True, box=None)
    for column in ("UTC", "Act", "État", "Conf", "Suite"):
        table.add_column(column, overflow="fold" if column == "Suite" else "ellipsis")
    from trader.cockpit.aggregates import decision_status

    for row in decisions:
        confidence = _safe_float(row.get("confidence"), default=None)
        table.add_row(
            _decision_time_label(row),
            str(row.get("action") or "—").upper(),
            decision_status(row),
            f"{confidence:.2f}" if confidence is not None else "—",
            _decision_effect_label(row),
        )
    if not decisions:
        table.add_row("—", "—", "—", "—", "aucune décision récente")
    parts.append(table)

    learnings_panel = _build_learnings_panel(learnings, palette=palette)
    if learnings_panel is not None:
        parts.append(learnings_panel)

    health = Text.assemble(("Santé data : ", palette["dim"]))
    if streak:
        health.append(f"stale ×{int(streak)}", style=palette["kpi_vol_warn"])
    else:
        health.append("OK", style=palette["status_nominal"])
    parts.append(health)

    return Group(*parts)


class SymbolDetailScreen(ModalScreen[None]):
    """Drill-down symbole (Enter depuis une table de la home). Esc ferme."""

    BINDINGS = [Binding("escape", "close_detail", "Fermer")]
    DEFAULT_CSS = """
    SymbolDetailScreen { align: center middle; }
    SymbolDetailScreen > VerticalScroll {
        width: 90%;
        height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, symbol: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._symbol = symbol

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="symbol-detail-body")

    def on_mount(self) -> None:
        app = self.app
        state = getattr(app, "_last_state", None) or {}
        palette = app._current_palette()  # type: ignore[attr-defined]
        self.query_one("#symbol-detail-body", Static).update(
            build_symbol_detail(state, self._symbol, palette=palette)
        )

    def action_close_detail(self) -> None:
        self.dismiss(None)
