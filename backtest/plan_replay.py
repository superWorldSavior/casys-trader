"""Replayer mécanique de plans armés — D8 étage 1 (évaluation a posteriori).

Un plan armé (condition + sens + taille + stop + TTL) est un artefact 100 %
déterministe : on le rejoue sur des barres historiques sans appel LLM, avec
les MÊMES briques que le live (évaluation de watch, cohérence prix/stop,
moteur de sortie). Outil de MESURE de la qualité des scénarios de l'agent —
jamais de validation a priori : l'agent reste libre d'armer (décision Erwan,
registre D8).

v1 : toutes les conditions sont évaluées sur les barres fournies, quel que
soit leur `interval` déclaré (l'agrégation multi-timeframe est un point
ouvert du registre).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trader.exit_engine import evaluate_plan
from trader.indicator_watch import (
    armed_order_price_coherent,
    evaluate_indicator_watches,
    is_armed_plan,
)
from trader.trade_plan import InvalidExitPlanError, create_trade_plan_from_order

__all__ = ["PlanReplayResult", "replay_armed_plan"]


@dataclass(frozen=True)
class PlanReplayResult:
    plan_id: str
    symbol: str
    status: str  # "expired" | "cancelled:stop_incoherent" | "executed" | "invalid"
    triggered_at: str | None = None
    entry_price: float | None = None
    exit_reason: str | None = None  # "hard_stop" | "take_profit:..." | "horizon" | ...
    exit_at: str | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None  # signé selon le sens (SHORT gagnant => positif)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_trigger_index(watch: dict, bars: list, *, min_window: int) -> int | None:
    """Premier index de barre où les conditions du plan déclenchent (as-of)."""
    symbol = str(watch.get("symbol"))
    intervals = {
        str(c.get("interval") or c.get("timeframe") or "15m")
        for c in watch.get("conditions") or []
    }
    created = _parse_ts(str(watch.get("created_at") or "")) or _parse_ts(bars[0].ts)
    expires = _parse_ts(str(watch.get("expires_at") or ""))
    for i in range(min_window, len(bars)):
        ts = _parse_ts(bars[i].ts)
        if ts is None or (created and ts <= created):
            continue
        if expires and ts > expires:
            return None
        asof = bars[: i + 1]
        bars_by_key = {(symbol, interval): asof for interval in intervals}
        if evaluate_indicator_watches([watch], bars_by_key, now=ts):
            return i
    return None


def replay_armed_plan(watch: dict, bars: list) -> PlanReplayResult:
    """Rejoue un plan armé sur des barres : déclenché ? annulé ? puis vie du trade.

    Mêmes règles qu'en live : cohérence prix/stop au déclenchement
    (`armed_order_price_coherent`), sorties via `exit_engine.evaluate_plan`
    (stop > max_hold > take_profits, extrêmes intra-barre). Données épuisées
    avant la sortie => clôture « horizon » au dernier close.
    """
    plan_id = str(watch.get("id") or "?")
    symbol = str(watch.get("symbol") or "?")
    if not is_armed_plan(watch) or not bars:
        return PlanReplayResult(plan_id=plan_id, symbol=symbol, status="invalid")
    order = watch["order"]

    max_window = max(
        (int(c.get("window") or 0) for c in watch.get("conditions") or []), default=0
    )
    trigger_index = _find_trigger_index(watch, bars, min_window=max(1, max_window - 1))
    if trigger_index is None:
        return PlanReplayResult(plan_id=plan_id, symbol=symbol, status="expired")

    trigger_bar = bars[trigger_index]
    entry_price = float(trigger_bar.close)
    if not armed_order_price_coherent(order, price=entry_price):
        return PlanReplayResult(
            plan_id=plan_id,
            symbol=symbol,
            status="cancelled:stop_incoherent",
            triggered_at=str(trigger_bar.ts),
        )

    quantity = float(order["qty"])
    try:
        plan = create_trade_plan_from_order(
            symbol=symbol,
            order_side=str(order["action"]),
            quantity=quantity,
            entry_price=entry_price,
            opened_at=str(trigger_bar.ts),
            raw_exit_plan=order.get("exit_plan"),
            llm_confidence=order.get("confidence"),
        )
    except InvalidExitPlanError:
        return PlanReplayResult(
            plan_id=plan_id,
            symbol=symbol,
            status="invalid",
            triggered_at=str(trigger_bar.ts),
            entry_price=entry_price,
        )
    direction = 1.0 if plan.side == "LONG" else -1.0

    realized = 0.0  # P&L cumulé des sorties partielles, en unités de prix × qty
    for bar in bars[trigger_index + 1 :]:
        ts = _parse_ts(bar.ts)
        if ts is None:
            continue
        evaluation = evaluate_plan(
            plan,
            price=float(bar.close),
            bar_high=float(bar.high),
            bar_low=float(bar.low),
            now=ts,
        )
        plan = evaluation.updated_plan
        signal = evaluation.signal
        if signal is not None:
            fill = float(signal.fill_price if signal.fill_price is not None else bar.close)
            realized += direction * (fill - entry_price) * float(signal.quantity)
            if evaluation.close_plan or plan.remaining_quantity <= 0:
                return PlanReplayResult(
                    plan_id=plan_id,
                    symbol=symbol,
                    status="executed",
                    triggered_at=str(trigger_bar.ts),
                    entry_price=entry_price,
                    exit_reason=str(signal.reason),
                    exit_at=str(bar.ts),
                    exit_price=fill,
                    pnl_pct=round(realized / (entry_price * quantity) * 100.0, 4),
                )

    # données épuisées : clôture du reliquat au dernier close
    last = bars[-1]
    last_close = float(last.close)
    realized += direction * (last_close - entry_price) * float(plan.remaining_quantity)
    return PlanReplayResult(
        plan_id=plan_id,
        symbol=symbol,
        status="executed",
        triggered_at=str(trigger_bar.ts),
        entry_price=entry_price,
        exit_reason="horizon",
        exit_at=str(last.ts),
        exit_price=last_close,
        pnl_pct=round(realized / (entry_price * quantity) * 100.0, 4),
    )
