"""Exit-plan and armed-plan Rich builders."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from trader.interfaces.ui.palette import PALETTE_DARK, Palette
from trader.interfaces.ui.panels.common import _expire_relative, _market_badge
from trader.market import fx
from trader.planning.indicator_watch import is_armed_plan as _is_armed_plan
from trader.reporting.read_models.runtime_state import (
    UTC,
    _safe_float,
    _safe_list_of_dicts,
)


def _build_exit_plans_panel(
    plans: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Plans de sortie ouverts (colonne gauche, sous positions).

    Source : state/trade_plans.json — liste brute de dicts.
    """
    if not plans:
        return Panel(
            Text("aucun plan ouvert", style=palette["dim"]),
            title="[bold]Plans sortie[/bold]",
            border_style=palette["border_plans"],
            expand=True,
        )

    lines: list[RenderableType] = []
    for plan in plans:
        symbol = str(plan.get("symbol", "?"))
        ccy = fx.currency_for(symbol)
        side = str(plan.get("side", "?"))
        entry = _safe_float(plan.get("entry_price"), default=None)
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        remaining = _safe_float(plan.get("remaining_quantity"), default=0.0) or 0.0
        max_hold = _safe_float(plan.get("max_hold_minutes"), default=None)
        take_profits = _safe_list_of_dicts(plan.get("take_profits") or [])

        side_style = (
            palette["action_buy"] if side == "LONG" else palette["action_sell"]
        )

        # Ligne principale : symbole side  entrée → stop (dist%)
        if entry is not None and stop is not None and entry > 0:
            dist_pct = abs(entry - stop) / entry * 100.0
            stop_str = f"{stop:,.2f} ({dist_pct:.1f}%)"
        elif stop is not None:
            stop_str = f"{stop:,.2f}"
        else:
            stop_str = "—"

        entry_str = f"{entry:,.2f}" if entry is not None else "—"

        header = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (side, side_style),
            (f"  prix natif {ccy} entrée:", palette["dim"]),
            (f" {entry_str}", "bold"),
            ("  stop:", palette["dim"]),
            (f" {stop_str}", palette["pnl_negative"] if stop else palette["dim"]),
            ("  qté:", palette["dim"]),
            (f" {remaining:,.4f}", "bold"),
        )
        lines.append(header)

        # Take-profits sur une ligne compacte
        if take_profits:
            tp_parts: list[tuple[str, str]] = []
            for tp in take_profits:
                tp_price = _safe_float(tp.get("price"), default=None)
                tp_name = str(tp.get("name") or "tp?")
                if tp_price is not None:
                    tp_parts.append((f"{tp_name}@{tp_price:,.2f}", palette["pnl_positive"]))
                    tp_parts.append(("  ", ""))
            if tp_parts:
                tp_line = Text.assemble((f"  TPs natifs {ccy}: ", palette["dim"]), *tp_parts)
                lines.append(tp_line)

        # max_hold
        if max_hold is not None:
            lines.append(
                Text.assemble(
                    ("  max hold:", palette["dim"]),
                    (f" {int(max_hold)}min", "bold"),
                )
            )

        lines.append(Text(""))  # séparateur

    # Retire le dernier séparateur vide
    if lines and isinstance(lines[-1], Text) and lines[-1].plain == "":
        lines.pop()

    return Panel(
        Group(*lines),
        title="[bold]Plans sortie[/bold]",
        border_style=palette["border_plans"],
        expand=True,
    )


def _build_armed_plans_panel(
    watches: list[dict],
    *,
    open_venues: "set[str] | None" = None,
    palette: Palette = PALETTE_DARK,
) -> Panel:
    """Plans armés (colonne gauche) : scénarios d'entrée que le daemon exécutera
    au déclenchement sans appel LLM. Distincts des positions (rien n'est ouvert)
    et des veilles simples (qui ne portent pas d'ordre).
    """
    armed = [w for w in watches if _is_armed_plan(w)]
    if not armed:
        return Panel(
            Text("aucun plan armé", style=palette["dim"]),
            title="[bold]Plans armés[/bold]",
            border_style=palette["border_watches"],
            expand=True,
        )

    now_utc = datetime.now(UTC)
    lines: list[Text] = []
    for watch in armed:
        symbol = str(watch.get("symbol", "?"))
        order = watch.get("order") or {}
        intent = str(order.get("intent") or "?")
        sens = "▲LONG" if intent == "OPEN_LONG" else "▼SHORT"
        sens_style = palette["pnl_positive"] if intent == "OPEN_LONG" else palette["pnl_negative"]
        qty = order.get("qty")
        stop = None
        exit_plan = order.get("exit_plan")
        if isinstance(exit_plan, dict):
            hard_stop = exit_plan.get("hard_stop")
            stop = hard_stop.get("price") if isinstance(hard_stop, dict) else hard_stop
        conditions = _safe_list_of_dicts(watch.get("conditions") or [])
        cond_parts = [
            f"{c.get('indicator', '?')}{c.get('op', '?')}{c.get('value', '?')}"
            f"@{c.get('timeframe') or c.get('interval') or '?'}"
            for c in conditions[:2]
        ]
        cond_str = f" [{watch.get('logic', 'all')}] ".join(cond_parts) if cond_parts else "?"
        if len(conditions) > 2:
            cond_str += f" +{len(conditions) - 2}"
        confidence = order.get("confidence")
        conf_str = f"  c.{confidence:.2f}".rstrip("0").rstrip(".") if isinstance(confidence, (int, float)) else ""
        # Conteneur neutre : le style du badge reste local (span) et ne
        # contamine pas le style de base de toute la ligne (cf. review Codex).
        line = Text()
        line.append_text(_market_badge(symbol, open_venues, palette))
        line.append(" ")
        line.append_text(Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (sens, f"bold {sens_style}"),
            (f" {qty:g}" if isinstance(qty, (int, float)) else " ?", ""),
            (f"  stop {stop}" if stop is not None else "  stop ?", palette["kpi_default"]),
            ("  si ", palette["dim"]),
            (cond_str, palette["dim"]),
            ("  ", ""),
            (_expire_relative(str(watch.get("expires_at") or ""), now=now_utc), palette["dim"]),
            (conf_str, palette["dim"]),
        ))
        lines.append(line)
    return Panel(
        Group(*lines),
        title=f"[bold]Plans armés[/bold] ({len(armed)})",
        border_style=palette["border_watches"],
        expand=True,
    )


def _build_exit_plans_enriched(
    plans: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Plans de sortie enrichis : entrée, stop, TPs et bénéfice estimé par scénario.

    Enrichit l'affichage de _build_exit_plans_panel sans le modifier.
    Champs lus : entry_price, hard_stop_price, take_profits[].price,
                 remaining_quantity, max_hold_minutes, llm_confidence.
    Bénéfice estimé : (prix_scénario - entrée) × quantité_restante × sens.
    """
    if not plans:
        return Panel(
            Text("aucun plan ouvert", style=palette["dim"]),
            title="[bold]Plans sortie enrichis[/bold]",
            border_style=palette["border_plans"],
            expand=True,
        )

    lines: list[RenderableType] = []
    for plan in plans:
        symbol = str(plan.get("symbol", "?"))
        ccy = fx.currency_for(symbol)
        side = str(plan.get("side", "?"))
        direction = -1.0 if side == "SHORT" else 1.0
        entry = _safe_float(plan.get("entry_price"), default=None)
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        remaining = _safe_float(plan.get("remaining_quantity"), default=0.0) or 0.0
        max_hold = _safe_float(plan.get("max_hold_minutes"), default=None)
        confidence = _safe_float(plan.get("llm_confidence"), default=None)
        take_profits = _safe_list_of_dicts(plan.get("take_profits") or [])

        side_style = (
            palette["action_buy"] if side == "LONG" else palette["action_sell"]
        )

        # Ligne principale
        entry_str = f"{entry:,.2f}" if entry is not None else "—"
        if entry is not None and stop is not None and entry > 0:
            dist_pct = abs(entry - stop) / entry * 100.0
            stop_str = f"{stop:,.2f} ({dist_pct:.1f}%)"
        elif stop is not None:
            stop_str = f"{stop:,.2f}"
        else:
            stop_str = "—"

        conf_str = f"{confidence:.2f}" if confidence is not None else "—"

        header = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (side, side_style),
            (f"  prix natif {ccy} entrée:", palette["dim"]),
            (f" {entry_str}", "bold"),
            ("  stop:", palette["dim"]),
            (f" {stop_str}", palette["pnl_negative"] if stop else palette["dim"]),
            ("  qté:", palette["dim"]),
            (f" {remaining:,.4f}", "bold"),
            ("  conf:", palette["dim"]),
            (f" {conf_str}", palette["kpi_default"]),
        )
        lines.append(header)

        # Bénéfice estimé scénario stop
        if entry is not None and stop is not None:
            stop_gain = (stop - entry) * remaining * direction
            stop_style = palette["pnl_positive"] if stop_gain >= 0 else palette["pnl_negative"]
            stop_gain_str = f"{stop_gain:+,.2f}"
            lines.append(
                Text.assemble(
                    ("  Stop natif: ", palette["dim"]),
                    (f"{stop:,.2f}", "bold"),
                    ("  → gain natif ", palette["dim"]),
                    (stop_gain_str, stop_style),
                )
            )

        # Take-profits avec bénéfice estimé
        if take_profits:
            for tp in take_profits:
                tp_price = _safe_float(tp.get("price"), default=None)
                tp_name = str(tp.get("name") or "tp?")
                if tp_price is not None:
                    tp_gain = (
                        (tp_price - entry) * remaining * direction
                        if entry is not None
                        else None
                    )
                    tp_gain_str = f"{tp_gain:+,.2f}" if tp_gain is not None else "—"
                    tp_gain_style = (
                        palette["pnl_positive"]
                        if (tp_gain is not None and tp_gain >= 0)
                        else palette["pnl_negative"]
                    )
                    lines.append(
                        Text.assemble(
                            (f"  {tp_name} natif: ", palette["dim"]),
                            (f"{tp_price:,.2f}", palette["pnl_positive"]),
                            ("  → gain natif ", palette["dim"]),
                            (tp_gain_str, tp_gain_style),
                        )
                    )

        # Raison de sortie : champ absent dans trade_plans.json → affiche —
        lines.append(
            Text.assemble(
                ("  Raison sortie:", palette["dim"]),
                (" —", palette["dim"]),
            )
        )

        # max_hold
        if max_hold is not None:
            lines.append(
                Text.assemble(
                    ("  max hold:", palette["dim"]),
                    (f" {int(max_hold)}min", "bold"),
                )
            )

        lines.append(Text(""))  # séparateur

    # Retire le dernier séparateur vide
    if lines and isinstance(lines[-1], Text) and lines[-1].plain == "":
        lines.pop()

    return Panel(
        Group(*lines),
        title="[bold]Plans sortie enrichis[/bold]",
        border_style=palette["border_plans"],
        expand=True,
    )


__all__ = [
    "_build_armed_plans_panel",
    "_build_exit_plans_enriched",
    "_build_exit_plans_panel",
]
