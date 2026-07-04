"""Home analytique du cockpit Textual.

Le shell Textual est dans ``trader.interfaces.cockpit.app``; ce module possède la home :
layout responsive et mini-artefacts Rich destinés aux tuiles analytiques.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from trader.market import fx
from trader.reporting.read_models.runtime_state import _safe_float, _safe_list_of_dicts
from trader.interfaces.ui.palette import PALETTE_DARK, PALETTE_LIGHT, Palette
from trader.interfaces.ui.rich_panels import _format_datetime
from trader.interfaces.cockpit import aggregates as _aggregates


def _build_attention_line(
    state: dict, *, kill_active: bool, palette: Palette = PALETTE_DARK
) -> Text:
    """Synthèse compacte des signaux qui méritent une attention humaine."""
    if not isinstance(state, dict):
        state = {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    holdings = _safe_list_of_dicts(portfolio.get("holdings"))
    armed_plans = _safe_list_of_dicts(state.get("armed_plans"))
    watches = _safe_list_of_dicts(state.get("indicator_watches"))
    recent_decisions = _safe_list_of_dicts(state.get("recent_decisions"))
    stale_streaks = (
        state.get("stale_streaks") if isinstance(state.get("stale_streaks"), dict) else {}
    )
    daemon_status = (
        state.get("daemon_status")
        if isinstance(state.get("daemon_status"), dict)
        else {}
    )

    stale_values = [
        int(value)
        for value in stale_streaks.values()
        if isinstance(value, int | float) and value > 0
    ]
    risk_count = _count_risk_rows(recent_decisions)
    phase = str(daemon_status.get("phase") or "—")
    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    progress = f"{done}/{total}" if done is not None and total is not None else "—"
    used = daemon_status.get("model_calls_used")
    limit = daemon_status.get("max_model_calls_per_cycle")
    calls = f"{used}/{limit}" if used is not None and limit is not None else "—"
    learnings_pending = state.get("learnings_pending_count") or 0
    halted = state.get("halted")

    items: list[tuple[str, str]] = []
    if kill_active:
        items.append(("KILL", "bold white on red"))
    if halted:
        items.append((f"HALT {halted}", palette["pnl_negative"]))
    items.append((f"phase {phase}", palette["status_phase"]))
    items.append((f"prog {progress}", palette["status_accent"]))
    if stale_values:
        items.append(
            (
                f"stale {len(stale_values)} max {max(stale_values)}",
                palette["kpi_vol_warn"],
            )
        )
    else:
        items.append(("data ok", palette["status_nominal"]))
    if risk_count:
        items.append((f"risk {risk_count}", palette["pnl_negative"]))
    items.extend(
        [
            (f"pos {len(holdings)}", palette["kpi_default"]),
            (f"plans {len(armed_plans)}", palette["kpi_default"]),
            (f"veilles {len(watches)}", palette["kpi_default"]),
            (f"LLM {calls}", palette["kpi_default"]),
            (f"learn {learnings_pending}", palette["dim"]),
        ]
    )

    text = Text("  ")
    for index, (label, style) in enumerate(items):
        if index:
            text.append("  |  ", style=palette["dim"])
        text.append(label, style=style)
    return text


def _overview_card(
    title: str,
    content: RenderableType,
    *,
    border_style: str,
) -> Panel:
    return Panel(
        content,
        title=f"[bold]{title}[/bold]",
        border_style=border_style,
        expand=True,
    )


def _count_risk_rows(rows: list[dict]) -> int:
    return sum(1 for row in rows if str(row.get("reason") or "").startswith("risk:"))


def _action_style(action: str, palette: Palette) -> str:
    upper = action.upper()
    if upper == "BUY":
        return palette["action_buy"]
    if upper == "SELL":
        return palette["action_sell"]
    return palette["dim"] if upper == "HOLD" else palette["kpi_default"]


def _fmt_compact_float(value: object, *, decimals: int = 2, default: str = "—") -> str:
    number = _safe_float(value, default=None)
    return f"{number:,.{decimals}f}" if number is not None else default


def _fmt_signed_compact_float(
    value: object, *, decimals: int = 2, default: str = "—"
) -> str:
    number = _safe_float(value, default=None)
    return f"{number:+,.{decimals}f}" if number is not None else default


def _fmt_pct_points(value: object, *, decimals: int = 1, default: str = "—") -> str:
    number = _safe_float(value, default=None)
    return f"{number:+.{decimals}f}%" if number is not None else default


def _clip_text(value: object, *, limit: int = 32) -> str:
    text = str(value or "—").replace("\n", " ")
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def _mini_line_chart(
    values: list[float],
    *,
    width: int = 46,
    height: int = 7,
    palette: Palette = PALETTE_LIGHT,
) -> RenderableType:
    """Petit graphe multi-ligne pour éviter la sparkline trop plate."""
    clean = [value for value in values if value == value]
    if not clean:
        return Text("aucune courbe", style=palette["dim"])
    series = clean[-width:]
    lo = min(series)
    hi = max(series)
    if hi == lo:
        return Text("\n".join(["─" * len(series)] * max(1, height)), style=palette["dim"])
    rows: list[str] = []
    for row in range(height):
        threshold = hi - (hi - lo) * row / max(1, height - 1)
        line = "".join("█" if value >= threshold else " " for value in series)
        rows.append(line.rstrip() or " ")
    label = f"{lo:,.0f} → {hi:,.0f}"
    return Text.assemble(
        (label, palette["dim"]),
        "\n",
        ("\n".join(rows), f"bold {palette['equity_line']}"),
    )


def _bar(value: float, total: float, *, width: int = 12) -> str:
    if total <= 0:
        return "·" * width
    filled = max(0, min(width, round(abs(value) / total * width)))
    return "█" * filled + "░" * (width - filled)


def _signed_bar(value: float, total_abs: float, *, width: int = 12) -> str:
    if total_abs <= 0:
        return "·" * width
    filled = max(0, min(width, round(abs(value) / total_abs * width)))
    prefix = "+" if value >= 0 else "-"
    return prefix + "█" * filled + "░" * (width - filled)


def _safe_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _holding_symbol(holding: dict) -> str:
    return str(holding.get("symbol") or "—")


def _holding_currency(holding: dict) -> str:
    symbol = _holding_symbol(holding)
    try:
        return fx.currency_for(symbol)
    except Exception:
        return "USD"


def _holding_quantity(holding: dict) -> float:
    return _safe_float(holding.get("quantity"), default=0.0) or 0.0


def _holding_native_price(holding: dict) -> float | None:
    return _safe_float(holding.get("last_price"), default=None) or _safe_float(
        holding.get("avg_price"), default=None
    )


def _holding_notional(holding: dict) -> float:
    qty = _holding_quantity(holding)
    price = _holding_native_price(holding) or 0.0
    fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0
    return abs(qty * price * fx_rate)


def _holding_pnl(holding: dict) -> float:
    net = _safe_float(holding.get("unrealized_pnl_net"), default=None)
    if net is not None:
        return net
    return _safe_float(holding.get("unrealized_pnl"), default=0.0) or 0.0


def _extract_equity_curve(state: dict) -> list[float]:
    return [
        value
        for value in (_safe_float(item, default=None) for item in (state.get("equity_curve") or []))
        if value is not None
    ]


def _metric_cell(label: str, value: str, *, value_style: str, palette: Palette) -> Text:
    return Text.assemble((f"{label}\n", palette["dim"]), (value, value_style))


def _first_take_profit(plan: dict) -> float | None:
    take_profits = _safe_list_of_dicts(plan.get("take_profits"))
    for take_profit in take_profits:
        price = _safe_float(take_profit.get("price"), default=None)
        if price is not None:
            return price
    return None


def _plan_for_symbol(trade_plans: list[dict], symbol: str) -> dict:
    for plan in trade_plans:
        if str(plan.get("symbol") or "") == symbol:
            return plan
    return {}


def _stop_risk_for_holding(holding: dict, plan: dict) -> tuple[str, str, str, str]:
    if not plan:
        return "—", "—", "—", "sans plan"

    stop = _safe_float(plan.get("hard_stop_price"), default=None)
    reference = _holding_native_price(holding) or _safe_float(plan.get("entry_price"), default=None)
    qty = _safe_float(plan.get("remaining_quantity"), default=None)
    if qty is None:
        qty = _safe_float(plan.get("quantity"), default=None)
    if qty is None:
        qty = abs(_holding_quantity(holding))
    side = str(plan.get("side") or "LONG").upper()
    direction = -1.0 if side == "SHORT" else 1.0
    fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0

    stop_label = _fmt_compact_float(stop, decimals=2)
    if stop is None or reference is None or reference == 0:
        return stop_label, "—", "—", "plan incomplet"

    distance_pct = (stop - reference) / reference * 100.0 * direction
    risk_usd = (stop - reference) * qty * direction * fx_rate
    state = "protégé" if risk_usd >= 0 else "risque"
    return (
        stop_label,
        _fmt_pct_points(distance_pct, decimals=1),
        _fmt_signed_compact_float(risk_usd, decimals=0),
        state,
    )


def _build_portfolio_overview_tile(
    state: dict,
    *,
    palette: Palette,
) -> RenderableType:
    state = state if isinstance(state, dict) else {}
    portfolio = _safe_dict(state.get("portfolio"))
    kpis = _safe_dict(state.get("kpis"))
    attribution = _safe_dict(state.get("attribution"))
    trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
    equity_curve = _extract_equity_curve(state)
    cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
    equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
    starting_cash = _safe_float(state.get("starting_cash"), default=cash) or cash
    pnl = equity - starting_cash
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        ret_pct = (_safe_float(kpis.get("total_return"), default=0.0) or 0.0) * 100.0
    ret_style = palette["pnl_positive"] if ret_pct >= 0 else palette["pnl_negative"]
    pnl_style = palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]
    holdings = sorted(
        _safe_list_of_dicts(portfolio.get("holdings")),
        key=lambda item: max(abs(_holding_pnl(item)), _holding_notional(item)),
        reverse=True,
    )
    total_notional = sum(_holding_notional(item) for item in holdings)
    cash_pct = cash / equity * 100.0 if equity else 0.0
    unrealized_total = sum(_holding_pnl(holding) for holding in holdings)
    realized = _safe_float(attribution.get("realized_pnl"), default=None)
    fees = _safe_float(attribution.get("total_commissions"), default=None)
    closed_trades = attribution.get("n_closed_trades")

    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(ratio=1)
    header.add_column(ratio=1)
    header.add_column(ratio=1)
    header.add_row(
        _metric_cell("Équité locale", f"{equity:,.0f}", value_style=f"bold {palette['status_equity']}", palette=palette),
        _metric_cell("Cash", f"{cash:,.0f}", value_style=f"bold {palette['status_equity']}", palette=palette),
        _metric_cell("Cash %", f"{cash_pct:.1f}%", value_style=palette["kpi_default"], palette=palette),
        _metric_cell("P&L total", f"{ret_pct:+.2f}%  {pnl:+,.0f}", value_style=ret_style or pnl_style, palette=palette),
    )
    header.add_row(
        _metric_cell(
            "PnL latent USD",
            _fmt_signed_compact_float(unrealized_total, decimals=0),
            value_style=palette["pnl_positive"] if unrealized_total >= 0 else palette["pnl_negative"],
            palette=palette,
        ),
        _metric_cell(
            "Réalisé USD",
            _fmt_signed_compact_float(realized, decimals=0),
            value_style=palette["pnl_positive"] if (realized or 0.0) >= 0 else palette["pnl_negative"],
            palette=palette,
        ),
        _metric_cell(
            "Frais USD",
            _fmt_compact_float(fees, decimals=0),
            value_style=palette["dim"],
            palette=palette,
        ),
        _metric_cell("Trades clos", str(closed_trades or 0), value_style=palette["kpi_default"], palette=palette),
    )

    allocation = Table(title="Allocation symboles", show_header=True, expand=True, box=None)
    allocation.add_column("Sym", no_wrap=True)
    allocation.add_column("Dev.", no_wrap=True)
    allocation.add_column("Poids", overflow="fold")
    for holding in sorted(holdings, key=_holding_notional, reverse=True)[:5]:
        notional = _holding_notional(holding)
        pct = notional / total_notional * 100.0 if total_notional else 0.0
        allocation.add_row(
            _holding_symbol(holding),
            _holding_currency(holding),
            f"{_bar(notional, total_notional, width=14)} {pct:>4.1f}%",
        )
    if not holdings:
        allocation.add_row("—", "—", "—")

    pnl_rows = sorted(holdings, key=lambda item: abs(_holding_pnl(item)), reverse=True)[:4]
    max_abs_pnl = max((abs(_holding_pnl(item)) for item in pnl_rows), default=0.0)
    contributors = Table(title="Contrib PnL", show_header=True, expand=True, box=None)
    contributors.add_column("Sym", no_wrap=True)
    contributors.add_column("Barre", overflow="fold")
    contributors.add_column("USD", justify="right", no_wrap=True)
    for holding in pnl_rows:
        pnl_value = _holding_pnl(holding)
        contributors.add_row(
            _holding_symbol(holding),
            _signed_bar(pnl_value, max_abs_pnl, width=12),
            Text(
                _fmt_signed_compact_float(pnl_value, decimals=0),
                style=palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"],
            ),
        )
    if not pnl_rows:
        contributors.add_row("—", "—", "—")

    charts = Table.grid(expand=True)
    charts.add_column(ratio=2)
    charts.add_column(ratio=1)
    charts.add_column(ratio=1)
    charts.add_row(
        Group(Text("Courbe équité", style=palette["dim"]), _mini_line_chart(equity_curve, palette=palette)),
        allocation,
        contributors,
    )

    positions = Table(title="Top positions", show_header=True, expand=True, box=None)
    positions.add_column("Sym", style="bold", no_wrap=True)
    positions.add_column("Dev.", no_wrap=True)
    positions.add_column("Qté", justify="right", no_wrap=True)
    positions.add_column("Dernier natif", justify="right", no_wrap=True)
    positions.add_column("Expo USD", justify="right", no_wrap=True)
    positions.add_column("PnL latent USD", justify="right", no_wrap=True)
    positions.add_column("FX→USD", justify="right", no_wrap=True)
    for holding in holdings[:5]:
        pnl_value = _holding_pnl(holding)
        notional = _holding_notional(holding)
        positions.add_row(
            _holding_symbol(holding),
            _holding_currency(holding),
            _fmt_compact_float(holding.get("quantity"), decimals=2),
            _fmt_compact_float(_holding_native_price(holding), decimals=2),
            _fmt_compact_float(notional, decimals=0),
            Text(
                _fmt_signed_compact_float(pnl_value, decimals=0),
                style=palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"],
            ),
            _fmt_compact_float(holding.get("fx_rate"), decimals=4),
        )
    if not holdings:
        positions.add_row("—", "—", "—", "—", "—", "—", "—")

    risk_table = Table(title="Risque sorties", show_header=True, expand=True, box=None)
    risk_table.add_column("Sym", style="bold", no_wrap=True)
    risk_table.add_column("Stop natif", justify="right", no_wrap=True)
    risk_table.add_column("Dist.", justify="right", no_wrap=True)
    risk_table.add_column("Perte stop USD", justify="right", no_wrap=True)
    risk_table.add_column("État", overflow="fold")
    for holding in holdings[:4]:
        symbol = _holding_symbol(holding)
        stop_label, dist_label, loss_label, state_label = _stop_risk_for_holding(
            holding,
            _plan_for_symbol(trade_plans, symbol),
        )
        style = palette["pnl_negative"] if state_label == "risque" else palette["dim"]
        risk_table.add_row(
            symbol,
            stop_label,
            dist_label,
            Text(loss_label, style=style),
            state_label,
        )
    if not holdings:
        risk_table.add_row("—", "—", "—", "—", "aucune position")

    return Group(header, charts, positions, risk_table)


def _decision_status(row: dict) -> str:
    return _aggregates.decision_status(row)


def _decision_priority(row: dict) -> tuple[int, str]:
    return _aggregates.decision_priority(row)


def _decision_source_label(row: dict) -> str:
    runtime = _safe_dict(row.get("runtime"))
    provider = row.get("llm_provider") or runtime.get("llm_provider")
    model = row.get("llm_model") or runtime.get("llm_model")
    data_source = row.get("data_source") or runtime.get("data_source")
    source = row.get("decision_source")
    if provider or model:
        return _clip_text(f"{provider or 'llm'}:{model or '?'}", limit=18)
    if source:
        return _clip_text(source, limit=18)
    return _clip_text(data_source, limit=18)


def _decision_time_label(row: dict) -> str:
    raw = str(row.get("cycle_ts") or row.get("ts") or "")
    if not raw:
        return "—"
    try:
        candidate = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).strftime("%H:%M")
    except ValueError:
        return raw[:5] or "—"


def _decision_effect_label(row: dict) -> str:
    runtime = _safe_dict(row.get("runtime"))
    if row.get("executed") is True:
        qty = row.get("qty") or row.get("quantity")
        price = _safe_float(row.get("price"), default=None)
        qty_part = f" {qty}" if qty is not None else ""
        price_part = f" @ {_fmt_compact_float(price, decimals=2)}" if price is not None else ""
        return _clip_text(f"ordre{qty_part}{price_part}", limit=30)
    if runtime.get("trade_plan_created"):
        return "plan créé"
    if runtime.get("indicator_watch_created"):
        return "veille créée"
    if runtime.get("next_wake_requested"):
        return "wake demandé"
    reason = row.get("reason") or row.get("rationale")
    return _clip_text(reason, limit=34)


def _decision_identity(row: dict) -> tuple[str, str, str, str, str]:
    return _aggregates._decision_identity(row)


def _decision_source_rows(decisions: list[dict], recent_decisions: list[dict]) -> list[dict]:
    return _aggregates.decision_source_rows(decisions, recent_decisions)


def _select_decision_rows(decisions: list[dict], recent_decisions: list[dict], *, limit: int) -> list[dict]:
    return _aggregates.select_decision_rows(decisions, recent_decisions, limit=limit)


def _build_decisions_overview_tile(
    decisions: list[dict],
    recent_decisions: list[dict],
    daemon_status: dict,
    *,
    palette: Palette,
) -> RenderableType:
    selected_rows = _select_decision_rows(decisions, recent_decisions, limit=6)
    source_rows = _decision_source_rows(decisions, recent_decisions)
    risk_count = _count_risk_rows(recent_decisions)
    calls_used = daemon_status.get("model_calls_used")
    calls_max = daemon_status.get("max_model_calls_per_cycle")
    calls = (
        f"{calls_used}/{calls_max}"
        if calls_used is not None and calls_max is not None
        else "—"
    )
    action_counts = {
        action: sum(
            1
            for row in source_rows
            if str(row.get("action") or "").upper() == action
        )
        for action in ("BUY", "SELL", "HOLD")
    }
    total_actions = sum(action_counts.values())
    mix = Text.assemble(
        ("Phase ", palette["dim"]),
        (str(daemon_status.get("phase") or "—"), palette["status_phase"]),
        ("  Appels ", palette["dim"]),
        (calls, palette["kpi_default"]),
        ("  Risk ", palette["dim"]),
        (str(risk_count), palette["pnl_negative"] if risk_count else palette["status_nominal"]),
        "\n",
        ("BUY ", palette["dim"]),
        (_bar(action_counts["BUY"], max(1, total_actions), width=8), palette["action_buy"]),
        (" SELL ", palette["dim"]),
        (_bar(action_counts["SELL"], max(1, total_actions), width=8), palette["action_sell"]),
        (" HOLD ", palette["dim"]),
        (_bar(action_counts["HOLD"], max(1, total_actions), width=8), palette["dim"]),
    )

    table = Table(
        title="Triage décisions · Décisions récentes",
        show_header=True,
        expand=True,
        box=None,
    )
    table.add_column("UTC", no_wrap=True, style=palette["dim"])
    table.add_column("Sym", style="bold", no_wrap=True)
    table.add_column("Act", no_wrap=True)
    table.add_column("État", no_wrap=True)
    table.add_column("Conf", justify="right", no_wrap=True)
    table.add_column("Src", no_wrap=True)
    table.add_column("Suite", overflow="fold")
    for decision in selected_rows:
        action = str(decision.get("action") or "—").upper()
        confidence = _safe_float(decision.get("confidence"), default=None)
        status = _decision_status(decision)
        status_style = {
            "exec": palette["status_nominal"],
            "risk": palette["pnl_negative"],
            "stale": palette["kpi_vol_warn"],
            "armé": palette["status_accent"],
            "plan": palette["status_accent"],
            "veille": palette["status_accent"],
            "quiet": palette["dim"],
        }.get(status, palette["kpi_default"])
        table.add_row(
            _decision_time_label(decision),
            str(decision.get("symbol") or "—"),
            Text(action, style=_action_style(action, palette)),
            Text(status, style=status_style),
            f"{confidence:.2f}" if confidence is not None else "—",
            _decision_source_label(decision),
            _decision_effect_label(decision),
        )
    if not selected_rows:
        table.add_row("—", "—", "—", "—", "—", "—", "—")

    risk_table = Table(title="Rejets récents", show_header=True, expand=True, box=None)
    risk_table.add_column("Sym", no_wrap=True)
    risk_table.add_column("Cause", overflow="fold")
    risks = [
        row
        for row in recent_decisions[-8:]
        if str(row.get("reason") or "").startswith("risk:")
    ]
    for row in risks[-3:]:
        risk_table.add_row(
            str(row.get("symbol") or "—"),
            str(row.get("reason") or "—").removeprefix("risk:"),
        )
    if not risks:
        risk_table.add_row("—", "aucun rejet risk")

    return Group(mix, table, risk_table)


def _price_for_symbol(state: dict, symbol: str) -> float | None:
    prices = _safe_dict(state.get("prices"))
    raw = prices.get(symbol)
    if isinstance(raw, dict):
        for key in ("price", "last", "last_price", "close"):
            price = _safe_float(raw.get(key), default=None)
            if price is not None:
                return price
        return None
    return _safe_float(raw, default=None)


def _symbol_is_stale(state: dict, symbol: str) -> bool:
    stale_market_data = _safe_dict(state.get("stale_market_data"))
    stale_streaks = _safe_dict(state.get("stale_streaks"))
    return bool(stale_market_data.get(symbol)) or (
        (_safe_float(stale_streaks.get(symbol), default=0.0) or 0.0) > 0.0
    )


def _condition_summary(conditions: object, logic: object, *, max_items: int = 2) -> str:
    rows = _safe_list_of_dicts(conditions)
    parts: list[str] = []
    for condition in rows[:max_items]:
        indicator = str(condition.get("indicator") or "?")
        op = str(condition.get("op") or "?")
        value = condition.get("value")
        timeframe = str(condition.get("timeframe") or condition.get("interval") or "?")
        parts.append(f"{indicator}{op}{value if value is not None else '?'}@{timeframe}")
    if not parts:
        return "—"
    if len(rows) > max_items:
        parts.append(f"+{len(rows) - max_items}")
    separator = f" {logic or 'any'} "
    return _clip_text(separator.join(parts), limit=36)


def _relative_expiry(expires_raw: object, *, now: datetime | None = None) -> str:
    raw = str(expires_raw or "")
    if not raw:
        return "—"
    try:
        candidate = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
        expires_at = datetime.fromisoformat(candidate)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        now = now or datetime.now(UTC)
        total_secs = int((expires_at - now).total_seconds())
    except ValueError:
        return "?"
    if total_secs < 0:
        return "expiré"
    hours, rem = divmod(total_secs, 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}min"


def _plan_qty_label(plan: dict) -> str:
    side = str(plan.get("side") or plan.get("action") or "?").upper()
    qty = (
        _safe_float(plan.get("remaining_quantity"), default=None)
        or _safe_float(plan.get("quantity"), default=None)
        or _safe_float(plan.get("qty"), default=None)
    )
    qty_label = f" {qty:g}" if qty is not None else ""
    return _clip_text(f"{side}{qty_label}", limit=18)


def _exit_plan_risk_label(plan: dict, price: float | None, stale: bool) -> str:
    if stale:
        return "stale"
    stop = _safe_float(plan.get("hard_stop_price"), default=None)
    reference = price or _safe_float(plan.get("entry_price"), default=None)
    if stop is None:
        return "stop —"
    if reference is None or reference == 0:
        return f"stop {_fmt_compact_float(stop, decimals=2)}"
    side = str(plan.get("side") or "LONG").upper()
    direction = -1.0 if side == "SHORT" else 1.0
    distance = (stop - reference) / reference * 100.0 * direction
    return f"stop {_fmt_pct_points(distance, decimals=1)}"


def _next_tp_label(plan: dict, price: float | None) -> str:
    tp_price = _first_take_profit(plan)
    if tp_price is None:
        watch = _safe_dict(plan.get("exit_watch"))
        if watch:
            return _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=1)
        return "TP —"
    reference = price or _safe_float(plan.get("entry_price"), default=None)
    if reference is None or reference == 0:
        return f"TP {_fmt_compact_float(tp_price, decimals=2)}"
    side = str(plan.get("side") or "LONG").upper()
    direction = -1.0 if side == "SHORT" else 1.0
    distance = (tp_price - reference) / reference * 100.0 * direction
    return f"TP {_fmt_pct_points(distance, decimals=1)}"


def _plan_state_label(plan: dict) -> str:
    if plan.get("trailing_stop"):
        return "trail"
    if plan.get("profit_protection"):
        return "prot"
    if plan.get("last_llm_review"):
        return "rev ok"
    if not _safe_dict(plan.get("exit_watch")):
        return "no-watch"
    return "ouvert"


def _armed_order_label(watch: dict) -> str:
    order = _safe_dict(watch.get("order"))
    action = str(order.get("action") or order.get("intent") or "ORDER").upper()
    qty = _safe_float(order.get("qty"), default=None)
    qty_label = f" {qty:g}" if qty is not None else ""
    return _clip_text(f"{action}{qty_label}", limit=18)


def _armed_stop_label(watch: dict) -> str:
    order = _safe_dict(watch.get("order"))
    exit_plan = _safe_dict(order.get("exit_plan"))
    hard_stop = exit_plan.get("hard_stop")
    if isinstance(hard_stop, dict):
        hard_stop = hard_stop.get("price")
    stop = _safe_float(hard_stop, default=None)
    return f"stop {_fmt_compact_float(stop, decimals=2)}" if stop is not None else "stop —"


def _watch_is_armed(watch: dict) -> bool:
    return bool(_safe_dict(watch.get("order"))) or str(watch.get("trigger_action") or "") == "EXECUTE_ORDER"


def _build_plans_overview_tile(
    state: dict,
    armed_plans: list[dict],
    trade_plans: list[dict],
    watches: list[dict],
    daemon_status: dict,
    *,
    palette: Palette,
) -> RenderableType:
    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    progress = f"{done}/{total}" if done is not None and total is not None else "—"
    header = Text.assemble(
        ("Progression ", palette["dim"]),
        (progress, palette["status_accent"]),
        ("  Armés ", palette["dim"]),
        (str(len(armed_plans)), palette["kpi_default"]),
        ("  Sorties ", palette["dim"]),
        (str(len(trade_plans)), palette["kpi_default"]),
        ("  Veilles ", palette["dim"]),
        (str(len(watches)), palette["kpi_default"]),
    )
    armed_table = Table(title="Plans armés", show_header=True, expand=True, box=None)
    armed_table.add_column("Sym", style="bold", no_wrap=True)
    armed_table.add_column("Ordre", no_wrap=True)
    armed_table.add_column("Stop", no_wrap=True)
    armed_table.add_column("Déclencheur", overflow="fold")
    armed_table.add_column("Exp.", no_wrap=True)
    for watch in armed_plans[:3]:
        armed_table.add_row(
            str(watch.get("symbol") or "—"),
            _armed_order_label(watch),
            _armed_stop_label(watch),
            _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
            _relative_expiry(watch.get("expires_at")),
        )
    if not armed_plans:
        armed_table.add_row("—", "—", "—", "aucun plan armé", "—")

    exits_table = Table(title="Sorties ouvertes", show_header=True, expand=True, box=None)
    exits_table.add_column("Sym", style="bold", no_wrap=True)
    exits_table.add_column("Sens/Qté", no_wrap=True)
    exits_table.add_column("Risque", no_wrap=True)
    exits_table.add_column("Prochain", overflow="fold")
    exits_table.add_column("État", no_wrap=True)
    exit_rows = []
    for plan in trade_plans:
        symbol = str(plan.get("symbol") or "—")
        price = _price_for_symbol(state, symbol) or _safe_float(plan.get("entry_price"), default=None)
        stale = _symbol_is_stale(state, symbol)
        exit_rows.append(
            (
                0 if stale else 1,
                symbol,
                _plan_qty_label(plan),
                _exit_plan_risk_label(plan, price, stale),
                _next_tp_label(plan, price),
                _plan_state_label(plan),
                stale,
            )
        )
    for _, symbol, side_qty, risk, next_step, status, stale in sorted(exit_rows)[:4]:
        exits_table.add_row(
            symbol,
            side_qty,
            Text(risk, style=palette["kpi_vol_warn"] if stale else palette["dim"]),
            next_step,
            status,
        )
    if not exit_rows:
        exits_table.add_row("—", "—", "—", "aucune sortie ouverte", "—")

    simple_watches = [watch for watch in watches if not _watch_is_armed(watch)]
    watches_table = Table(title="Veilles", show_header=True, expand=True, box=None)
    watches_table.add_column("Sym", style="bold", no_wrap=True)
    watches_table.add_column("Mode", no_wrap=True)
    watches_table.add_column("Déclencheur", overflow="fold")
    watches_table.add_column("Exp.", no_wrap=True)
    for watch in simple_watches[:3]:
        watches_table.add_row(
            str(watch.get("symbol") or "—"),
            "WAKE",
            _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
            _relative_expiry(watch.get("expires_at")),
        )
    if not simple_watches:
        watches_table.add_row("—", "—", "aucune veille active", "—")

    return Group(header, armed_table, exits_table, watches_table)


def _build_observability_overview_tile(
    state: dict,
    stale_streaks: dict,
    learnings: list[dict],
    *,
    palette: Palette,
) -> RenderableType:
    table = Table(title="Data health", show_header=True, expand=True, box=None)
    table.add_column("Signal", no_wrap=True)
    table.add_column("Valeur", overflow="fold")
    stale_rows = [
        (symbol, value)
        for symbol, value in stale_streaks.items()
        if isinstance(value, int | float) and value > 0
    ]
    if stale_rows:
        worst = sorted(stale_rows, key=lambda item: int(item[1]), reverse=True)[:5]
        for symbol, streak in worst:
            table.add_row(
                str(symbol),
                Text(f"stale x{int(streak)}", style=palette["kpi_vol_warn"]),
            )
    else:
        table.add_row("data", Text("OK", style=palette["status_nominal"]))
    table.add_row("source", str(state.get("source") or "—"))
    table.add_row("cycle", _format_datetime(state.get("ts")))
    table.add_row("pending learn", str(state.get("learnings_pending_count") or 0))

    learn_table = Table(title="Learnings", show_header=True, expand=True, box=None)
    learn_table.add_column("Sym", no_wrap=True)
    learn_table.add_column("Note", overflow="fold")
    for item in learnings[-3:]:
        learn_table.add_row(
            str(item.get("symbol") or "—"),
            _clip_text(item.get("note"), limit=54),
        )
    if not learnings:
        learn_table.add_row("—", "aucun learning récent")
    return Group(table, learn_table)


def _build_logs_overview_tile(
    state: dict,
    recent_decisions: list[dict],
    daemon_status: dict,
    stale_streaks: dict,
    *,
    kill_active: bool,
    palette: Palette = PALETTE_LIGHT,
) -> RenderableType:
    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    progress = f"{done}/{total}" if done is not None and total is not None else "—"
    calls_used = daemon_status.get("model_calls_used")
    calls_max = daemon_status.get("max_model_calls_per_cycle")
    calls = f"{calls_used}/{calls_max}" if calls_used is not None and calls_max is not None else "—"
    stale_values = [
        int(value)
        for value in stale_streaks.values()
        if isinstance(value, int | float) and value > 0
    ]
    data_label = (
        f"stale {len(stale_values)} max {max(stale_values)}"
        if stale_values
        else "OK"
    )

    runtime = Table(title="Runtime", show_header=True, expand=True, box=None)
    runtime.add_column("Signal", no_wrap=True)
    runtime.add_column("Valeur", overflow="fold")
    runtime.add_row("phase", str(daemon_status.get("phase") or "—"))
    runtime.add_row("progression", progress)
    runtime.add_row("LLM", calls)
    runtime.add_row("source", str(state.get("source") or "—"))
    runtime.add_row("cycle", _format_datetime(state.get("ts")))
    runtime.add_row(
        "data",
        Text(
            data_label,
            style=palette["kpi_vol_warn"] if stale_values else palette["status_nominal"],
        ),
    )
    runtime.add_row("learn pending", str(state.get("learnings_pending_count") or 0))
    runtime.add_row(
        "kill",
        Text(
            "KILL actif" if kill_active else "nominal",
            style=palette["pnl_negative"] if kill_active else palette["status_nominal"],
        ),
    )

    signals = Table(title="Derniers signaux", show_header=True, expand=True, box=None)
    signals.add_column("UTC", no_wrap=True, style=palette["dim"])
    signals.add_column("Sym", style="bold", no_wrap=True)
    signals.add_column("État", no_wrap=True)
    signals.add_column("Suite", overflow="fold")
    for decision in _select_decision_rows([], recent_decisions, limit=4):
        status = _decision_status(decision)
        status_style = {
            "exec": palette["status_nominal"],
            "risk": palette["pnl_negative"],
            "stale": palette["kpi_vol_warn"],
            "plan": palette["status_accent"],
            "veille": palette["status_accent"],
            "quiet": palette["dim"],
        }.get(status, palette["kpi_default"])
        signals.add_row(
            _decision_time_label(decision),
            str(decision.get("symbol") or "—"),
            Text(status, style=status_style),
            _decision_effect_label(decision),
        )
    if not recent_decisions:
        signals.add_row("—", "—", "—", "aucun signal récent")

    return Group(runtime, signals)


def _overview_state_parts(state: dict) -> tuple[
    dict,
    dict,
    dict,
    list[dict],
    list[dict],
    list[dict],
    list[dict],
    list[dict],
    dict,
    list[float],
    list[dict],
    float,
]:
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    daemon_status = (
        state.get("daemon_status")
        if isinstance(state.get("daemon_status"), dict)
        else {}
    )
    decisions = _safe_list_of_dicts(state.get("decisions"))
    recent_decisions = _safe_list_of_dicts(state.get("recent_decisions"))
    armed_plans = _safe_list_of_dicts(state.get("armed_plans"))
    trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
    watches = _safe_list_of_dicts(state.get("indicator_watches"))
    stale_streaks = (
        state.get("stale_streaks") if isinstance(state.get("stale_streaks"), dict) else {}
    )
    equity_curve = [
        value
        for value in (
            _safe_float(item, default=None) for item in (state.get("equity_curve") or [])
        )
        if value is not None
    ]
    learnings = _safe_list_of_dicts(state.get("learnings"))
    cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
    starting_cash = _safe_float(state.get("starting_cash"), default=cash) or cash
    return (
        portfolio,
        kpis,
        daemon_status,
        decisions,
        recent_decisions,
        armed_plans,
        trade_plans,
        watches,
        stale_streaks,
        equity_curve,
        learnings,
        starting_cash,
    )


def _build_overview_panel(
    state: dict, *, kill_active: bool, palette: Palette = PALETTE_LIGHT
) -> RenderableType:
    (
        _portfolio,
        _kpis,
        daemon_status,
        decisions,
        recent_decisions,
        armed_plans,
        trade_plans,
        watches,
        stale_streaks,
        _equity_curve,
        learnings,
        _starting_cash,
    ) = _overview_state_parts(state)

    top = Table.grid(expand=True)
    top.add_column(ratio=2)
    top.add_column(ratio=1)
    top.add_row(
        _overview_card(
            "Portefeuille",
            _build_portfolio_overview_tile(
                state,
                palette=palette,
            ),
            border_style=palette["border_default"],
        ),
        _overview_card(
            "Décisions",
            _build_decisions_overview_tile(
                decisions,
                recent_decisions,
                daemon_status,
                palette=palette,
            ),
            border_style=palette["border_attribution"],
        ),
    )
    bottom = Table.grid(expand=True)
    bottom.add_column(ratio=1)
    bottom.add_column(ratio=1)
    bottom.add_column(ratio=1)
    bottom.add_row(
        _overview_card(
            "Plans",
            _build_plans_overview_tile(
                state,
                armed_plans,
                trade_plans,
                watches,
                daemon_status,
                palette=palette,
            ),
            border_style=palette["border_plans"],
        ),
        _overview_card(
            "Observabilité",
            _build_observability_overview_tile(
                state,
                stale_streaks,
                learnings,
                palette=palette,
            ),
            border_style=palette["border_learnings"],
        ),
        _overview_card(
            "Flux live",
            _build_logs_overview_tile(
                state,
                recent_decisions,
                daemon_status,
                stale_streaks,
                kill_active=kill_active,
                palette=palette,
            ),
            border_style=palette["border_default"],
        ),
    )
    return Group(
        Panel(
            _build_attention_line(state, kill_active=kill_active, palette=palette),
            title="[bold]Synthèse[/bold]",
            border_style=palette["border_default"],
            expand=True,
        ),
        top,
        bottom,
    )


class OverviewPane(Static):
    """Home analytique : tuiles Textual responsives et non scrollables."""

    DEFAULT_CSS = """
    OverviewPane {
        height: 100%;
        width: 100%;
        overflow-y: hidden;
        padding: 0 1;
        layout: vertical;
    }
    #overview-top-row {
        height: 3fr;
        width: 100%;
        layout: horizontal;
    }
    #overview-bottom-row {
        height: 2fr;
        width: 100%;
        layout: horizontal;
    }
    #overview-portfolio-tile {
        width: 2fr;
        height: 100%;
    }
    #overview-decisions-tile {
        width: 1fr;
        height: 100%;
    }
    #overview-plans-tile,
    #overview-observability-tile,
    #overview-logs-tile {
        width: 1fr;
        height: 100%;
    }
    .overview-tile {
        margin: 0 1 1 0;
        overflow: hidden;
    }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        with Horizontal(id="overview-top-row"):
            yield Static(id="overview-portfolio-tile", classes="overview-tile")
            yield Static(id="overview-decisions-tile", classes="overview-tile")
        with Horizontal(id="overview-bottom-row"):
            yield Static(id="overview-plans-tile", classes="overview-tile")
            yield Static(id="overview-observability-tile", classes="overview-tile")
            yield Static(id="overview-logs-tile", classes="overview-tile")

    def update_state(self, state: dict, kill_active: bool) -> None:
        (
            _portfolio,
            _kpis,
            daemon_status,
            decisions,
            recent_decisions,
            armed_plans,
            trade_plans,
            watches,
            stale_streaks,
            _equity_curve,
            learnings,
            _starting_cash,
        ) = _overview_state_parts(state)
        palette = self._current_palette

        self.query_one("#overview-portfolio-tile", Static).update(
            _overview_card(
                "Portefeuille",
                _build_portfolio_overview_tile(
                    state,
                    palette=palette,
                ),
                border_style=palette["border_default"],
            )
        )
        self.query_one("#overview-decisions-tile", Static).update(
            _overview_card(
                "Décisions",
                _build_decisions_overview_tile(
                    decisions,
                    recent_decisions,
                    daemon_status,
                    palette=palette,
                ),
                border_style=palette["border_attribution"],
            )
        )
        self.query_one("#overview-plans-tile", Static).update(
            _overview_card(
                "Plans",
                _build_plans_overview_tile(
                    state,
                    armed_plans,
                    trade_plans,
                    watches,
                    daemon_status,
                    palette=palette,
                ),
                border_style=palette["border_plans"],
            )
        )
        self.query_one("#overview-observability-tile", Static).update(
            _overview_card(
                "Observabilité",
                _build_observability_overview_tile(
                    state,
                    stale_streaks,
                    learnings,
                    palette=palette,
                ),
                border_style=palette["border_learnings"],
            )
        )
        self.query_one("#overview-logs-tile", Static).update(
            _overview_card(
                "Flux live",
                _build_logs_overview_tile(
                    state,
                    recent_decisions,
                    daemon_status,
                    stale_streaks,
                    kill_active=kill_active,
                    palette=palette,
                ),
                border_style=palette["border_default"],
            )
        )
