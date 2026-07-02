"""daemon — la boucle runtime (boucle 2).

Un cycle = réveil -> contexte (marché + portefeuille) -> Codex décide ->
risk gate -> exécution paper -> log -> l'agent planifie son prochain réveil.

L'infra orchestre ; la STRATÉGIE n'est pas ici. Le daemon ne fait que :
  câbler les outils, appeler Codex, faire respecter le fusible, exécuter, logger.

Safe defaults : `--dry-run` par défaut (n'exécute pas, log seulement). Kill switch
via fichier. Toute erreur Codex -> HOLD.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

from .agent_context import build_market_cockpit, resolve_indicator_requests
from . import (
    agent_tools,
    attribution,
    code_version,
    codex_client,
    consolidator,
    decision_ledger,
    embeddings as embeddings_mod,
    family_regime,
    fx,
    fx_rates,
    learnings_store as recall_store_mod,
    ledger_rotation,
    llm,
    macro_calendar,
    macro_series,
    meta_performance,
    relevance_gate,
    stats,
)
from .exit_engine import evaluate_plan
from .features import DEFAULT_INDICATORS, build_indicator_snapshot
from .ib_attach import IBAttachBackoff
from .indicator_watch import (
    armed_order_price_coherent,
    build_indicator_watch,
    evaluate_indicator_watches,
    summarize_watch,
    watch_market_requests,
)
from .gross_priority import PriorityItem, gross_execution_order
from .risk import RiskGate, RiskLimits
from .tools import market, memory as memory_mod, news_feed, portfolio, scheduler
from .tools.execution import (
    CommissionModel,
    Order,
    SimBroker,
    commission_model_from_name,
    round_trip_cost,
)
from .tools.data_source import (
    CompositeDataSource,
    YFinanceDataSource,
    parse_data_sources_config,
)
from .tools.ib_source import IBDataSource, connect_ib
from .trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    TradePlanStore,
    create_trade_plan,
    create_trade_plan_from_order,
    normalize_exit_plan,
    resolve_exit_plan,
    validate_exit_plan,
)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

log = logging.getLogger("casys-trader")

# Drapeau d'échec définitif du store recall (db corrompu/verrouillé à l'ouverture).
# Posé à True dès le premier échec ; ne pas réessayer à chaque cycle pour éviter
# de logguer la même erreur indéfiniment. Réinitialisable dans les tests.
_RECALL_STORE_FAILED: bool = False

_VALID_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "HOLD"}
_ACTION_INTENTS = {
    "BUY": {"OPEN_LONG", "REDUCE", "CLOSE", "REVERSE"},
    "SELL": {"OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE"},
}
_INFRA_HOLD_REASONS = {
    "no_decision_in_batch",
    "model_call_budget_exhausted",
    "model_call_budget_exhausted_after_context",
}
_NON_REVIEW_RATIONALES = {
    *_INFRA_HOLD_REASONS,
    "batch_bad_output",
    "missing_in_batch",
    "context_loop_blocked",
}


def _llm_exit_reason_for_intent(intent: str) -> str | None:
    """Libellé déterministe pour les sorties pilotées par le LLM."""
    return "llm_exit" if intent in {"CLOSE", "REDUCE", "REVERSE"} else None


_GROSS_REJECT_REASON = "risk:gross_exposure_exceeded"


def summarize_gross_rejections(decisions: list[dict]) -> dict | None:
    """Résume les ouvertures recalées faute de marge gross sur un cycle.

    Feedback léger réinjecté au cycle suivant (les sessions LLM sont stateless) :
    l'agent voit qu'il a collectivement sur-proposé contre le plafond gross
    partagé et peut être plus sélectif. Retourne None s'il n'y a rien à signaler.
    """
    symbols = sorted(
        str(d.get("symbol"))
        for d in decisions
        if d.get("reason") == _GROSS_REJECT_REASON
        and d.get("intent") in {"OPEN_LONG", "OPEN_SHORT", "REVERSE"}
        and d.get("symbol")
    )
    if not symbols:
        return None
    return {"rejected_opens": len(symbols), "symbols": symbols}


def _counts_as_llm_review(decision: codex_client.Decision) -> bool:
    """True seulement si le LLM a vraiment rendu une décision exploitable."""
    if not (decision.llm_provider or decision.llm_model):
        return False
    if decision.llm_error:
        return False
    rationale = str(decision.rationale or "")
    if rationale in _NON_REVIEW_RATIONALES:
        return False
    if rationale.startswith(("batch_bad_output:", "codex_bad_output:", "llm_failed:")):
        return False
    return True


# Barres fines (15m) pour coller à la cadence scalping (réveils 5-30 min) et avoir
# un prix qui bouge intra-heure. La fraîcheur sur ces barres sert de garde
# « marché live » (UTC, sans logique de fuseau). ~40 min ≈ tolérance de 2 barres + délai.
DEFAULT_RUNTIME_INTERVAL = "15m"
DEFAULT_RUNTIME_LOOKBACK = "5d"
DEFAULT_MAX_MARKET_DATA_AGE_MINUTES = 40.0
COCKPIT_DAILY_LOOKBACK = "1y"
COCKPIT_DAILY_INTERVAL = "1d"
DEFAULT_IB_HOST = "127.0.0.1"
DEFAULT_IB_PORT = 4002
DEFAULT_IB_CLIENT_ID = 17
DEFAULT_LEARNING_CONSOLIDATION_THRESHOLD = consolidator.DEFAULT_CONSOLIDATION_THRESHOLD
DEFAULT_DECISION_BATCH_SIZE = 5
DEFAULT_DECISION_BATCH_PARALLELISM = 3

# D7 étage A — dernier passage LLM par (state_dir, symbole), pour la revue
# périodique garantie du gate de pertinence. Volatile : reset au restart.
_LAST_LLM_AT: dict[tuple[str, str], object] = {}
# Feedback gross d'un cycle au suivant (process daemon long-vivant, comme
# _LAST_LLM_AT). Keyé par STATE_DIR. Best-effort : vidé au redémarrage.
_LAST_GROSS_REJECTIONS: dict[str, dict | None] = {}
# Intervalle fin pour les checks de sortie (stop/TP/trailing).
# Fetché uniquement pour les symboles ayant un plan ouvert.
EXIT_CHECK_INTERVAL = "5m"
EXIT_CHECK_LOOKBACK = "1d"
# Nombre de barres 5m agrégées pour les checks de sortie (fenêtre = 3×5m = 15m).
EXIT_CHECK_WINDOW_BARS = 3


def _write_json_state(filename: str, payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / filename).write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def _write_status(phase: str, **payload: object) -> None:
    _write_json_state(
        "daemon_status.json",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "pid": os.getpid(),
            **payload,
        },
    )


def _write_current_report(report: dict) -> None:
    _write_json_state("current_report.json", report)


def _append_event(event: str, **payload: object) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
    with (STATE_DIR / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _append_model_performance(**payload: object) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "model_performance.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _append_cycle_history(report: dict) -> None:
    pf = report.get("portfolio")
    n_executed_decisions = sum(1 for d in report.get("decisions", []) if d.get("executed"))
    n_executed_planned = sum(1 for item in report.get("planned_exits", []) if item.get("executed"))
    history_row = {
        "ts": report["ts"],
        "equity": pf["equity"] if pf is not None else None,
        "cash": pf["cash"] if pf is not None else None,
        "n_decisions": len(report.get("decisions", [])),
        "n_executed": n_executed_decisions + n_executed_planned,
        "n_planned_exits": len(report.get("planned_exits", [])),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(history_row, ensure_ascii=False) + "\n")


def _build_portfolio_fee_estimator(
    commission_model: CommissionModel | None,
) -> Callable[[str, float, float, float], float | None] | None:
    if commission_model is None:
        return None

    def estimate(
        symbol: str,
        quantity: float,
        avg_price: float,
        last_price: float,
    ) -> float | None:
        if (
            not math.isfinite(quantity)
            or quantity == 0.0
            or not math.isfinite(avg_price)
            or avg_price <= 0.0
            or not math.isfinite(last_price)
            or last_price <= 0.0
        ):
            return None
        abs_quantity = abs(quantity)
        entry_side = "BUY" if quantity > 0.0 else "SELL"
        exit_side = "SELL" if quantity > 0.0 else "BUY"
        entry = commission_model.calculate(
            Order(symbol=symbol, side=entry_side, quantity=abs_quantity),
            avg_price,
        )
        exit_ = commission_model.calculate(
            Order(symbol=symbol, side=exit_side, quantity=abs_quantity),
            last_price,
        )
        if {
            entry.model,
            exit_.model,
        } & {"ibkr_unknown", "ibkr_invalid_order"}:
            return None
        return entry.amount + exit_.amount

    return estimate


def _log_cycle_progress(message: str, *args: object) -> None:
    log.info(message, *args)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("env %s invalide (%r), défaut=%s", name, raw, default)
        return default


def _disconnect_quietly(resource: object) -> None:
    disconnect = getattr(resource, "disconnect", None)
    if disconnect is None:
        return
    try:
        disconnect()
    except Exception as exc:  # noqa: BLE001 - fermeture best-effort frontière IB
        log.warning("IB disconnect failed: %s", exc)


def _is_connection_market_error(exc: market.MarketError) -> bool:
    if exc.code == "ib_connect_failed":
        return True
    if exc.code not in {"ib_fetch_failed", "ib_qualify_failed"}:
        return False
    context = exc.context.lower()
    markers = (
        "connection",
        "connexion",
        "socket",
        "disconnect",
        "closed",
        "ferm",
        "reset",
        "broken pipe",
        "peer",
    )
    return any(marker in context for marker in markers)


def _kill_switch_active() -> bool:
    return (ROOT / "KILL").exists()


def _bounded_wake_minutes(value: float, *, minimum: float, maximum: float) -> float:
    return min(max(float(value), minimum), maximum)


def _stale_backoff_wake_minutes(streak: int, *, default_wake_minutes: float) -> float:
    """Next-wake pour un symbole stale avec backoff exponentiel.

    Tous les chemins sont cappés à STALE_BACKOFF_MAX_MINUTES, y compris streak=0.
    Cela évite qu'un --default-wake-minutes élevé (ex. 240) dépasse le cap.

    streak=0 → min(default, cap)
    streak=N → min(default * 2^N, cap)

    Le streak est borné à STALE_BACKOFF_MAX_STREAK avant appel (voir Scheduler)
    pour éviter tout OverflowError sur 2**streak.

    Constantes dans trader/tools/scheduler.py :
      STALE_BACKOFF_BASE_MULTIPLIER = 2
      STALE_BACKOFF_MAX_MINUTES = 120.0
      STALE_BACKOFF_MAX_STREAK = 8
    """
    from .tools.scheduler import (
        STALE_BACKOFF_BASE_MULTIPLIER,
        STALE_BACKOFF_MAX_MINUTES,
        STALE_BACKOFF_MAX_STREAK,
    )
    # Court-circuit défensif : si streak >= MAX_STREAK, le wake est déjà cappé.
    # Évite aussi OverflowError sur 2**streak pour des valeurs arbitraires.
    if streak == 0:
        return min(default_wake_minutes, STALE_BACKOFF_MAX_MINUTES)
    if streak >= STALE_BACKOFF_MAX_STREAK:
        return STALE_BACKOFF_MAX_MINUTES
    raw = default_wake_minutes * (STALE_BACKOFF_BASE_MULTIPLIER ** streak)
    return min(raw, STALE_BACKOFF_MAX_MINUTES)


def _ensure_default_wake(
    sched: scheduler.Scheduler,
    *,
    now: datetime,
    default_wake_minutes: float,
) -> None:
    current = sched.next_wake()
    if current is None or current <= now:
        sched.set_next_wake_in(minutes=default_wake_minutes, now=now)


def _select_due_symbols(
    symbols: list[str],
    *,
    sched: scheduler.Scheduler,
    once: bool,
    bootstrap: bool,
    now: datetime | None = None,
) -> list[str]:
    if once or bootstrap:
        return symbols
    return sched.due_symbols(symbols, now=now)


def _invalid_intent_reason(decision: codex_client.Decision) -> str | None:
    if decision.action == "HOLD" or decision.quantity == 0:
        return None
    if decision.intent not in _VALID_INTENTS:
        return "invalid_intent"
    if decision.intent == "HOLD":
        return "invalid_intent"
    if decision.intent not in _ACTION_INTENTS.get(decision.action, set()):
        return "invalid_intent"
    return None


def _merge_gate_feedback(
    reason: str | None,
    context: str | None,
    note: str | None,
) -> str | None:
    """Fusionne le feedback du gate de confiance avec le learning de l'agent.

    Quand une ouverture est rejetée faute de confiance, le seuil exact raté (porté
    par `context`, ex. 'confidence=0.58 required=0.7000') ne remonte jamais à
    l'agent : au mieux il devine qu'il a été refusé, sans savoir de combien. On
    l'append aux learnings relus au prochain réveil pour qu'il calibre sa confiance
    au lieu de re-proposer un ordre voué au même rejet. Retourne la note finale à
    persister, ou None s'il n'y a rien à enregistrer.
    """
    if reason == "risk:confidence_below_required" and context:
        feedback = f"[gate confiance] rejet — {context}"
        return f"{note}\n{feedback}" if note else feedback
    return note


def _gross_exposure(
    broker: SimBroker,
    prices: dict[str, float],
    rate_of: Callable[[str], float] | None = None,
) -> float:
    return sum(
        abs(pos.quantity * prices.get(symbol, 0.0) * (rate_of(symbol) if rate_of else 1.0))
        for symbol, pos in broker.positions().items()
    )


def _finite_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def _side_capacity_usd(
    *,
    side: str,
    current_position_value: float,
    gross_exposure: float,
    limits: RiskLimits,
    equity: float,
) -> float:
    if (
        side not in {"BUY", "SELL"}
        or not math.isfinite(current_position_value)
        or not math.isfinite(gross_exposure)
        or not math.isfinite(equity)
        or equity < limits.min_equity
    ):
        return 0.0

    max_order = float(limits.max_order_value)
    position_cap = float(limits.max_position_value)
    gross_cap_for_symbol = float(limits.max_gross_exposure) - gross_exposure + abs(current_position_value)
    cap = min(position_cap, gross_cap_for_symbol)
    if max_order <= 0.0 or cap < 0.0:
        return 0.0

    sign = 1.0 if side == "BUY" else -1.0
    if sign > 0.0:
        lower = -cap - current_position_value
        upper = cap - current_position_value
    else:
        lower = current_position_value - cap
        upper = current_position_value + cap
    lower = max(0.0, lower)
    upper = min(max_order, upper)
    if upper < lower:
        return 0.0
    return max(0.0, upper)


def _risk_capacity_context(
    *,
    symbols: list[str],
    prices: dict[str, float],
    broker: SimBroker,
    gross_exposure: float,
    limits: RiskLimits,
    equity: float,
    rate_of: Callable[[str], float],
) -> dict:
    """Expose les plafonds de sizing que le LLM doit respecter avant RiskGate.

    `max_order_native` du cockpit est un montant en devise native. Ici on ajoute
    aussi une quantite maximale par symbole, en tenant compte du plafond de gross
    exposure restant, du plafond par position et du plafond par ordre.
    """
    positions = broker.positions()
    per_symbol: dict[str, dict] = {}
    for symbol in symbols:
        price = prices.get(symbol)
        rate = rate_of(symbol)
        ccy = fx.currency_for(symbol)
        if not _finite_positive(price) or not _finite_positive(rate):
            per_symbol[symbol] = {
                "price": price,
                "ccy": ccy,
                "fx_usd": rate,
                "current_position_value_usd": 0.0,
                "max_buy_qty": 0.0,
                "max_buy_notional_native": 0.0,
                "max_buy_notional_usd": 0.0,
                "max_sell_qty": 0.0,
                "max_sell_notional_native": 0.0,
                "max_sell_notional_usd": 0.0,
            }
            continue
        pos = positions.get(symbol)
        current_position_value = 0.0 if pos is None else pos.quantity * float(price) * float(rate)
        buy_usd = _side_capacity_usd(
            side="BUY",
            current_position_value=current_position_value,
            gross_exposure=gross_exposure,
            limits=limits,
            equity=equity,
        )
        sell_usd = _side_capacity_usd(
            side="SELL",
            current_position_value=current_position_value,
            gross_exposure=gross_exposure,
            limits=limits,
            equity=equity,
        )
        price_usd = float(price) * float(rate)
        per_symbol[symbol] = {
            "price": float(price),
            "ccy": ccy,
            "fx_usd": float(rate),
            "current_position_value_usd": current_position_value,
            "max_buy_qty": buy_usd / price_usd,
            "max_buy_notional_native": buy_usd / float(rate),
            "max_buy_notional_usd": buy_usd,
            "max_sell_qty": sell_usd / price_usd,
            "max_sell_notional_native": sell_usd / float(rate),
            "max_sell_notional_usd": sell_usd,
        }
    return {
        "gross_exposure_usd": gross_exposure,
        "max_gross_exposure_usd": float(limits.max_gross_exposure),
        "gross_remaining_usd": max(0.0, float(limits.max_gross_exposure) - gross_exposure),
        "max_order_value_usd": float(limits.max_order_value),
        "max_position_value_usd": float(limits.max_position_value),
        "equity_usd": equity,
        "per_symbol": per_symbol,
    }


def _attribution_min_entry_confidence(
    risk_cfg: dict, *, confidence_gate_enabled: bool
) -> float | None:
    """Seuil de censure de l'attribution par confiance d'entrée.

    Gate confiance désactivé → None : aucune censure, les trips basse-confiance
    entrent dans les buckets `by_confidence` (boucle de calibration). Gate actif →
    `min_trade_confidence` (comportement live inchangé).
    """
    if not confidence_gate_enabled:
        return None
    return float(risk_cfg.get("min_trade_confidence", 0.7))


def _hard_stop_price(raw_exit_plan: dict | None) -> float | None:
    if raw_exit_plan is None:
        return None
    try:
        normalized = normalize_exit_plan(raw_exit_plan)
    except InvalidExitPlanError:
        return None
    if not normalized:
        return None

    hard_stop = normalized.get("hard_stop")
    if isinstance(hard_stop, dict):
        if hard_stop.get("type", "price") != "price":
            return None
        raw_price = hard_stop.get("price")
    else:
        raw_price = hard_stop
    if raw_price is None:
        return None

    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def _hard_stop_wrong_side(intent: str | None, entry_price: float, stop_price: float) -> bool:
    if not math.isfinite(entry_price) or not math.isfinite(stop_price):
        return False
    if intent == "OPEN_LONG":
        return stop_price >= entry_price
    if intent == "OPEN_SHORT":
        return stop_price <= entry_price
    return False


def _reverse_open_quantity(*, action: str, quantity: float, position_quantity: float) -> float:
    signed_order = quantity if action == "BUY" else -quantity
    if position_quantity != 0 and position_quantity * signed_order < 0:
        return max(0.0, abs(signed_order) - abs(position_quantity))
    return quantity


def _risk_pct_for_quantity(quantity: float, stop_distance: float | None, equity: float) -> float | None:
    if stop_distance is None:
        return None
    if not math.isfinite(quantity) or not math.isfinite(stop_distance) or stop_distance < 0:
        return None
    if not math.isfinite(equity) or equity <= 0:
        return None
    risk_pct = quantity * stop_distance / equity
    return risk_pct if math.isfinite(risk_pct) else None


def _set_entry_risk_metrics(
    entry: dict,
    *,
    quantity: float,
    stop_distance: float | None,
    equity: float,
) -> None:
    entry["stop_distance"] = stop_distance
    entry["risk_pct"] = _risk_pct_for_quantity(quantity, stop_distance, equity)



def _create_plan_for_final_position(
    *,
    broker: SimBroker,
    plan_store: TradePlanStore,
    symbol: str,
    price: float,
    opened_at: str,
    raw_exit_plan: dict,
    reference_volatility: float | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_fallback_reason: str | None = None,
    llm_confidence: float | None = None,
) -> TradePlan | None:
    position = broker.positions().get(symbol)
    if position is None or position.quantity == 0:
        return None
    plan = create_trade_plan(
        symbol=symbol,
        side="LONG" if position.quantity > 0 else "SHORT",
        quantity=abs(position.quantity),
        entry_price=position.avg_price or price,
        opened_at=opened_at,
        raw_exit_plan=raw_exit_plan,
        reference_volatility=reference_volatility,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_fallback_reason=llm_fallback_reason,
        llm_confidence=llm_confidence,
    )
    plan_store.upsert(plan)
    return plan


def _clamp_exit_quantity(
    *,
    intent: str | None,
    action: str,
    quantity: float,
    position_quantity: float,
) -> tuple[float, str | None]:
    if intent not in {"CLOSE", "REDUCE"}:
        return quantity, None
    if position_quantity == 0:
        return 0.0, "no_position_to_reduce"
    if (position_quantity > 0 and action != "SELL") or (position_quantity < 0 and action != "BUY"):
        return 0.0, "exit_side_not_reducing"
    return min(quantity, abs(position_quantity)), None


def _bar_ts_after_plan_open(bar_ts: str, opened_at: str | None) -> bool:
    """Return True when bar_ts >= opened_at (bar started at or after plan entry).

    Parsing failures on either side are treated as True (extremes applied) so
    that old plans without a parseable opened_at keep the current behaviour.
    """
    if not opened_at:
        return True
    try:
        bar_dt = datetime.fromisoformat(bar_ts)
        plan_dt = datetime.fromisoformat(opened_at)
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        if plan_dt.tzinfo is None:
            plan_dt = plan_dt.replace(tzinfo=timezone.utc)
        return bar_dt >= plan_dt
    except (ValueError, TypeError):
        return True  # conservative: apply extremes on parse error


def _plan_snapshot(plan: TradePlan) -> dict:
    """Minimal JSON-serialisable snapshot of a plan at exit time (Chantier B)."""
    return {
        "id": plan.id,
        "symbol": plan.symbol,
        "side": plan.side,
        "entry_price": plan.entry_price,
        "reference_volatility": plan.reference_volatility,
        "quantity": plan.quantity,
        "remaining_quantity": plan.remaining_quantity,
        "hard_stop_price": plan.hard_stop_price,
        "take_profits": [
            {"name": tp.name, "price": tp.price, "fraction": tp.fraction}
            for tp in plan.take_profits
        ],
        "trailing_stop": (
            {
                "trail_type": plan.trailing_stop.trail_type,
                "trail_value": plan.trailing_stop.trail_value,
                "enabled_after": plan.trailing_stop.enabled_after,
                "trail_floored": plan.trailing_stop.trail_floored,
            }
            if plan.trailing_stop is not None
            else None
        ),
        "max_hold_minutes": plan.max_hold_minutes,
        "filled_take_profits": list(plan.filled_take_profits),
        "high_watermark": plan.high_watermark,
        "low_watermark": plan.low_watermark,
    }


def _llm_review_verdict(decision: codex_client.Decision) -> str:
    if decision.intent == "HOLD":
        return "intact"
    if decision.intent == "REDUCE":
        return "fragile"
    if decision.intent in {"CLOSE", "REVERSE"}:
        return "invalidated"
    return "fragile"


def _persist_last_llm_review(
    *,
    plan_store: TradePlanStore,
    symbol: str,
    now: datetime,
    decision: codex_client.Decision,
) -> None:
    if not (decision.llm_provider or decision.llm_model):
        return
    review = {
        "ts": now.astimezone(timezone.utc).isoformat(),
        "verdict": _llm_review_verdict(decision),
        "action": decision.action,
        "intent": decision.intent,
        "llm_provider": decision.llm_provider,
        "llm_model": decision.llm_model,
    }
    for plan in plan_store.open_plans():
        if plan.symbol == symbol:
            plan_store.upsert(replace(plan, last_llm_review=review))


def _last_review_by_symbol(
    plan_store: TradePlanStore, symbols: list[str]
) -> dict[str, dict]:
    """Mapping {symbole: last_llm_review} pour les plans ouverts du périmètre qui
    portent une revue. Narrow contract : on ne passe pas le store entier au batch,
    juste le dernier verdict à réinjecter (continuité de thèse au réveil)."""
    wanted = set(symbols)
    out: dict[str, dict] = {}
    for plan in plan_store.open_plans():
        if plan.symbol in wanted and plan.last_llm_review:
            out[plan.symbol] = plan.last_llm_review
    return out


def _build_execution_eligibility(
    symbols: list[str],
    *,
    stale_market_data: dict[str, dict],
    prices: dict[str, float],
    daily_bars_by_symbol: dict[str, list],
    data_age_by_symbol: dict[str, float],
    now: datetime,
    runtime_interval: str,
) -> dict[str, dict]:
    """Classifie chaque symbole en {execution, planning} (design §5.1) depuis l'état
    du cycle. execution gate les ordres (runtime frais + prix présent + session
    ouverte) ; planning autorise l'analyse/veille dès que le daily est présent (jugé
    frais par séance complétée), même runtime stale."""
    eligibility: dict[str, dict] = {}
    for sym in symbols:
        daily_bars = daily_bars_by_symbol.get(sym)
        stale = stale_market_data.get(sym) or {}
        eligibility[sym] = market.classify_symbol_context(
            runtime_interval=runtime_interval,
            has_runtime_price=sym in prices,
            is_runtime_stale=sym in stale_market_data,
            session_open=bool(market.session_snapshot(sym, now=now).get("open")),
            daily_fresh=bool(daily_bars),
            last_runtime_bar_ts=stale.get("last_bar_ts"),
            data_age_minutes=data_age_by_symbol.get(sym),
            daily_as_of=str(daily_bars[-1].ts) if daily_bars else None,
            next_session_open=market.next_regular_session_open(now, symbol=sym).isoformat(),
        )
    return eligibility


def _execution_blocked_reason(
    execution_eligibility: dict[str, dict], symbol: str, *, fail_closed: bool = False
) -> str | None:
    """Garde déterministe d'exécution (§13.5) : raison `execution:<reason>` si
    l'exécution est explicitement interdite pour ce symbole (session fermée, runtime
    stale, pas de prix), sinon None. Le RiskGate déterministe reste le fusible séparé.

    Symbole non classé (absent de `execution_eligibility`) : `fail_closed=True` =>
    bloqué (`execution:unclassified`), pour les OUVERTURES/REVERSE (invariant §10
    "aucun ordre d'ouverture sans execution.enabled=true"). `fail_closed=False`
    (défaut) => non bloqué, pour ne jamais empêcher une SORTIE de protection par
    manque d'info."""
    ctx = (execution_eligibility.get(symbol) or {}).get("execution")
    if ctx is None:
        return "execution:unclassified" if fail_closed else None
    if ctx.get("enabled"):
        return None
    return f"execution:{ctx.get('reason') or 'disabled'}"


def _positive_finite_float(raw: object) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _cockpit_vol_fraction(cockpit: dict, symbol: str) -> float | None:
    cols = cockpit.get("cols")
    rows = cockpit.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list):
        return None
    try:
        symbol_index = cols.index("s")
    except ValueError:
        return None
    vol_indices = []
    for column in ("vol_d", "vol"):
        try:
            vol_indices.append(cols.index(column))
        except ValueError:
            continue
    if not vol_indices:
        return None
    for row in rows:
        if not isinstance(row, list):
            continue
        if len(row) <= symbol_index:
            continue
        if row[symbol_index] == symbol:
            for vol_index in vol_indices:
                if len(row) <= vol_index:
                    continue
                value = _positive_finite_float(row[vol_index])
                if value is not None:
                    return value
            return None
    return None


def _feature_vol_fraction(
    symbol: str,
    tradable_bars_by_symbol: dict[str, list],
) -> float | None:
    if symbol not in tradable_bars_by_symbol:
        return None
    snapshot = build_indicator_snapshot(
        tradable_bars_by_symbol,
        symbols=[symbol],
        names=["volatility"],
        window=48,
    )
    item = snapshot.get(symbol, {})
    indicators = item.get("indicators") if isinstance(item, dict) else None
    if not isinstance(indicators, dict):
        return None
    return _positive_finite_float(indicators.get("volatility"))


def _reference_volatility_for_symbol(
    symbol: str,
    *,
    entry_price: float,
    cockpit: dict,
    tradable_bars_by_symbol: dict[str, list],
) -> float | None:
    vol_fraction = _cockpit_vol_fraction(cockpit, symbol)
    if vol_fraction is None:
        vol_fraction = _feature_vol_fraction(symbol, tradable_bars_by_symbol)
    price = _positive_finite_float(entry_price)
    if price is None or vol_fraction is None:
        return None
    return price * vol_fraction


def _apply_planned_exits(
    *,
    broker: SimBroker,
    plan_store: TradePlanStore,
    prices: dict[str, float],
    bars_by_symbol: dict[str, list] | None = None,
    bars_intervals_by_symbol: dict[str, str] | None = None,
    valuation_prices: dict[str, float] | None = None,
    now: datetime,
    dry_run: bool,
    starting_equity: float,
    execution_eligibility: dict[str, dict] | None = None,
    rate_fn: Callable[[str], float] | None = None,
) -> list[dict]:
    entries: list[dict] = []
    for plan in plan_store.open_plans():
        price = prices.get(plan.symbol)
        if price is None:
            continue

        # Extract bar extremes for intra-bar stop/TP detection.
        #
        # For 15m (runtime interval), we use only the last bar — one bar = one decision window.
        # For 5m (exit-check interval), we aggregate over EXIT_CHECK_WINDOW_BARS recent bars
        # (default 3 × 5m = 15m window) so that a spike in any bar within the window is caught,
        # not just the very last one. Without aggregation a spike in bar N-1 that closes by bar N
        # would be invisible, whereas the 15m bar would have captured its high/low.
        #
        # Temporal guard: bars are filtered to ts >= plan.opened_at before aggregation, so
        # pre-entry spikes in the window are excluded. For unparseable opened_at, all bars pass
        # (conservative: we'd rather detect a spike than miss it).
        bar_high: float | None = None
        bar_low: float | None = None
        interval_used = (bars_intervals_by_symbol or {}).get(plan.symbol, DEFAULT_RUNTIME_INTERVAL)
        if bars_by_symbol is not None:
            bars = bars_by_symbol.get(plan.symbol)
            if bars:
                if interval_used == EXIT_CHECK_INTERVAL:
                    # 5m path: aggregate high/low over the recent window, respecting temporal guard.
                    window = bars[-EXIT_CHECK_WINDOW_BARS:]
                    eligible = [b for b in window if _bar_ts_after_plan_open(b.ts, plan.opened_at)]
                    if eligible:
                        bar_high = max(b.high for b in eligible)
                        bar_low = min(b.low for b in eligible)
                else:
                    # 15m path: single last bar with temporal guard (existing behaviour).
                    last_bar = bars[-1]
                    if _bar_ts_after_plan_open(last_bar.ts, plan.opened_at):
                        bar_high = last_bar.high
                        bar_low = last_bar.low

        evaluation = evaluate_plan(plan, price=price, bar_high=bar_high, bar_low=bar_low, now=now)
        if evaluation.signal is None:
            if not dry_run:
                plan_store.upsert(evaluation.updated_plan)
            continue

        requested_quantity = evaluation.signal.quantity
        position = broker.positions().get(plan.symbol)
        clamped_quantity, exit_block_reason = _clamp_exit_quantity(
            intent="CLOSE",
            action=evaluation.signal.side,
            quantity=requested_quantity,
            position_quantity=0.0 if position is None else position.quantity,
        )
        if exit_block_reason is not None or clamped_quantity <= 0:
            if not dry_run:
                plan_store.close(plan.id)
            blocked_fill = (
                evaluation.signal.fill_price if evaluation.signal.fill_price is not None else price
            )
            entries.append(
                {
                    "symbol": plan.symbol,
                    "side": evaluation.signal.side,
                    "quantity": clamped_quantity,
                    "requested_quantity": requested_quantity,
                    "reason": exit_block_reason or "zero_exit_quantity",
                    "price": price,
                    "fill_price": blocked_fill,  # MINOR 5
                    "plan_snapshot": _plan_snapshot(plan),  # MINOR 5
                    "bars_interval": (bars_intervals_by_symbol or {}).get(plan.symbol, DEFAULT_RUNTIME_INTERVAL),
                    "executed": False,
                    "dry_run": dry_run,
                }
            )
            continue

        # §13.5 — garde déterministe d'exécution AUSSI sur les sorties mécaniques :
        # marché fermé / runtime stale => l'ordre ne part pas (évite un fill irréaliste
        # hors séance). Le plan reste OUVERT : la sortie repartira au prochain cycle
        # exécutable. Le calcul de la sortie a bien eu lieu, seul le submit est retenu.
        exit_execution_blocked = _execution_blocked_reason(execution_eligibility or {}, plan.symbol)
        if exit_execution_blocked is not None:
            entries.append(
                {
                    "symbol": plan.symbol,
                    "side": evaluation.signal.side,
                    "quantity": clamped_quantity,
                    "reason": exit_execution_blocked,
                    "price": price,
                    "executed": False,
                    "dry_run": dry_run,
                }
            )
            continue

        # Use fill_price from the signal when available (intra-bar stop/TP detection).
        # fill_price is conservative: never better than the stop/TP level.
        effective_fill_price = evaluation.signal.fill_price if evaluation.signal.fill_price is not None else price

        order = Order(
            symbol=evaluation.signal.symbol,
            side=evaluation.signal.side,
            quantity=clamped_quantity,
            rationale=evaluation.signal.reason,
        )
        _fill_rate = rate_fn(plan.symbol) if rate_fn is not None else 1.0
        fill = broker.submit(order, effective_fill_price, now.isoformat(), dry_run=dry_run, fx_rate=_fill_rate)
        if not dry_run:
            final_position = broker.positions().get(plan.symbol)
            final_quantity = 0.0 if final_position is None else final_position.quantity
            if final_quantity == 0:
                plan_store.close_symbol(plan.symbol)
            elif evaluation.close_plan:
                plan_store.close(plan.id)
                plan_store.sync_symbol_quantity(plan.symbol, abs(final_quantity))
            else:
                plan_store.upsert(evaluation.updated_plan)
            plan_store.sync_symbol_quantity(plan.symbol, abs(final_quantity))
            if fill is not None:
                price_map = valuation_prices if valuation_prices is not None else prices
                latest = portfolio.snapshot(broker, lambda s: price_map.get(s, 0.0), starting_equity, fx_rate_of=rate_fn)
                _append_model_performance(
                    ts=fill.ts,
                    symbol=plan.symbol,
                    action=evaluation.signal.side,
                    intent="PLANNED_EXIT",
                    exit_reason=evaluation.signal.reason,
                    source_plan_id=plan.id,
                    quantity=clamped_quantity,
                    price=effective_fill_price,
                    commission=fill.commission,
                    commission_currency=fill.commission_currency,
                    commission_model=fill.commission_model,
                    fx_rate=fill.fx_rate,
                    confidence=plan.llm_confidence,
                    llm_provider=plan.llm_provider or "unknown",
                    llm_model=plan.llm_model or "unknown",
                    llm_fallback_reason=plan.llm_fallback_reason,
                    equity=latest.equity,
                    cash=latest.cash,
                    position_quantity=final_quantity,
                )
        entries.append(
            {
                "symbol": plan.symbol,
                "side": evaluation.signal.side,
                "quantity": clamped_quantity,
                "requested_quantity": requested_quantity,
                "reason": evaluation.signal.reason,
                "price": price,
                "fill_price": effective_fill_price,  # Chantier B
                "plan_snapshot": _plan_snapshot(plan),  # Chantier B
                "bars_interval": (bars_intervals_by_symbol or {}).get(plan.symbol, DEFAULT_RUNTIME_INTERVAL),
                "executed": fill is not None,
                "dry_run": dry_run,
                **(
                    {
                        "commission": fill.commission,
                        "commission_currency": fill.commission_currency,
                        "commission_model": fill.commission_model,
                        "fx_rate": fill.fx_rate,
                    }
                    if fill is not None
                    else {}
                ),
            }
        )
    return entries


def _exit_watch_cooldown_elapsed(watch: dict, *, now: datetime) -> bool:
    last_raw = watch.get("last_triggered_at")
    if not last_raw:
        return True
    try:
        last = datetime.fromisoformat(str(last_raw))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    cooldown = float(watch.get("cooldown_minutes") or 15.0)
    return (now - last).total_seconds() >= cooldown * 60.0


def _scan_exit_watches(
    *,
    plan_store: TradePlanStore,
    bars_by_symbol: dict[str, list],
    symbols: list[str],
    now: datetime,
    dry_run: bool,
    bars_interval: str,
    data_source: object,
) -> list[dict]:
    plans_by_watch_id: dict[str, object] = {}
    watches: list[dict] = []
    for plan in plan_store.open_plans():
        if plan.symbol not in symbols or not isinstance(plan.exit_watch, dict):
            continue
        if not _exit_watch_cooldown_elapsed(plan.exit_watch, now=now):
            continue
        watch = dict(plan.exit_watch)
        watch["symbol"] = plan.symbol
        watch["on_trigger"] = "WAKE"
        watch["source"] = "exit_watch"
        watches.append(watch)
        plans_by_watch_id[str(watch["id"])] = plan
    if not watches:
        return []

    # Les barres runtime préchargées sont à `bars_interval` (15m), pas 1h : on les
    # indexe sous leur vrai intervalle pour qu'une watch ne lise pas le mauvais TF.
    bars_by_key: dict[tuple[str, str], list] = {
        (symbol, bars_interval): bars
        for symbol, bars in bars_by_symbol.items()
    }
    for symbol, interval, lookback in watch_market_requests(watches, universe_symbols=symbols):
        key = (symbol, interval)
        if key in bars_by_key:
            continue
        try:
            bars_by_key[key] = data_source.get_bars(symbol, lookback=lookback, interval=interval)
        except market.MarketError as exc:
            if _is_connection_market_error(exc):
                raise
            log.warning("exit_watch data unavailable %s/%s: %s", symbol, interval, exc.code)

    triggered = evaluate_indicator_watches(watches, bars_by_key, now=now)
    enriched: list[dict] = []
    for event in triggered:
        plan = plans_by_watch_id.get(str(event["watch_id"]))
        if plan is None:
            continue
        event = {
            **event,
            "source": "exit_watch",
            "plan_id": plan.id,
            "on_trigger": "WAKE",
        }
        enriched.append(event)
        if not dry_run:
            watch = dict(plan.exit_watch or {})
            watch["last_triggered_at"] = now.astimezone(timezone.utc).isoformat()
            plan_store.upsert(replace(plan, exit_watch=watch))
        _append_event(
            "exit_watch_triggered",
            symbol=event["symbol"],
            plan_id=event["plan_id"],
            watch_id=event["watch_id"],
        )
    if enriched:
        _log_cycle_progress("[exit_watch] triggered=%d symbols=%s", len(enriched), [item["symbol"] for item in enriched])
    return enriched


def _scan_indicator_watches(
    symbols: list[str],
    *,
    sched: scheduler.Scheduler,
    now: datetime,
    data_source: object,
) -> list[dict]:
    watches = [
        watch
        for watch in sched.active_indicator_watches(now=now)
        if watch.get("symbol") in symbols
    ]
    if not watches:
        return []

    bars_by_key: dict[tuple[str, str], list] = {}
    for symbol, interval, lookback in watch_market_requests(watches, universe_symbols=symbols):
        try:
            bars_by_key[(symbol, interval)] = data_source.get_bars(symbol, lookback=lookback, interval=interval)
        except market.MarketError as exc:
            if _is_connection_market_error(exc):
                raise
            log.warning("indicator_watch data unavailable %s/%s: %s", symbol, interval, exc.code)

    triggered = evaluate_indicator_watches(watches, bars_by_key, now=now)
    for event in triggered:
        sched.remove_indicator_watch(str(event["watch_id"]))
        sched.set_symbol_next_wake(str(event["symbol"]), now.isoformat())
        _append_event(
            "indicator_watch_triggered",
            symbol=event["symbol"],
            watch_id=event["watch_id"],
            on_trigger=event.get("on_trigger"),
        )
    if triggered:
        _log_cycle_progress("[indicator_watch] triggered=%d symbols=%s", len(triggered), [item["symbol"] for item in triggered])
    return triggered


def _context_request_summary(
    req: codex_client.ContextResearchRequest,
    *,
    resolved: int,
) -> dict:
    return {
        "rounds": 1,
        "requested": [
            {
                "symbol": request.symbol,
                "indicators": list(request.indicators),
                "timeframe": request.timeframe,
            }
            for request in req.requests
        ],
        "resolved": resolved,
    }


def _active_watch_summaries_by_symbol(
    *,
    sched: scheduler.Scheduler | None,
    symbols: list[str],
    now: datetime,
) -> dict[str, list[dict]]:
    summaries = {symbol: [] for symbol in symbols}
    if sched is None:
        return summaries
    for watch in sched.active_indicator_watches(now=now):
        symbol = str(watch.get("symbol"))
        if symbol in summaries:
            summaries[symbol].append(summarize_watch(watch))
    return summaries


def _apply_decision_schedule(
    *,
    sched: scheduler.Scheduler | None,
    sym: str,
    now: datetime,
    next_wake_in_minutes: float | None,
    cancel_watch_ids: list[str],
    pending_indicator_watch: dict | None,
    entry: dict,
) -> None:
    if sched is None:
        return
    if next_wake_in_minutes is not None:
        sched.set_symbol_next_wake_in(sym, minutes=next_wake_in_minutes, now=now)
    else:
        sched.clear_symbol_next_wake(sym)
    for watch_id in cancel_watch_ids:
        watch_id = str(watch_id)
        if not watch_id.startswith(f"{sym}:"):
            _append_event(
                "watch_cancel_rejected",
                symbol=sym,
                watch_id=watch_id,
                reason="not_owned_by_symbol",
            )
            continue
        sched.remove_indicator_watch(watch_id)
        _append_event("watch_cancelled_by_agent", symbol=sym, watch_id=watch_id)
    if pending_indicator_watch is not None:
        sched.set_symbol_indicator_watch(sym, pending_indicator_watch)
        entry["indicator_watch_created"] = True
        entry["indicator_watch"] = {
            "id": pending_indicator_watch["id"],
            "expires_at": pending_indicator_watch["expires_at"],
            "logic": pending_indicator_watch["logic"],
            "on_trigger": pending_indicator_watch["on_trigger"],
            "conditions": pending_indicator_watch["conditions"],
        }
        if "order" in pending_indicator_watch:
            entry["indicator_watch"]["order"] = pending_indicator_watch["order"]


def _build_recall_provider(
    store: recall_store_mod.LearningsStore,
    now: datetime,
    *,
    embedder: Callable[[list[str], ...], list[bytes]] | None = None,
) -> Callable[[dict], dict]:
    """Construit le provider learnings_recall injectable dans le ToolContext.

    Paramètres :
    - ``store`` : LearningsStore SQLite (dérivé reconstructible).
    - ``now`` : timestamp du cycle courant (borné temporel de la recherche).
    - ``embedder`` : callable pour les embeddings (injectable pour les tests) ;
      par défaut ``embeddings_mod.embed_texts``.

    Le provider retourne ``{"rows": [...]}`` — le handler re-tronque à ≤ 8.
    Sans OPENAI_API_KEY ou sans query, la recherche tombe en mode FTS5+facettes.
    """
    _use_default_embedder = embedder is None
    _embed = embeddings_mod.embed_texts if _use_default_embedder else embedder
    # Cache run-local : évite de ré-embedder la même query dans le même cycle.
    _embed_cache: dict[str, bytes] = {}

    def _provider(args: dict) -> dict:
        query = args.get("query")
        query_vec: bytes | None = None
        if query:
            if query in _embed_cache:
                query_vec = _embed_cache[query]
            else:
                api_key = os.getenv("OPENAI_API_KEY")
                if api_key:
                    try:
                        # timeout_s=3 seulement pour l'embedder par défaut ;
                        # les embedders injectés (tests) gèrent leur propre timeout.
                        if _use_default_embedder:
                            blobs = _embed([query], api_key=api_key, timeout_s=3)
                        else:
                            blobs = _embed([query], api_key=api_key)
                        query_vec = blobs[0] if blobs else None
                    except Exception:  # noqa: BLE001 — embed optionnel, dégradation FTS5
                        log.warning("learnings_recall: embed échoué, dégradation FTS5")
                        query_vec = None
                    if query_vec is not None:
                        _embed_cache[query] = query_vec
        limit = min(int(args.get("limit") or 5), 8)
        rows = store.search(
            query_vec=query_vec,
            text_query=query,
            symbol=args.get("symbol"),
            family=args.get("family"),
            limit=limit,
            now=now,
        )
        return {"rows": rows}

    return _provider


def _run_tool_round(
    request: codex_client.BatchToolCallRequest,
    *,
    chunk: list[str],
    now: datetime,
    data_age_by_symbol: dict[str, float],
    market_contexts: dict[str, dict],
    active_watches_by_symbol: dict[str, list],
    shared_context: dict,
    indicator_resolver: object,
    learnings_recall_provider: Callable[[dict], dict] | None = None,
) -> tuple[list[dict], dict]:
    """Exécute UNE tournée d'outils bornée et retourne :
    - results_payload : liste compacte réinjectable dans le prompt du tour final
    - runtime_payload : dict durable pour decisions.jsonl (runtime.tool_*)

    Toute erreur outil est absorbée en résultat compact — aucune exception ne
    remonte au daemon (AX §4 : machine-readable errors, §8 : structured outputs).
    """
    # Borner le contexte au chunk : évite la fuite inter-chunks en parallélisme>1
    # (un call sans symbol explicite comme get_attribution voyait TOUT le batch).
    chunk_set = frozenset(chunk)
    context = agent_tools.ToolContext(
        now=now,
        allowed_symbols=chunk_set,
        data_age_by_symbol={s: v for s, v in data_age_by_symbol.items() if s in chunk_set},
        market_context_by_symbol={s: v for s, v in market_contexts.items() if s in chunk_set},
        active_watches_by_symbol={s: v for s, v in active_watches_by_symbol.items() if s in chunk_set},
        attribution=shared_context.get("attribution"),
        # V0 : providers lourds non câblés — répondent "unavailable" proprement.
        # À brancher quand la mesure d'usage le justifie (Phase 2+).
        position_risk_provider=None,
        recent_decisions_provider=None,
        indicator_resolver=indicator_resolver,
        learnings_recall_provider=learnings_recall_provider,
    )
    results, traces = agent_tools.execute_tool_round(
        request.calls,
        context=context,
        limits=agent_tools.ToolRoundLimits(),
        allowed_tools=frozenset(agent_tools.TOOL_REGISTRY),
    )
    results_prompt = agent_tools.results_prompt_payload(results)
    runtime = agent_tools.round_runtime_payload(traces, rounds=1)

    # Enrichir le detail des appels recall_learnings avec les note_ids retournés.
    # Ces ids sont stockés dans le trace durable (decisions.jsonl runtime.tool_calls)
    # et utilisés par record_decision pour appeler store.record_recall (design §4.4).
    #
    # Association PAR POSITION : results et tool_calls sont ordonnés 1:1,
    # sauf une éventuelle trace sentinel "[sentinel]" en tête de tc_list
    # (OUTCOME_TRUNCATED, id="[sentinel]") sans résultat correspondant.
    tc_list = runtime["tool_calls"]
    sentinel_offset = 1 if (tc_list and tc_list[0].get("id") == "[sentinel]") else 0
    for pos, tc in enumerate(tc_list[sentinel_offset:]):
        if tc["tool"] == "recall_learnings" and tc["outcome"] == agent_tools.OUTCOME_OK:
            r = results[pos] if pos < len(results) else None
            if r and r.ok and isinstance(r.result, dict):
                rows = r.result.get("rows", [])
                note_ids = [
                    row["id"] for row in rows
                    if isinstance(row, dict) and isinstance(row.get("id"), int)
                ]
                tc["detail"] = {**tc["detail"], "note_ids": note_ids}

    return results_prompt, runtime


def _batch_decide(
    *,
    decidable: list[str],
    mandate: str,
    memory: str,
    shared_context: dict,
    triggers_by_symbol: dict[str, list[dict]],
    tradable_bars_by_symbol: dict[str, list],
    tradable_symbols: list[str],
    runtime_interval: str,
    runtime_lookback: str,
    max_context_requests_per_symbol: int,
    max_indicators_per_request: int,
    max_model_calls: int,
    now: datetime,
    data_age_by_symbol: dict[str, float],
    sched: scheduler.Scheduler | None = None,
    last_review_by_symbol: dict[str, dict] | None = None,
    market_context_by_symbol: dict[str, dict] | None = None,
    decision_timeout_s: int = 900,
    decision_batch_size: int = DEFAULT_DECISION_BATCH_SIZE,
    decision_batch_parallelism: int = DEFAULT_DECISION_BATCH_PARALLELISM,
    agent_tools_enabled: bool = False,
    learnings_recall_provider: Callable[[dict], dict] | None = None,
) -> tuple[dict[str, codex_client.Decision], int]:
    """Décide les symboles dus par chunks LLM bornés et parallélisables.

    Chaque chunk consomme un appel modèle et respecte `max_model_calls`. Les
    demandes REQUEST_CONTEXT sont résolues puis re-décidées en chunks séparés.
    Quand `agent_tools_enabled=True`, le LLM peut émettre UNE tournée d'outils
    lecture-seule (flag CASYS_AGENT_TOOLS_ENABLED) ; chaque chunk avec tournée
    consomme alors 2 appels modèle au lieu d'un.
    """
    if not decidable:
        return {}, 0
    if max_model_calls < 1:
        return {sym: codex_client.Decision.hold(sym, "model_call_budget_exhausted") for sym in decidable}, 0
    active_watches_by_symbol = _active_watch_summaries_by_symbol(sched=sched, symbols=decidable, now=now)

    reviews = last_review_by_symbol or {}
    market_contexts = market_context_by_symbol or {}

    def _symbol_facts(sym: str) -> dict:
        # Faits calculés par le code (pas des consignes en prose) : âge réel des
        # prix et état de la séance de la place du symbole. Âge inconnu = None.
        age = data_age_by_symbol.get(sym)
        session = market.session_snapshot(sym, now=now)
        facts = {
            "data_age_m": None if age is None else int(round(age)),
            "session": {"open": bool(session.get("open"))},
            "active_watches": active_watches_by_symbol.get(sym, []),
        }
        # Séparation analyse/exécution (§5.1) : le LLM voit s'il peut exécuter
        # (execution.enabled) distinctement de s'il peut seulement analyser/planifier
        # (planning.enabled) — il ne confond plus une thèse swing et un ordre immédiat.
        mc = market_contexts.get(sym)
        if mc:
            facts["execution"] = mc.get("execution")
            facts["planning"] = mc.get("planning")
        # Continuité de thèse : le dernier verdict LLM persisté dans le TradePlan
        # (sessions acpx jetables) est réinjecté au réveil d'une position ouverte.
        review = reviews.get(sym)
        if review:
            facts["last_llm_review"] = review
        return facts

    per_symbol = {
        sym: {"indicator_triggers": triggers_by_symbol.get(sym, []), **_symbol_facts(sym)}
        for sym in decidable
    }

    batch_size = max(1, int(decision_batch_size))
    parallelism = max(1, int(decision_batch_parallelism))

    # Résolveur d'indicateurs réutilisable pour la tournée d'outils (mêmes
    # bornes que REQUEST_CONTEXT — le coût d'un resolve reste identique).
    def _indicator_resolver(requests):
        return resolve_indicator_requests(
            requests,
            tradable_bars_by_symbol,
            symbols=tradable_symbols,
            max_requests=max_context_requests_per_symbol,
            max_indicators=max_indicators_per_request,
            cached_interval=runtime_interval,
            cached_lookback=runtime_lookback,
        )

    def _chunks(symbols: list[str]) -> list[list[str]]:
        return [symbols[start : start + batch_size] for start in range(0, len(symbols), batch_size)]

    def _decide_chunks(
        *,
        symbols: list[str],
        per_symbol_payload: dict[str, dict],
        allow_context_request: bool,
        budget: int,
    ) -> tuple[dict[str, object], int, list[str]]:
        chunks = _chunks(symbols)
        # Pire cas budget en mode tournée : 2 appels par chunk (tournée + final).
        # On réserve ce pire cas quand allow_tool_calls est actif pour ce pass.
        _chunk_budget = budget // 2 if (agent_tools_enabled and allow_context_request) else budget
        allowed_chunks = chunks[: max(0, _chunk_budget)]
        skipped = [sym for chunk in chunks[max(0, _chunk_budget) :] for sym in chunk]
        if not allowed_chunks:
            return {}, 0, skipped

        def _call(chunk: list[str]) -> tuple[dict[str, object], int]:
            """Retourne (réponses par symbole, nombre d'appels modèle consommés).

            Quand agent_tools_enabled, le premier appel peut rendre une tournée
            d'outils (BatchToolCallRequest) ; le daemon l'exécute et relance un
            tour final — tout en absorbant les erreurs (design §11, §4).
            """
            try:
                resp = codex_client.decide_batch(
                    symbols=chunk,
                    mandate=mandate,
                    memory=memory,
                    shared_context=shared_context,
                    per_symbol={sym: per_symbol_payload[sym] for sym in chunk},
                    allow_context_request=allow_context_request,
                    allow_tool_calls=agent_tools_enabled and allow_context_request,
                    timeout_s=decision_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                return (
                    {sym: codex_client.Decision.hold(sym, f"llm_failed:batch_exception:{type(exc).__name__}") for sym in chunk},
                    1,
                )

            if not (agent_tools_enabled and isinstance(resp, codex_client.BatchToolCallRequest)):
                # Comportement historique : réponse décision directe.
                return resp, 1

            # --- Tournée d'outils (flag actif, LLM a demandé des outils) ---
            results_payload, runtime_payload = _run_tool_round(
                resp,
                chunk=chunk,
                now=now,
                data_age_by_symbol=data_age_by_symbol,
                market_contexts=market_contexts,
                active_watches_by_symbol=active_watches_by_symbol,
                shared_context=shared_context,
                indicator_resolver=_indicator_resolver,
                learnings_recall_provider=learnings_recall_provider,
            )
            # Enrichir le payload par-symbole avec les tool_results filtrés.
            # Un call sans symbole explicite (scope global) est réinjecté à tous.
            trace_calls = runtime_payload["tool_calls"]
            per_symbol_round2 = {}
            for sym in chunk:
                sym_traces = agent_tools.calls_for_symbol(trace_calls, sym)
                sym_ids = {t["id"] for t in sym_traces}
                sym_results = [r for r in results_payload if r["id"] in sym_ids]
                per_symbol_round2[sym] = {**per_symbol_payload[sym], "tool_results": sym_results}

            # Tour final : le LLM DOIT décider — plus de tool_calls acceptés.
            try:
                resp2 = codex_client.decide_batch(
                    symbols=chunk,
                    mandate=mandate,
                    memory=memory,
                    shared_context=shared_context,
                    per_symbol=per_symbol_round2,
                    allow_context_request=False,
                    allow_tool_calls=False,
                    timeout_s=decision_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                return (
                    {sym: codex_client.Decision.hold(sym, f"llm_failed:batch_exception:{type(exc).__name__}") for sym in chunk},
                    2,
                )

            # Deuxième tournée d'outils au tour final → blocage HOLD (design §6.2).
            # Défense en profondeur : le parser (allow_tool_calls=False → parse_batch)
            # protège déjà en amont ; ce guard couvre un futur refactor.
            if isinstance(resp2, codex_client.BatchToolCallRequest):
                return (
                    {sym: codex_client.Decision.hold(sym, "tool_loop_blocked") for sym in chunk},
                    2,
                )

            # Attacher les traces d'outils à chaque décision pour persistance ledger.
            final_decisions: dict[str, object] = {}
            for sym in chunk:
                decision = resp2.get(sym, codex_client.Decision.hold(sym, "missing_in_batch"))
                if isinstance(decision, codex_client.Decision):
                    sym_traces = agent_tools.calls_for_symbol(trace_calls, sym)
                    decision = replace(decision, domain_tools={
                        "tool_rounds": runtime_payload["tool_rounds"],
                        "tool_calls": sym_traces,
                    })
                final_decisions[sym] = decision
            return final_decisions, 2

        responses_by_symbol: dict[str, object] = {}
        total_calls = 0
        if parallelism == 1 or len(allowed_chunks) == 1:
            for chunk in allowed_chunks:
                chunk_responses, chunk_n_calls = _call(chunk)
                responses_by_symbol.update(chunk_responses)
                total_calls += chunk_n_calls
        else:
            with ThreadPoolExecutor(max_workers=min(parallelism, len(allowed_chunks))) as executor:
                futures = [executor.submit(_call, chunk) for chunk in allowed_chunks]
                for future in as_completed(futures):
                    chunk_responses, chunk_n_calls = future.result()
                    responses_by_symbol.update(chunk_responses)
                    total_calls += chunk_n_calls
        return responses_by_symbol, total_calls, skipped

    responses, calls, skipped_symbols = _decide_chunks(
        symbols=decidable,
        per_symbol_payload=per_symbol,
        allow_context_request=True,
        budget=max_model_calls,
    )
    decisions: dict[str, codex_client.Decision] = {}
    need: dict[str, codex_client.ContextResearchRequest] = {}
    for sym, resp in responses.items():
        if isinstance(resp, codex_client.ContextResearchRequest):
            need[sym] = resp
        else:
            decisions[sym] = resp
    for sym in skipped_symbols:
        decisions[sym] = codex_client.Decision.hold(sym, "model_call_budget_exhausted")

    if need and calls >= max_model_calls:
        # Budget épuisé : pas de 2e batch pour résoudre les demandes de contexte.
        for sym, req in need.items():
            decisions[sym] = replace(
                codex_client.Decision.hold(sym, "model_call_budget_exhausted_after_context"),
                context_request=_context_request_summary(req, resolved=0),
            )
        need = {}

    if need:
        context_requests: dict[str, dict] = {}
        per_symbol2: dict[str, dict] = {}
        for sym, req in need.items():
            research = resolve_indicator_requests(
                req.requests,
                tradable_bars_by_symbol,
                symbols=tradable_symbols,
                max_requests=max_context_requests_per_symbol,
                max_indicators=max_indicators_per_request,
                cached_interval=runtime_interval,
                cached_lookback=runtime_lookback,
            )
            _append_event("context_resolved", symbol=sym, requested=len(req.requests), resolved=len(research["requests"]))
            context_requests[sym] = _context_request_summary(req, resolved=len(research["requests"]))
            per_symbol2[sym] = {
                "indicator_triggers": triggers_by_symbol.get(sym, []),
                **_symbol_facts(sym),
                "research": research,
                # Sessions jetables : le 2e batch n'a pas l'historique du 1er ; on
                # repasse la rationale de la demande pour reprendre le raisonnement.
                "prior_rationale": req.rationale,
            }
        responses2, calls2, skipped_context_symbols = _decide_chunks(
            symbols=list(need),
            per_symbol_payload=per_symbol2,
            allow_context_request=False,
            budget=max_model_calls - calls,
        )
        calls += calls2
        for sym in skipped_context_symbols:
            decisions[sym] = replace(
                codex_client.Decision.hold(sym, "model_call_budget_exhausted_after_context"),
                context_request=context_requests[sym],
            )
        for sym in need:
            if sym in skipped_context_symbols:
                continue
            resp2 = responses2.get(sym)
            decision = (
                resp2
                if isinstance(resp2, codex_client.Decision)
                else codex_client.Decision.hold(sym, "context_loop_blocked")
            )
            decisions[sym] = replace(decision, context_request=context_requests[sym])

    return decisions, calls


def _is_valid_5m_bar(bar: object) -> bool:
    """Retourne True si la barre est utilisable pour les checks de sortie.

    Critères :
    - ts parsable en ISO-8601 (garde temporelle en aval exige un ts valide).
    - high et low sont des flottants finis avec low <= high.
    """
    try:
        ts_str = getattr(bar, "ts", None)
        if ts_str is None:
            return False
        parsed = market._parse_ts(str(ts_str))
        if parsed is None:
            return False
        h = getattr(bar, "high", None)
        lo = getattr(bar, "low", None)
        if h is None or lo is None:
            return False
        if not (math.isfinite(float(h)) and math.isfinite(float(lo))):
            return False
        if float(lo) > float(h):
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


def _fetch_5m_bars_for_open_plans(
    *,
    plan_store: TradePlanStore,
    data_source: object,
    tradable_bars_by_symbol: dict[str, list],
    tradable_prices: dict[str, float],
    now: datetime,
) -> tuple[dict[str, list], dict[str, str]]:
    """Fetche et valide des barres 5m pour les symboles ayant un plan ouvert.

    Retourne :
      - exit_bars_by_symbol : tradable_bars_by_symbol enrichi avec les barres 5m
        VALIDES et FRAÎCHES pour chaque symbole réussi ; fallback sur les 15m sinon.
      - intervals_by_symbol : intervalle réellement utilisé par symbole ('5m' ou '15m').

    Validation par barre : ts parsable, high/low finis et cohérents (low ≤ high).
    Fraîcheur : assess_freshness appliqué avec budget propre à '5m'.
    Aucun fetch pour les symboles SANS plan ouvert (coût = 0).
    Toute exception ou résultat invalide/stale → fallback 15m sans jamais bloquer.
    """
    open_symbols = {plan.symbol for plan in plan_store.open_plans() if plan.symbol in tradable_prices}
    exit_bars: dict[str, list] = dict(tradable_bars_by_symbol)
    intervals: dict[str, str] = {sym: DEFAULT_RUNTIME_INTERVAL for sym in tradable_bars_by_symbol}

    freshness_budget = market.freshness_budget_minutes(EXIT_CHECK_INTERVAL)

    for symbol in open_symbols:
        try:
            bars_5m = data_source.get_bars(symbol, lookback=EXIT_CHECK_LOOKBACK, interval=EXIT_CHECK_INTERVAL)
        except Exception as exc:  # noqa: BLE001 — fallback inconditionnelle, ne jamais bloquer une sortie
            log.warning(
                "5m bars fetch failed for %s — falling back to %s (%s)",
                symbol, DEFAULT_RUNTIME_INTERVAL, exc,
            )
            continue
        if not bars_5m:
            log.warning("5m bars empty for %s — falling back to %s", symbol, DEFAULT_RUNTIME_INTERVAL)
            continue

        # Valider chaque barre individuellement — rejeter toute barre malformée.
        valid_bars = [b for b in bars_5m if _is_valid_5m_bar(b)]
        if not valid_bars:
            log.warning(
                "5m bars all invalid for %s — falling back to %s", symbol, DEFAULT_RUNTIME_INTERVAL
            )
            continue

        # Contrôle de fraîcheur sur les barres valides (budget propre à l'intervalle 5m).
        freshness = market.assess_freshness(valid_bars, now=now, max_age_minutes=freshness_budget)
        if not freshness.fresh:
            log.debug(
                "5m bars stale for %s (%s, age=%.1f min) — falling back to %s",
                symbol, freshness.reason, freshness.age_minutes or 0.0, DEFAULT_RUNTIME_INTERVAL,
            )
            continue

        exit_bars[symbol] = valid_bars
        intervals[symbol] = EXIT_CHECK_INTERVAL

    return exit_bars, intervals


def run_cycle(
    *,
    dry_run: bool,
    now: datetime | None = None,
    symbols_filter: list[str] | None = None,
    sched: scheduler.Scheduler | None = None,
    data_source: object,
    default_wake_minutes: float = 30.0,
    min_wake_minutes: float = 5.0,
    max_wake_minutes: float = 240.0,
    max_context_requests_per_symbol: int = 2,
    max_indicators_per_request: int = 4,
    max_model_calls_per_cycle: int = 25,
    max_market_data_age_minutes: float = DEFAULT_MAX_MARKET_DATA_AGE_MINUTES,
    runtime_interval: str = DEFAULT_RUNTIME_INTERVAL,
    runtime_lookback: str = DEFAULT_RUNTIME_LOOKBACK,
    max_learnings_in_context: int = 10,
    indicator_triggers: list[dict] | None = None,
    learning_consolidation_threshold: int = DEFAULT_LEARNING_CONSOLIDATION_THRESHOLD,
    consolidator_acpx_bin: str | None = None,
    consolidator_acpx_agent: str | None = None,
    consolidator_model: str | None = None,
    consolidator_timeout_s: int = consolidator.DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    decision_timeout_s: int = 900,
    decision_batch_size: int = DEFAULT_DECISION_BATCH_SIZE,
    decision_batch_parallelism: int = DEFAULT_DECISION_BATCH_PARALLELISM,
    commission_model: CommissionModel | None = None,
    agent_tools_enabled: bool = False,
) -> dict:
    """Exécute UN cycle. Retourne un rapport structuré (machine-readable)."""
    now = now or datetime.now(timezone.utc)
    universe_cfg = _load_yaml(ROOT / "config" / "universe.yaml")
    risk_cfg = _load_yaml(ROOT / "config" / "risk.yaml")
    regime_path = ROOT / "config" / "regime.yaml"
    regime_cfg = _load_yaml(regime_path) if regime_path.exists() else {}
    regime_cfg = regime_cfg or {}
    attribution_since = regime_cfg.get("attribution_since")
    attribution_since = None if attribution_since is None else str(attribution_since)

    symbols: list[str] = universe_cfg["symbols"]
    symbols_to_decide = symbols
    if symbols_filter is not None:
        wanted = set(symbols_filter)
        symbols_to_decide = [symbol for symbol in symbols if symbol in wanted]
    from .portfolio_config import load_starting_cash

    starting_equity = load_starting_cash(ROOT / "config")
    indicator_triggers = indicator_triggers or []
    triggers_by_symbol: dict[str, list[dict]] = {}
    for trigger in indicator_triggers:
        triggers_by_symbol.setdefault(str(trigger.get("symbol")), []).append(trigger)
    _log_cycle_progress(
        "[cycle] start dry_run=%s due=%d max_model_calls=%d",
        dry_run,
        len(symbols_to_decide),
        max_model_calls_per_cycle,
    )
    _write_status(
        "cycle_started",
        current_symbol=None,
        symbols_due=symbols_to_decide,
        symbols_total=len(symbols_to_decide),
        decisions_done=0,
        dry_run=dry_run,
        model_calls_used=0,
        max_model_calls_per_cycle=max_model_calls_per_cycle,
    )
    _append_event("cycle_started", symbols_due=symbols_to_decide, dry_run=dry_run)

    broker = SimBroker(
        STATE_DIR / "broker.json",
        starting_cash=starting_equity,
        commission_model=commission_model,
    )
    plan_store = TradePlanStore(STATE_DIR / "trade_plans.json")
    gate = RiskGate(RiskLimits.from_dict(risk_cfg))
    # Paper/exploration : si False, une ouverture SANS hard_stop n'est plus rejetée
    # (stop optionnel, position bornée par les seuls fusibles notionnels). Défaut
    # True = guardrail D6 préservé (live-safe). Voir spec exploration-basse-confiance.
    require_hard_stop = bool(risk_cfg.get("require_hard_stop", True))
    gate.start_cycle()
    mem = memory_mod.Memory(ROOT / "mandate" / "mandate.md", ROOT / "mandate" / "memory.md")
    learnings_store = memory_mod.LearningsStore(
        STATE_DIR / "learnings.jsonl",
        max_entries=consolidator.DEFAULT_RAW_MAX_ENTRIES,
    )
    consolidated_learnings_store = consolidator.ConsolidatedLearningsStore(
        STATE_DIR / "learnings_consolidated.json"
    )
    decision_ledger_store = decision_ledger.DecisionLedgerStore(
        STATE_DIR / decision_ledger.DEFAULT_LEDGER_FILENAME
    )

    # Store SQLite de recall des learnings (dérivé reconstructible, paresseux).
    # Si learnings.db est absent, provider=None → l'outil répond "unavailable".
    # La création du .db reste le job du script d'ingestion (Task 7).
    _learnings_db_path = STATE_DIR / "learnings.db"
    _recall_store: recall_store_mod.LearningsStore | None = None
    global _RECALL_STORE_FAILED
    if _learnings_db_path.exists() and not _RECALL_STORE_FAILED:
        try:
            _recall_store = recall_store_mod.LearningsStore(str(_learnings_db_path))
        except (sqlite3.Error, OSError) as _store_exc:
            _RECALL_STORE_FAILED = True
            log.warning(
                "learnings_recall: échec ouverture db (échec définitif, outil unavailable) — %s",
                _store_exc,
            )
    _recall_provider: Callable[[dict], dict] | None = (
        _build_recall_provider(_recall_store, now) if _recall_store is not None else None
    )

    if _kill_switch_active():
        _log_cycle_progress("[cycle] halted kill_switch")
        report = {
            "ts": now.isoformat(),
            "halted": "kill_switch",
            "decisions": [],
            "symbols_due": symbols_to_decide,
            "portfolio": None,
            "prices": {},
        }
        _write_current_report(report)
        _write_status("halted", current_symbol=None, halted="kill_switch")
        return report

    # Données marché : barres récentes par symbole. Les indicateurs gouvernés sont
    # calculés par `trader.features`, pas mentalement par le LLM.
    bars_by_symbol: dict[str, list] = {}
    prices: dict[str, float] = {}
    stale_market_data: dict[str, dict] = {}
    data_age_by_symbol: dict[str, float] = {}
    # F5 : capturé immédiatement après le fetch runtime décisionnel, avant tout
    # fetch secondaire (daily, watches) qui pourrait écraser last_source().
    runtime_data_source_by_sym: dict[str, str | None] = {}
    freshness_max_age = max(
        max_market_data_age_minutes,
        market.freshness_budget_minutes(runtime_interval),
    )
    _log_cycle_progress("[market] loading bars symbols=%d interval=%s", len(symbols), runtime_interval)
    for sym in symbols:
        try:
            bars = data_source.get_bars(sym, lookback=runtime_lookback, interval=runtime_interval)
        except market.MarketError as e:
            if _is_connection_market_error(e):
                raise
            log.warning("données indisponibles %s: %s", sym, e.code)
            continue
        if not bars:  # défensif : get_bars lève normalement sur vide
            log.warning("données vides %s", sym)
            continue
        bars_by_symbol[sym] = bars
        prices[sym] = bars[-1].close
        # F5 : capturer last_source ici, avant tout fetch secondaire (daily, watches).
        runtime_data_source_by_sym[sym] = getattr(data_source, "last_source", lambda _: None)(sym)
        # La fraîcheur EST le garde « marché live » : une dernière barre trop
        # vieille / imparsable => stale => exclue du tradable (pas de fill sur
        # données mortes hors-séance ou gelées).
        freshness = market.assess_freshness(bars, now=now, max_age_minutes=freshness_max_age)
        if freshness.age_minutes is not None:
            data_age_by_symbol[sym] = freshness.age_minutes
        if not freshness.fresh:
            stale_market_data[sym] = {
                "last_bar_ts": str(bars[-1].ts),
                "stale_reason": freshness.reason,
                "data_age_minutes": (
                    None if freshness.age_minutes is None else round(freshness.age_minutes, 4)
                ),
            }
    _log_cycle_progress(
        "[market] loaded ok=%d missing=%d",
        len(prices),
        max(0, len(symbols) - len(prices)),
    )
    if stale_market_data:
        _log_cycle_progress("[market] stale symbols=%s", sorted(stale_market_data))

    # Taux FX du cycle — chargés une fois par cycle après la boucle prix.
    # Fail-safe : si fx.yaml absent (tests, env simplifié), tous les symboles
    # tombent sur le fallback USD=1.0. Le fetch utilise la même source que les
    # barres (data_source), pour la cohérence temporelle du cycle.
    _fx_cfg: dict = {}
    _fx_yaml_path = ROOT / "config" / "fx.yaml"
    if _fx_yaml_path.exists():
        try:
            _fx_cfg = fx_rates.load_fx_config(_fx_yaml_path)
        except Exception as _fx_cfg_exc:  # noqa: BLE001 — fx.yaml mal formé : dégrade en USD
            log.warning("fx.yaml illisible (%s), dégradation USD fallback", _fx_cfg_exc)

    def _fx_fetch(yahoo_symbol: str) -> float | None:
        try:
            bars = data_source.get_bars(yahoo_symbol, lookback="2d", interval="1d")
            return bars[-1].close if bars else None
        except Exception:  # noqa: BLE001 — le fetch FX ne doit jamais casser le cycle
            return None

    try:
        fx_rate_by_ccy: dict[str, float] = fx_rates.rates_for_symbols(
            prices.keys(), fetcher=_fx_fetch, config=_fx_cfg
        )
    except Exception as _fx_rates_exc:  # noqa: BLE001 — devise non configurée → USD fallback
        log.warning("fx rates fetch échoué (%s), dégradation USD fallback", _fx_rates_exc)
        fx_rate_by_ccy = {fx.BASE_CCY: 1.0}

    def _rate(sym: str) -> float:
        """Retourne le taux USD/ccy pour le symbole ; USD=1.0 si inconnu."""
        return fx_rate_by_ccy.get(fx.currency_for(sym), 1.0)

    # Reset streak pour tous les symboles frais (data fraîche reçue)
    if sched is not None:
        for sym in symbols:
            if sym in prices and sym not in stale_market_data:
                current_streak = sched.get_stale_streak(sym)
                if current_streak > 0:
                    sched.reset_stale_streak(sym)

    tradable_prices = {symbol: price for symbol, price in prices.items() if symbol not in stale_market_data}
    tradable_symbols = [symbol for symbol in symbols if symbol not in stale_market_data]
    tradable_bars_by_symbol = {
        symbol: bars
        for symbol, bars in bars_by_symbol.items()
        if symbol not in stale_market_data
    }

    # §13.3 — le daily est fetché pour TOUT l'univers (pas seulement tradable) :
    # un symbole runtime-stale doit pouvoir être analysé/planifié sur son daily.
    # La fraîcheur daily est jugée par séance complétée (assess_daily_freshness),
    # indépendamment du verrou runtime.
    daily_bars_by_symbol: dict[str, list] = {}
    for sym in symbols:
        try:
            daily_bars = data_source.get_bars(
                sym,
                lookback=COCKPIT_DAILY_LOOKBACK,
                interval=COCKPIT_DAILY_INTERVAL,
            )
        except market.MarketError as exc:
            if _is_connection_market_error(exc):
                raise
            log.warning("daily data unavailable %s: %s", sym, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 - daily cockpit data is optional
            log.warning("daily data failed %s: %s", sym, exc)
            continue
        if not daily_bars:
            continue
        try:
            freshness = market.assess_daily_freshness(daily_bars, now=now, symbol=sym)
        except Exception as exc:  # noqa: BLE001 - daily cockpit data is optional
            log.warning("daily freshness failed %s: %s", sym, exc)
            continue
        if not freshness.fresh:
            log.warning("daily data stale %s: %s", sym, freshness.reason)
            continue
        daily_bars_by_symbol[sym] = daily_bars

    # §13.2 — classification execution/planning par symbole, injectée au contexte LLM.
    execution_eligibility = _build_execution_eligibility(
        symbols,
        stale_market_data=stale_market_data,
        prices=prices,
        daily_bars_by_symbol=daily_bars_by_symbol,
        data_age_by_symbol=data_age_by_symbol,
        now=now,
        runtime_interval=runtime_interval,
    )

    exit_bars_by_symbol, exit_intervals_by_symbol = _fetch_5m_bars_for_open_plans(
        plan_store=plan_store,
        data_source=data_source,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
        tradable_prices=tradable_prices,
        now=now,
    )
    planned_exits = _apply_planned_exits(
        broker=broker,
        plan_store=plan_store,
        prices=tradable_prices,
        bars_by_symbol=exit_bars_by_symbol,
        bars_intervals_by_symbol=exit_intervals_by_symbol,
        valuation_prices=prices,
        now=now,
        dry_run=dry_run,
        starting_equity=starting_equity,
        execution_eligibility=execution_eligibility,
        rate_fn=_rate,
    )
    if planned_exits:
        _log_cycle_progress("[exit] planned exits=%d", len(planned_exits))

    exit_watch_triggers = _scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol=tradable_bars_by_symbol,
        symbols=tradable_symbols,
        now=now,
        dry_run=dry_run,
        bars_interval=runtime_interval,
        data_source=data_source,
    )
    if exit_watch_triggers:
        indicator_triggers = [*indicator_triggers, *exit_watch_triggers]
        for trigger in exit_watch_triggers:
            symbol = str(trigger.get("symbol"))
            triggers_by_symbol.setdefault(symbol, []).append(trigger)
            if symbol in symbols and symbol not in symbols_to_decide:
                symbols_to_decide.append(symbol)

    snap = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity, fx_rate_of=_rate)
    gross = sum(abs(h.market_value) for h in snap.holdings)
    portfolio_fee_estimator = _build_portfolio_fee_estimator(commission_model)

    active_families = family_regime.families_for_universe(tradable_symbols)
    # Coût de transaction injecté dans le cockpit : break-even (bps) + coût
    # aller-retour par symbole, pour un ordre de référence = plafond par ordre du
    # risk gate. L'agent compare l'amplitude attendue au break-even avant de
    # scalper. Seulement si un modèle de frais est actif (sinon colonnes absentes).
    cockpit_fee_estimator = None
    fee_ref_notional = None
    if commission_model is not None:
        fee_ref_notional = float(risk_cfg.get("max_order_value", 10_000.0))

        def cockpit_fee_estimator(symbol: str, price: float | None) -> dict | None:
            return round_trip_cost(commission_model, symbol, price, fee_ref_notional)

    # Contexte cross-asset partagé : tout l'univers est visible à chaque décision
    # (l'edge de l'agent = relations entre symboles, pas un graphe isolé).
    cockpit = build_market_cockpit(
        tradable_bars_by_symbol,
        symbols=tradable_symbols,
        prices=tradable_prices,
        window=48,
        daily_bars_by_symbol=daily_bars_by_symbol,
        fee_estimator=cockpit_fee_estimator,
        fee_ref_notional=fee_ref_notional,
        fx_rate_by_ccy=fx_rate_by_ccy,
        equity_usd=float(snap.equity),
        risk_pct=float(gate.limits.max_risk_per_trade_pct),
        max_order_value=float(gate.limits.max_order_value),
    )
    excluded_attribution_symbols = tuple(regime_cfg.get("exclude_symbols") or [])
    attribution_payload = attribution.compute_attribution(
        STATE_DIR,
        since=attribution_since,
        exclude_symbols=excluded_attribution_symbols,
        min_entry_confidence=_attribution_min_entry_confidence(
            risk_cfg, confidence_gate_enabled=gate.limits.confidence_gate_enabled
        ),
    )
    meta_performance_payload = meta_performance.compute_meta_performance(STATE_DIR)
    base_context = {
        "now": now.isoformat(),
        "portfolio": snap.as_context(fee_estimator=portfolio_fee_estimator),
        "risk_limits": risk_cfg,
        "risk_capacity": _risk_capacity_context(
            symbols=[symbol for symbol in symbols if symbol in prices],
            prices=prices,
            broker=broker,
            gross_exposure=gross,
            limits=gate.limits,
            equity=snap.equity,
            rate_of=_rate,
        ),
        "semantic": {
            "requestable_indicator_ids": DEFAULT_INDICATORS,
        },
        "cockpit": cockpit,
        "stale_market_data": stale_market_data,
        # KPI live injectés pour que l'agent décideur pilote sa performance.
        "kpis": stats.compute_live_kpis(STATE_DIR),
        # Attribution décision->résultat : P&L réalisé par trade, calibration de la
        # confidence et coût par raison de sortie. Le signal qui dit à l'agent si
        # ses choix (surtout ses calls confiants) gagnent vraiment.
        "attribution": attribution_payload,
        # Stats ex-post des décisions agent (dont HOLD missed), groupées par
        # action/reason_code. Descriptif uniquement : l'agent garde le jugement.
        "meta_performance": meta_performance_payload,
        # Boucle de feedback (D6) : guardrails humains nommés à part ; dès qu'un
        # consolidé existe, les bruts ne sont plus réinjectés (anti auto-renforcement).
        "learnings": consolidator.build_context_learnings(
            consolidated_learnings_store.read(),
            raw_recent=learnings_store.recent(limit=max_learnings_in_context),
            guardrails=consolidator.load_guardrails(ROOT / "mandate" / "guardrails.json"),
        ),
        # Biais de régime cross-asset par famille thématique (D2) : le code
        # calcule la synthèse directionnelle, l'agent juge l'opportunité.
        "regime_families": family_regime.compute_family_bias(
            {
                sym: family_regime.momentum_from_bars(bars)
                for sym, bars in daily_bars_by_symbol.items()
            },
            active_families,
        ),
    }

    # Feedback léger du cycle précédent : si des ouvertures ont été recalées faute
    # de marge gross, on le signale à l'agent (marge partagée entre tous les
    # symboles) — présent seulement si non vide.
    _gross_feedback = _LAST_GROSS_REJECTIONS.get(str(STATE_DIR))
    if _gross_feedback:
        base_context["gross_budget_feedback"] = _gross_feedback

    report: dict = {
        "ts": now.isoformat(),
        "dry_run": dry_run,
        "code_version": code_version.current_code_version(ROOT),
        "symbols_due": symbols_to_decide,
        "planned_exits": planned_exits,
        "exit_watch_triggers": exit_watch_triggers,
        "indicator_triggers": indicator_triggers,
        "decisions": [],
        "portfolio": snap.as_context(fee_estimator=portfolio_fee_estimator),
        "prices": {s: round(p, 4) for s, p in prices.items()},
        "stale_market_data": stale_market_data,
        "fx_rates": fx_rate_by_ccy,
        "model_calls_used": 0,
    }

    def refresh_report_portfolio() -> None:
        latest = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity, fx_rate_of=_rate)
        report["portfolio"] = latest.as_context(fee_estimator=portfolio_fee_estimator)

    _write_current_report(report)
    mandate_txt, memory_txt = mem.read_mandate(), mem.read_memory()
    model_calls_used = 0

    # Calendrier macro : calculé UNE fois par cycle (pas par symbole) — best-effort.
    # Attribution-first : n'entre PAS dans le contexte LLM (même mécanique que news).
    _calendar = macro_calendar.load_calendar(STATE_DIR / "macro_calendar.json")
    _cycle_macro_next = macro_calendar.macro_next(now, _calendar)

    def record_decision(decision_entry: dict) -> None:
        # Phase attribution : snapshot news loggé sur CHAQUE décision, tous chemins
        # confondus (y compris quiet_gate / stale_market_data). setdefault : n'écrase
        # pas si déjà fourni (ex. enrichissement préalable dans le chemin LLM normal).
        decision_entry.setdefault("news", news_feed.news_snapshot(decision_entry["symbol"], now=now))
        # macro_next ajouté au payload news de chaque décision (UNE fois par cycle,
        # pas par symbole). Copie défensive du dict pour ne pas muter le cache news.
        _news = decision_entry.get("news")
        if isinstance(_news, dict) and "macro_next" not in _news:
            decision_entry["news"] = {**_news, "macro_next": _cycle_macro_next}
        # Boucle de feedback : l'agent possède ses learnings ; l'infra les persiste
        # (machine-owned, borné) AVEC le résultat de la décision (executed/reason/
        # dry_run) pour qu'on puisse juger si l'agent fait les bons choix, puis les
        # réinjecte au prochain réveil.
        note = _merge_gate_feedback(
            decision_entry.get("reason"),
            decision_entry.get("context"),
            decision_entry.get("learning"),
        )
        if note:
            decision_entry["learning_recorded"] = learnings_store.append(
                symbol=decision_entry["symbol"],
                note=note,
                now=now,
                action=decision_entry.get("action"),
                intent=decision_entry.get("intent"),
                executed=decision_entry.get("executed"),
                reason=decision_entry.get("reason"),
                dry_run=dry_run,
            )
        sequence = len(report["decisions"])
        report["decisions"].append(decision_entry)
        report["model_calls_used"] = model_calls_used
        refresh_report_portfolio()
        decision_ledger_store.append(
            decision_ledger.build_decision_row(
                report,
                decision_entry,
                sequence=sequence,
                source="armed_plan" if decision_entry.get("armed_plan_id") else "daemon",
            )
        )
        # Trace d'injection recall_learnings (design §4.4).
        # Appelé après la persistance ledger : le decision_id est construit au
        # même instant (cycle_ts|sequence|symbol), invariant lecture seule de l'outil.
        if _recall_store is not None:
            _cycle_ts = str(report.get("ts") or "")
            _sym = str(decision_entry.get("symbol") or "")
            if _cycle_ts and _sym:
                _did = decision_ledger._decision_id(_cycle_ts, sequence, _sym)
                for _tc in decision_entry.get("tool_calls") or []:
                    if _tc.get("tool") == "recall_learnings" and _tc.get("outcome") == "ok":
                        _note_ids = [
                            _nid for _nid in (_tc.get("detail") or {}).get("note_ids", [])
                            if isinstance(_nid, int)
                        ]
                        if _note_ids:
                            try:
                                _recall_store.record_recall(decision_id=_did, note_ids=_note_ids)
                            except Exception as _exc:  # noqa: BLE001
                                log.warning("record_recall failed: %s", _exc)
        _write_current_report(report)
        _write_status(
            "decision_recorded",
            current_symbol=decision_entry["symbol"],
            decisions_done=len(report["decisions"]),
            symbols_total=len(symbols_to_decide),
            last_decision=decision_entry,
            model_calls_used=model_calls_used,
            max_model_calls_per_cycle=max_model_calls_per_cycle,
        )
        _append_event(
            "decision_recorded",
            symbol=decision_entry["symbol"],
            action=decision_entry.get("action"),
            reason=decision_entry.get("reason"),
            executed=decision_entry.get("executed"),
        )

    if sched is not None:
        _ensure_default_wake(sched, now=now, default_wake_minutes=default_wake_minutes)

    # §13.4 — décidables : runtime frais avec prix (exécution possible) OU planning
    # autorisé (daily valide → analyse swing même runtime stale) OU position ouverte
    # (toujours relire la thèse). Les stale-analysables passent ensuite par le MÊME
    # gate de pertinence (anti-déluge) ; seuls les stale non-analysables retombent
    # sur le HOLD synthétique + backoff plus bas.
    held_symbols = {h.symbol for h in snap.holdings if h.quantity}

    def _analysis_eligible(sym: str) -> bool:
        planning = (execution_eligibility.get(sym) or {}).get("planning") or {}
        return bool(planning.get("enabled")) or sym in held_symbols

    # Un prix runtime (même vieux) est REQUIS pour entrer dans le batch : sans prix,
    # la boucle finale ne peut pas traiter le symbole (il retombe sur le HOLD stale).
    # Cela évite une décision LLM fantôme jetée plus bas (gap silencieux).
    decidable = [
        s
        for s in symbols_to_decide
        if s in prices and (s not in stale_market_data or _analysis_eligible(s))
    ]

    # Plans armés (D7 étage B) : un trigger EXECUTE_ORDER s'exécute SANS appel
    # LLM — le scénario a été validé à l'armement, le gate de risque déterministe
    # reste le fusible à l'exécution. Annulation si le prix au déclenchement a
    # déjà franchi le hard_stop (position instantanément stoppable) ou si stale.
    armed_decisions: dict[str, codex_client.Decision] = {}
    armed_plan_ids: dict[str, str] = {}
    armed_plan_orders: dict[str, dict] = {}
    stale_armed_plans: dict[str, dict] = {}
    armed_reference_volatilities: dict[str, float | None] = {}
    # Conflit : plusieurs scénarios armés du MÊME symbole déclenchés au même
    # cycle = ambiguïté — on n'exécute pas arbitrairement, le planificateur
    # arbitre (les triggers annotés restent dans son contexte).
    _armed_by_symbol: dict[str, list[dict]] = {}
    for trigger in indicator_triggers:
        if str(trigger.get("on_trigger")) == "EXECUTE_ORDER" and isinstance(trigger.get("order"), dict):
            _armed_by_symbol.setdefault(str(trigger.get("symbol")), []).append(trigger)
    _armed_conflicts = {sym for sym, items in _armed_by_symbol.items() if len(items) > 1}
    for sym in _armed_conflicts:
        plan_ids = [str(t.get("watch_id") or "") for t in _armed_by_symbol[sym]]
        _log_cycle_progress("[armed_plan] %s conflit (%d plans déclenchés) — réveil planificateur", sym, len(plan_ids))
        _append_event("armed_plan_conflict", symbol=sym, plan_ids=plan_ids)
        for t in _armed_by_symbol[sym]:
            t["armed_conflict"] = True
    _positions_snapshot = broker.positions() if _armed_by_symbol else {}
    for sym, sym_triggers in _armed_by_symbol.items():
        if sym in _armed_conflicts or sym not in symbols_to_decide:
            continue
        trigger = sym_triggers[0]
        order = trigger["order"]
        plan_id = str(trigger.get("watch_id") or "")
        position = _positions_snapshot.get(sym)
        cancel_reason = None
        if sym in stale_market_data or sym not in prices:
            cancel_reason = "armed_plan_cancelled:stale"
        elif position is not None and position.quantity:
            # position déjà ouverte : un plan d'OUVERTURE armé avant ne doit pas
            # s'empiler mécaniquement — le planificateur re-décide (review Codex)
            cancel_reason = "armed_plan_cancelled:position_exists"
        else:
            intent_side = {"OPEN_LONG": "LONG", "OPEN_SHORT": "SHORT"}.get(str(order.get("intent")))
            if intent_side is None:
                cancel_reason = "armed_plan_cancelled:exit_unresolved:side_unsupported"
            else:
                ref_vol = _reference_volatility_for_symbol(
                    sym,
                    entry_price=prices[sym],
                    cockpit=cockpit,
                    tradable_bars_by_symbol=tradable_bars_by_symbol,
                )
                try:
                    resolved_exit_plan, trace = resolve_exit_plan(
                        order.get("exit_plan"),
                        entry_price=prices[sym],
                        side=intent_side,  # type: ignore[arg-type]
                        reference_volatility=ref_vol,
                        bars=tradable_bars_by_symbol.get(sym),
                    )
                except InvalidExitPlanError as exc:
                    cancel_reason = f"armed_plan_cancelled:exit_unresolved:{exc}"
                else:
                    order = {**order, "exit_plan": resolved_exit_plan}
                    trigger["order"] = order
                    armed_reference_volatilities[sym] = ref_vol
                    _append_event(
                        "armed_plan_resolved",
                        symbol=sym,
                        plan_id=plan_id,
                        trace=trace,
                        exit_plan=order.get("exit_plan"),
                    )
                    if not armed_order_price_coherent(order, price=prices[sym]):
                        cancel_reason = "armed_plan_cancelled:stop_incoherent"
        if cancel_reason is not None:
            # Scénario invalidé = événement : on n'exécute pas en aveugle, on
            # réveille le planificateur AVEC le contexte (trigger annoté), il
            # re-décide (re-armer autrement, ou laisser).
            _log_cycle_progress("[armed_plan] %s %s plan=%s — réveil planificateur", sym, cancel_reason, plan_id)
            _append_event(
                "armed_plan_cancelled",
                symbol=sym,
                plan_id=plan_id,
                reason=cancel_reason,
                exit_plan=order.get("exit_plan"),
            )
            trigger["armed_cancelled"] = cancel_reason
            if cancel_reason == "armed_plan_cancelled:stale":
                stale_armed_plans[sym] = {"id": plan_id, "order": dict(order)}
            continue
        armed_decisions[sym] = codex_client.Decision(
            symbol=sym,
            action=str(order["action"]),
            quantity=float(order["qty"]),
            confidence=float(order["confidence"]),
            rationale=f"armed_plan:{plan_id} — {order.get('rationale') or ''}".strip(" —"),
            intent=str(order["intent"]),
            exit_plan=order.get("exit_plan"),
            decision_reason_code="ARMED_PLAN",
        )
        armed_plan_ids[sym] = plan_id
        armed_plan_orders[sym] = dict(order)
        _log_cycle_progress("[armed_plan] %s déclenché plan=%s — exécution sans LLM", sym, plan_id)
    # les symboles armés ont déjà leur décision : pas d'appel LLM, pas de
    # relevance_gate. Le RiskGate déterministe reste appliqué plus bas.
    decidable = [s for s in decidable if s not in armed_decisions]

    # Gate de pertinence (D7 étage A) : ne soumettre au LLM que les réveils
    # demandés par l'agent, les événements, ou la revue périodique garantie.
    # Le polling par défaut sur symbole calme ne consomme pas d'appel modèle.
    activity = relevance_gate.cockpit_activity(cockpit)
    strong_families = {
        fam
        for fam, bias in base_context["regime_families"].items()
        if (bias.get("frac") or 0.0) >= 0.70
    }
    family_of = {
        member: fam for fam, members in active_families.items() for member in members
    }
    agent_wakes = sched.symbols_with_wake() if sched is not None else set()
    gated_symbols: list[str] = []
    kept: list[str] = []
    for sym in decidable:
        last_seen = _LAST_LLM_AT.get((str(STATE_DIR), sym))
        hours = None if last_seen is None else (now - last_seen).total_seconds() / 3600.0
        act = activity.get(sym) or {}
        needed, gate_reason = relevance_gate.symbol_needs_llm(
            agent_requested_wake=sym in agent_wakes,
            has_trigger=bool(triggers_by_symbol.get(sym)),
            has_position=sym in held_symbols,
            family_regime_strong=family_of.get(sym) in strong_families,
            stretched=act.get("stretched"),
            sig=act.get("sig"),
            hours_since_last_llm=hours,
        )
        if needed:
            kept.append(sym)
        else:
            gated_symbols.append(sym)
    if gated_symbols:
        _log_cycle_progress("[gate] quiet symbols=%s (pas d'appel LLM)", gated_symbols)
        for sym in gated_symbols:
            # Pas de wake par symbole : le gated retombe sur le polling par
            # défaut (un wake posé ici se ferait passer pour un wake agent).
            record_decision(
                {
                    "symbol": sym,
                    "action": "HOLD",
                    "qty": 0.0,
                    "confidence": 0.0,
                    "rationale": "quiet_gate",
                    "next_wake_in_minutes": None,
                    "intent": "HOLD",
                    "trade_plan_created": False,
                    "executed": False,
                    "reason": "quiet_gate",
                    "decision_reason_code": "NO_EDGE",
                    "decision_source": "infra",
                    "model_called": False,
                    "data_source": runtime_data_source_by_sym.get(sym),
                }
            )
    decidable = kept

    _log_cycle_progress("[batch] deciding symbols=%d/%d", len(decidable), len(symbols_to_decide))
    _write_status(
        "deciding_batch",
        current_symbol=None,
        decisions_done=0,
        symbols_total=len(symbols_to_decide),
        batch_size=len(decidable),
    )
    # §13.4 — barres pour le batch : runtime des non-stale + DAILY des stale-analysables,
    # sinon le LLM analyserait un symbole stale sans aucune barre exploitable.
    analysis_bars_by_symbol = dict(tradable_bars_by_symbol)
    for sym in decidable:
        if sym not in analysis_bars_by_symbol and sym in daily_bars_by_symbol:
            analysis_bars_by_symbol[sym] = daily_bars_by_symbol[sym]
    # §13.4 — les symboles dont resolve_indicator_requests peut servir un REQUEST_CONTEXT
    # = ceux qui ont des barres (runtime non-stale + daily des stale-analysables). Sans
    # ça, un stale qui demande du contexte sur lui-même reçoit un research vide.
    analysis_symbols = sorted(analysis_bars_by_symbol)
    decisions_by_symbol, model_calls_used = _batch_decide(
        decidable=decidable,
        mandate=mandate_txt,
        memory=memory_txt,
        shared_context=base_context,
        triggers_by_symbol=triggers_by_symbol,
        tradable_bars_by_symbol=analysis_bars_by_symbol,
        tradable_symbols=analysis_symbols,
        runtime_interval=runtime_interval,
        runtime_lookback=runtime_lookback,
        max_context_requests_per_symbol=max_context_requests_per_symbol,
        max_indicators_per_request=max_indicators_per_request,
        max_model_calls=max_model_calls_per_cycle,
        now=now,
        data_age_by_symbol=data_age_by_symbol,
        sched=sched,
        last_review_by_symbol=_last_review_by_symbol(plan_store, decidable),
        market_context_by_symbol=execution_eligibility,
        decision_timeout_s=decision_timeout_s,
        decision_batch_size=decision_batch_size,
        decision_batch_parallelism=decision_batch_parallelism,
        agent_tools_enabled=agent_tools_enabled,
        learnings_recall_provider=_recall_provider,
    )
    # revue effective seulement si le modèle a réellement statué (review Codex :
    # un échec/budget à 0 ne doit pas compter comme revue périodique)
    for sym in decidable:
        decision = decisions_by_symbol.get(sym)
        if decision is not None and _counts_as_llm_review(decision):
            _LAST_LLM_AT[(str(STATE_DIR), sym)] = now
    if armed_decisions:
        decisions_by_symbol = {**decisions_by_symbol, **armed_decisions}
    _log_cycle_progress("[batch] decided=%d model_calls=%d", len(decisions_by_symbol), model_calls_used)

    # Admission gross équitable sans clamp (spec 2026-06-30) : on réordonne
    # l'exécution — réducteurs d'abord (ils libèrent de la marge), puis ouvertures
    # par conviction décroissante — pour que le RiskGate arbitre au mérite plutôt
    # que selon l'ordre de liste. Aucune qty n'est rognée : le gate admet/rejette à
    # pleine taille ; quand la marge est épuisée ce sont les plus basse-conviction
    # qui sautent (déterministe), pas les dernières de la liste.
    def _gross_priority_item(sym: str) -> PriorityItem:
        decided = decisions_by_symbol.get(sym)
        return PriorityItem(
            symbol=sym,
            intent=(decided.intent if decided is not None else "HOLD"),
            confidence=float((decided.confidence if decided is not None else 0.0) or 0.0),
        )

    symbols_to_decide = gross_execution_order([_gross_priority_item(sym) for sym in symbols_to_decide])

    gated_set = set(gated_symbols)
    for index, sym in enumerate(symbols_to_decide, start=1):
        if sym in gated_set:
            # Déjà tracé quiet_gate — ne pas générer un second HOLD
            # "no_decision_in_batch" (doublon ledger, faux HOLD agent).
            # NB : un plan armé annulé n'est PAS ici — il passe au LLM (voulu).
            continue
        stale_data = stale_market_data.get(sym)
        # §13.4 — un stale-analysable (planning.enabled / position) est passé au LLM
        # via `decidable` : il ne retombe PLUS sur le HOLD synthétique + backoff. Seuls
        # les stale NON décidables (rien d'exploitable) gardent le HOLD infra ci-dessous.
        if stale_data is not None and sym not in decidable:
            streak = sched.get_stale_streak(sym) if sched is not None else 0
            wake_minutes = _stale_backoff_wake_minutes(streak, default_wake_minutes=default_wake_minutes)
            # Ne jamais dormir au-delà de la prochaine ouverture : on raccourcit le
            # backoff pour être réveillé pile avant la cloche DU marché du symbole
            # (TWSE/Euronext/XETRA/US — anti-rater-l'open).
            wake_minutes = market.clamp_wake_to_session_open(wake_minutes, now=now, symbol=sym)
            new_streak = streak + 1
            if sched is not None:
                sched.set_stale_streak(sym, new_streak)
                sched.set_symbol_next_wake_in(sym, minutes=wake_minutes, now=now)

            is_first_stale = streak == 0  # transition fresh→stale : enregistrer la décision
            stale_armed_plan = stale_armed_plans.get(sym)
            should_record_stale = is_first_stale or stale_armed_plan is not None
            if should_record_stale:
                _log_cycle_progress(
                    "[decision %d/%d] %s stale_market_data (streak=%d) reason=%s age=%s wake=%.0fmin",
                    index,
                    len(symbols_to_decide),
                    sym,
                    new_streak,
                    stale_data.get("stale_reason"),
                    stale_data.get("data_age_minutes"),
                    wake_minutes,
                )
                record_decision(
                    {
                        "symbol": sym,
                        **(
                            {
                                "armed_plan_id": stale_armed_plan["id"],
                                "armed_plan_order": stale_armed_plan["order"],
                            }
                            if stale_armed_plan is not None
                            else {}
                        ),
                        "action": "HOLD",
                        "qty": 0.0,
                        "confidence": 0.0,
                        "rationale": "stale_market_data",
                        "next_wake_in_minutes": wake_minutes,
                        "intent": "HOLD",
                        "trade_plan_created": False,
                        "executed": False,
                        "reason": "stale_market_data",
                        "decision_reason_code": "DATA_STALE",
                        "decision_source": "infra",
                        "model_called": False,
                        "stale_streak": new_streak,
                        "data_source": runtime_data_source_by_sym.get(sym),
                        **stale_data,
                    }
                )
            else:
                _log_cycle_progress(
                    "[decision %d/%d] %s stale_backoff streak=%d wake=%.0fmin reason=%s",
                    index,
                    len(symbols_to_decide),
                    sym,
                    new_streak,
                    wake_minutes,
                    stale_data.get("stale_reason"),
                )
                _append_event(
                    "stale_backoff",
                    symbol=sym,
                    streak=new_streak,
                    next_wake_minutes=wake_minutes,
                    stale_reason=stale_data.get("stale_reason"),
                    data_age_minutes=stale_data.get("data_age_minutes"),
                )
            continue
        if sym not in prices:
            _log_cycle_progress("[decision %d/%d] %s skipped no_price", index, len(symbols_to_decide), sym)
            continue

        decision = decisions_by_symbol.get(sym) or codex_client.Decision.hold(sym, "no_decision_in_batch")
        # Trades (action != HOLD) à l'INFO ; HOLD en DEBUG pour désengorger la
        # console (le compte reste visible via [batch] decided / [cycle] completed).
        _res_log = log.debug if decision.action == "HOLD" else log.info
        _res_log(
            "[decision %d/%d] %s result action=%s qty=%s intent=%s wake=%s confidence=%.2f provider=%s model=%s fallback=%s",
            index,
            len(symbols_to_decide),
            sym,
            decision.action,
            decision.quantity,
            decision.intent,
            decision.next_wake_in_minutes,
            decision.confidence,
            decision.llm_provider,
            decision.llm_model,
            decision.llm_fallback_reason,
        )

        next_wake_in_minutes = None
        if decision.next_wake_in_minutes is not None:
            next_wake_in_minutes = _bounded_wake_minutes(
                decision.next_wake_in_minutes,
                minimum=min_wake_minutes,
                maximum=max_wake_minutes,
            )

        effective_quantity = abs(decision.quantity)

        decision_source = (
            "armed_plan"
            if sym in armed_plan_ids
            else ("llm" if decision.llm_provider or decision.llm_model else "infra")
        )
        armed_plan_order = armed_plan_orders.get(sym)
        entry = {"symbol": sym, "action": decision.action, "qty": effective_quantity,
                 **({"armed_plan_id": armed_plan_ids[sym]} if sym in armed_plan_ids else {}),
                 **({"armed_plan_order": armed_plan_order} if armed_plan_order is not None else {}),
                 "confidence": decision.confidence, "rationale": decision.rationale,
                 "next_wake_in_minutes": next_wake_in_minutes,
                 "next_wake_requested": decision.next_wake_in_minutes,
                 "context_request": decision.context_request,
                 "intent": decision.intent,
                 "decision_reason_code": decision.decision_reason_code,
                 "decision_source": decision_source,
                 "model_called": decision_source == "llm",
                 "llm_provider": decision.llm_provider,
                 "llm_model": decision.llm_model,
                 "llm_fallback_reason": decision.llm_fallback_reason,
                 "llm_error": decision.llm_error,
                 "learning": decision.learning,
                 "trade_plan_created": False,
                 "indicator_watch_created": False,
                 "indicator_watch_requested": bool(decision.indicator_watch),
                 "indicator_watch_rejections": [],
                 "data_source": runtime_data_source_by_sym.get(sym),
                 "tool_rounds": (decision.domain_tools or {}).get("tool_rounds"),
                 "tool_calls": (decision.domain_tools or {}).get("tool_calls")}
        if decision_source == "llm" and sym in held_symbols and _counts_as_llm_review(decision):
            _persist_last_llm_review(
                plan_store=plan_store,
                symbol=sym,
                now=now,
                decision=decision,
            )

        reference_volatility: float | None = None
        runtime_exit_plan = decision.exit_plan
        # Trace le plan de sortie BRUT (avant résolution) pour diagnostiquer les
        # rejets invalid_exit_plan:hard_stop_* sans fouiller decision_audit.json.
        # deepcopy : normalize_exit_plan/resolve peuvent muter la structure.
        entry["exit_plan"] = copy.deepcopy(decision.exit_plan) if decision.exit_plan else None
        pending_indicator_watch = None
        if decision.indicator_watch:
            indicator_watch_result = build_indicator_watch(decision.indicator_watch, owner_symbol=sym, now=now)
            entry["indicator_watch_rejections"] = indicator_watch_result.rejections
            if indicator_watch_result.rejections:
                log.warning(
                    "indicator_watch rejets %s: %d conditions %s",
                    sym,
                    len(indicator_watch_result.rejections),
                    [r["reason"] for r in indicator_watch_result.rejections],
                )
            if sched is not None:
                pending_indicator_watch = indicator_watch_result.watch

        def apply_decision_schedule() -> None:
            _apply_decision_schedule(
                sched=sched,
                sym=sym,
                now=now,
                next_wake_in_minutes=next_wake_in_minutes,
                cancel_watch_ids=decision.cancel_watch_ids,
                pending_indicator_watch=pending_indicator_watch,
                entry=entry,
            )

        def apply_default_schedule_after_blocked() -> None:
            if sched is not None:
                sched.clear_symbol_next_wake(sym)

        if decision.action == "HOLD" or effective_quantity == 0:
            # Pas de 2e ligne "hold" : la ligne result ci-dessus (DEBUG pour HOLD)
            # porte déjà l'action.
            apply_decision_schedule()
            hold_reason = "hold"
            if decision_source == "infra" and decision.rationale in _INFRA_HOLD_REASONS:
                hold_reason = decision.rationale
            record_decision({**entry, "executed": False, "reason": hold_reason})
            continue

        invalid_intent = _invalid_intent_reason(decision)
        if invalid_intent is not None:
            _log_cycle_progress("[decision %d/%d] %s blocked %s", index, len(symbols_to_decide), sym, invalid_intent)
            apply_default_schedule_after_blocked()
            record_decision({**entry, "executed": False, "reason": invalid_intent})
            continue

        # §13.5 — garde déterministe d'exécution : marché fermé / runtime stale / pas
        # de prix => aucun ordre ne part (le LLM a pu décider, l'infra ne fille pas une
        # exécution irréaliste). On APPLIQUE le scheduling non-exécutif du LLM
        # (next_wake + indicator_watch) comme pour un HOLD : la veille/le réveil sont
        # réellement conservés. Le RiskGate reste le fusible séparé sur le risque/montant.
        # Fail-closed pour les ouvertures (invariant §10) ; fail-open pour les sorties.
        execution_blocked = _execution_blocked_reason(
            execution_eligibility,
            sym,
            fail_closed=decision.intent in {"OPEN_LONG", "OPEN_SHORT", "REVERSE"},
        )
        if execution_blocked is not None:
            _log_cycle_progress(
                "[execution] %s ordre bloqué (%s) — watch/wake conservés", sym, execution_blocked
            )
            apply_decision_schedule()
            record_decision({**entry, "executed": False, "reason": execution_blocked})
            continue

        if runtime_exit_plan and decision.intent in {"OPEN_LONG", "OPEN_SHORT", "REVERSE"}:
            if sym in armed_reference_volatilities:
                reference_volatility = armed_reference_volatilities[sym]
            else:
                reference_volatility = _reference_volatility_for_symbol(
                    sym,
                    entry_price=prices[sym],
                    cockpit=cockpit,
                    tradable_bars_by_symbol=tradable_bars_by_symbol,
                )
            try:
                if decision.intent in {"OPEN_LONG", "OPEN_SHORT"} and sym not in armed_plan_ids:
                    intent_side = "LONG" if decision.intent == "OPEN_LONG" else "SHORT"
                    runtime_exit_plan, _trace = resolve_exit_plan(
                        runtime_exit_plan,
                        entry_price=prices[sym],
                        side=intent_side,
                        reference_volatility=reference_volatility,
                        bars=tradable_bars_by_symbol.get(sym),
                    )
                else:
                    validate_exit_plan(
                        runtime_exit_plan,
                        reference_volatility=reference_volatility,
                    )
            except InvalidExitPlanError as exc:
                _log_cycle_progress(
                    "[decision %d/%d] %s blocked invalid_exit_plan:%s",
                    index,
                    len(symbols_to_decide),
                    sym,
                    exc,
                )
                apply_default_schedule_after_blocked()
                record_decision({**entry, "executed": False, "reason": f"invalid_exit_plan:{exc}"})
                continue
            hard_stop_price = _hard_stop_price(runtime_exit_plan)
            if (
                decision.intent in {"OPEN_LONG", "OPEN_SHORT"}
                and hard_stop_price is not None
                and _hard_stop_wrong_side(decision.intent, prices[sym], hard_stop_price)
            ):
                _log_cycle_progress(
                    "[decision %d/%d] %s blocked invalid_exit_plan:hard_stop_wrong_side",
                    index,
                    len(symbols_to_decide),
                    sym,
                )
                apply_default_schedule_after_blocked()
                record_decision({**entry, "executed": False, "reason": "invalid_exit_plan:hard_stop_wrong_side"})
                continue

        pos = broker.positions().get(sym)
        clamped_quantity, exit_block_reason = _clamp_exit_quantity(
            intent=decision.intent,
            action=decision.action,
            quantity=effective_quantity,
            position_quantity=0.0 if pos is None else pos.quantity,
        )
        if exit_block_reason is not None:
            _log_cycle_progress("[decision %d/%d] %s blocked %s", index, len(symbols_to_decide), sym, exit_block_reason)
            apply_default_schedule_after_blocked()
            record_decision({**entry, "executed": False, "reason": exit_block_reason})
            continue
        if clamped_quantity != effective_quantity:
            entry.setdefault("requested_qty", effective_quantity)
            effective_quantity = clamped_quantity
            entry["qty"] = effective_quantity
        if effective_quantity == 0:
            _log_cycle_progress("[decision %d/%d] %s hold zero_exit_quantity", index, len(symbols_to_decide), sym)
            apply_default_schedule_after_blocked()
            record_decision({**entry, "executed": False, "reason": "zero_exit_quantity"})
            continue

        pure_open = decision.intent in {"OPEN_LONG", "OPEN_SHORT"}
        trace_risk = pure_open or decision.intent == "REVERSE"
        open_stop_distance: float | None = None
        if trace_risk:
            hard_stop_price = _hard_stop_price(runtime_exit_plan)
            risk_quantity = effective_quantity
            if decision.intent == "REVERSE":
                # REVERSE est tracé mais PAS clampé au risque (dette connue).
                risk_quantity = _reverse_open_quantity(
                    action=decision.action,
                    quantity=effective_quantity,
                    position_quantity=0.0 if pos is None else pos.quantity,
                )
            entry["risk_clamped"] = False
            entry["risk_unbounded_no_stop"] = hard_stop_price is None
            if hard_stop_price is None:
                _set_entry_risk_metrics(
                    entry,
                    quantity=risk_quantity,
                    stop_distance=None,
                    equity=snap.equity,
                )
                if pure_open and require_hard_stop:
                    # Guardrail humain rendu déterministe (mandate/guardrails.json,
                    # D6 du registre) : pas d'ouverture sans hard_stop, quelle que
                    # soit la confiance. REVERSE reste tracé non bloqué (dette connue).
                    # Désactivable en paper (require_hard_stop=false) : stop optionnel.
                    _log_cycle_progress(
                        "[risk] %s rejected code=missing_hard_stop", sym
                    )
                    apply_default_schedule_after_blocked()
                    record_decision(
                        {**entry, "executed": False, "reason": "risk:missing_hard_stop"}
                    )
                    continue
            else:
                open_stop_distance = abs(prices[sym] - hard_stop_price)
                _set_entry_risk_metrics(
                    entry,
                    quantity=risk_quantity,
                    stop_distance=open_stop_distance,
                    equity=snap.equity,
                )
                if pure_open:
                    max_risk_quantity = gate.max_quantity_at_risk(
                        snap.equity,
                        prices[sym],
                        hard_stop_price,
                        fx_rate=_rate(sym),
                    )
                    entry["max_risk_qty"] = max_risk_quantity
                    if max_risk_quantity <= 0:
                        _log_cycle_progress("[decision %d/%d] %s hold zero_risk_quantity", index, len(symbols_to_decide), sym)
                        apply_default_schedule_after_blocked()
                        record_decision({**entry, "executed": False, "reason": "zero_risk_quantity"})
                        continue
                    if effective_quantity > max_risk_quantity:
                        _log_cycle_progress(
                            "[risk] %s rejected code=risk_per_trade_exceeded qty=%s max_qty=%s",
                            sym,
                            effective_quantity,
                            max_risk_quantity,
                        )
                        apply_default_schedule_after_blocked()
                        record_decision(
                            {
                                **entry,
                                "executed": False,
                                "reason": "risk:risk_per_trade_exceeded",
                                "context": (
                                    f"qty={effective_quantity} max_qty={max_risk_quantity:.8f} "
                                    f"risk_pct={entry.get('risk_pct')} "
                                    f"limit={gate.limits.max_risk_per_trade_pct}"
                                ),
                            }
                        )
                        continue

            if pure_open and effective_quantity == 0:
                _log_cycle_progress("[decision %d/%d] %s hold zero_risk_quantity", index, len(symbols_to_decide), sym)
                apply_default_schedule_after_blocked()
                record_decision({**entry, "executed": False, "reason": "zero_risk_quantity"})
                continue

            if pure_open:
                # Gate de confiance adapté au risque (ouvertures pures uniquement).
                # REDUCE/CLOSE/REVERSE : réduire le risque doit toujours rester possible.
                conf_verdict = gate.check_confidence(
                    decision.confidence,
                    entry.get("risk_pct"),
                )
                if not conf_verdict.approved:
                    _log_cycle_progress(
                        "[risk] %s rejected code=%s confidence=%s",
                        sym,
                        conf_verdict.code,
                        decision.confidence,
                    )
                    apply_default_schedule_after_blocked()
                    record_decision(
                        {**entry, "executed": False, "reason": f"risk:{conf_verdict.code}", "context": conf_verdict.context}
                    )
                    continue

        order = Order(symbol=sym, side=decision.action, quantity=effective_quantity, rationale=decision.rationale)
        cur_pos_value = (pos.quantity * prices[sym] * _rate(sym)) if pos else 0.0
        allow_risk_reduction = decision.intent in {"REDUCE", "CLOSE"}
        verdict = gate.check(
            order,
            prices[sym],
            current_position_value=cur_pos_value,
            gross_exposure=gross,
            equity=snap.equity,
            allow_risk_reduction=allow_risk_reduction,
            fx_rate=_rate(sym),
        )

        if not verdict.approved:
            _log_cycle_progress(
                "[risk] %s rejected code=%s qty=%s price=%s",
                sym,
                verdict.code,
                order.quantity,
                round(prices[sym], 6),
            )
            apply_default_schedule_after_blocked()
            record_decision({**entry, "executed": False, "reason": f"risk:{verdict.code}", "context": verdict.context})
            continue

        fill = broker.submit(order, prices[sym], now.isoformat(), dry_run=dry_run, fx_rate=_rate(sym))
        if not dry_run:
            gate.record_pass()
            gross = _gross_exposure(broker, prices, rate_of=_rate)
            if fill is not None:
                latest = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity, fx_rate_of=_rate)
                final_position = broker.positions().get(sym)
                llm_exit_reason = _llm_exit_reason_for_intent(decision.intent)
                _append_model_performance(
                    ts=fill.ts,
                    symbol=sym,
                    action=decision.action,
                    intent=decision.intent,
                    **({"exit_reason": llm_exit_reason} if llm_exit_reason is not None else {}),
                    quantity=effective_quantity,
                    price=prices[sym],
                    commission=fill.commission,
                    commission_currency=fill.commission_currency,
                    commission_model=fill.commission_model,
                    fx_rate=fill.fx_rate,
                    confidence=decision.confidence,
                    llm_provider=decision.llm_provider or "unknown",
                    llm_model=decision.llm_model or "unknown",
                    llm_fallback_reason=decision.llm_fallback_reason,
                    equity=latest.equity,
                    cash=latest.cash,
                    position_quantity=0.0 if final_position is None else final_position.quantity,
                )
                entry["model_performance_logged"] = True
                entry["commission"] = fill.commission
                entry["commission_currency"] = fill.commission_currency
                entry["commission_model"] = fill.commission_model
                entry["fx_rate"] = fill.fx_rate
            if fill is not None and decision.intent in {"CLOSE", "REVERSE"}:
                plan_store.close_symbol(sym)
            if fill is not None and decision.intent == "REDUCE":
                final_position = broker.positions().get(sym)
                plan_store.sync_symbol_quantity(sym, 0.0 if final_position is None else abs(final_position.quantity))
            if (
                fill is not None
                and runtime_exit_plan
                and decision.intent in {"OPEN_LONG", "OPEN_SHORT", "REVERSE"}
            ):
                # §13.7 — contexte d'entrée durable capturé dans le TradePlan : la thèse
                # (rationale LLM) et un snapshot du contexte au tir, réinjectables au réveil.
                _entry_age = data_age_by_symbol.get(sym)
                entry_meta = {
                    "entry_thesis": decision.rationale,
                    "entry_context": {
                        "price": prices[sym],
                        "runtime_interval": runtime_interval,
                        "data_age_m": None if _entry_age is None else int(round(_entry_age)),
                        "session_open": bool(market.session_snapshot(sym, now=now).get("open")),
                        "daily_as_of": (
                            (execution_eligibility.get(sym) or {}).get("planning") or {}
                        ).get("daily_as_of"),
                    },
                }
                if decision.intent == "REVERSE":
                    created_plan = _create_plan_for_final_position(
                        broker=broker,
                        plan_store=plan_store,
                        symbol=sym,
                        price=prices[sym],
                        opened_at=fill.ts,
                        raw_exit_plan=runtime_exit_plan,
                        reference_volatility=reference_volatility,
                        llm_provider=decision.llm_provider,
                        llm_model=decision.llm_model,
                        llm_fallback_reason=decision.llm_fallback_reason,
                        llm_confidence=decision.confidence,
                    )
                    entry["trade_plan_created"] = created_plan is not None
                    if created_plan is not None:
                        created_plan = replace(created_plan, **entry_meta)
                        plan_store.upsert(created_plan)
                        entry["trade_plan"] = _plan_snapshot(created_plan)
                else:
                    plan = create_trade_plan_from_order(
                        symbol=sym,
                        order_side=decision.action,
                        quantity=effective_quantity,
                        entry_price=prices[sym],
                        opened_at=fill.ts,
                        raw_exit_plan=runtime_exit_plan,
                        reference_volatility=reference_volatility,
                        llm_provider=decision.llm_provider,
                        llm_model=decision.llm_model,
                        llm_fallback_reason=decision.llm_fallback_reason,
                        llm_confidence=decision.confidence,
                    )
                    plan = replace(plan, **entry_meta)
                    plan_store.upsert(plan)
                    entry["trade_plan_created"] = True
                    entry["trade_plan"] = _plan_snapshot(plan)
            if fill is not None and decision.intent in {"OPEN_LONG", "OPEN_SHORT", "REVERSE"}:
                post_entry_wake = market.freshness_budget_minutes(runtime_interval, grace_minutes=0.0)
                if next_wake_in_minutes is None or next_wake_in_minutes > post_entry_wake:
                    next_wake_in_minutes = post_entry_wake
                    entry["next_wake_in_minutes"] = next_wake_in_minutes
                    entry["post_entry_review_scheduled"] = True
        _log_cycle_progress(
            "[order] %s %s qty=%s price=%s executed=%s plan=%s",
            sym,
            decision.action,
            effective_quantity,
            round(prices[sym], 6),
            not dry_run,
            entry["trade_plan_created"],
        )
        apply_decision_schedule()
        record_decision({**entry, "executed": not dry_run, "reason": "ok", "price": prices[sym]})

    report["model_calls_used"] = model_calls_used
    refresh_report_portfolio()
    _write_current_report(report)
    _log_cycle_progress(
        "[cycle] completed decisions=%d executed=%d calls=%d/%d",
        len(report["decisions"]),
        sum(1 for item in report["decisions"] if item.get("executed")),
        model_calls_used,
        max_model_calls_per_cycle,
    )
    _write_status(
        "cycle_completed",
        current_symbol=None,
        decisions_done=len(report["decisions"]),
        symbols_total=len(symbols_to_decide),
        model_calls_used=model_calls_used,
        max_model_calls_per_cycle=max_model_calls_per_cycle,
        last_decision=report["decisions"][-1] if report["decisions"] else None,
    )
    _append_event("cycle_completed", decisions_done=len(report["decisions"]), model_calls_used=model_calls_used)
    consolidation_result = consolidator.maybe_consolidate(
        learnings_store,
        consolidated_learnings_store,
        threshold=learning_consolidation_threshold,
        acpx_bin=consolidator_acpx_bin,
        acpx_agent=consolidator_acpx_agent,
        model=consolidator_model,
        timeout_s=consolidator_timeout_s,
        attribution=attribution_payload,
        meta_performance=meta_performance_payload,
    )
    if consolidation_result.get("triggered"):
        report["learning_consolidation"] = consolidation_result
        _write_current_report(report)
        _append_event("learning_consolidated", **consolidation_result)
    # Collecte macro quotidienne best-effort (DBnomics, zéro clé API).
    # Ne touche jamais au chemin de décision : avalée silencieusement si elle échoue.
    try:
        _macro_collect_result = macro_series.maybe_collect(STATE_DIR, now)
        if _macro_collect_result.get("triggered"):
            log.debug(
                "macro_series: collected=%s skipped=%s errors=%s",
                _macro_collect_result.get("collected", 0),
                _macro_collect_result.get("skipped", 0),
                _macro_collect_result.get("errors", 0),
            )
    except Exception:  # noqa: BLE001 — best-effort total, jamais d'impact cycle
        pass
    # Mémorise les rejets gross de CE cycle pour les réinjecter au prochain (None
    # si aucun → efface un éventuel feedback périmé).
    _LAST_GROSS_REJECTIONS[str(STATE_DIR)] = summarize_gross_rejections(report["decisions"])
    return report


def main(
    argv: list[str] | None = None,
    *,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> None:
    # Charge le .env AVANT l'argparse pour que les defaults _env_int/_env
    # (CASYS_DECISION_BATCH_PARALLELISM, etc.) le voient. override=False ⇒
    # une var déjà posée en CLI/inline reste prioritaire.
    llm.load_dotenv()
    parser = argparse.ArgumentParser(description="casys-trader daemon (boucle runtime)")
    parser.add_argument("--live", action="store_true", help="exécute réellement les ordres (défaut: dry-run)")
    parser.add_argument("--once", action="store_true", help="un seul cycle puis sortie")
    parser.add_argument("--poll", type=float, default=30.0, help="secondes entre deux vérifications du scheduler")
    parser.add_argument("--default-wake-minutes", type=float, default=30.0, help="cadence globale par défaut")
    parser.add_argument("--min-wake-minutes", type=float, default=5.0, help="borne basse pour un override symbole")
    parser.add_argument("--max-wake-minutes", type=float, default=240.0, help="borne haute pour un override symbole")
    parser.add_argument("--max-context-requests-per-symbol", type=int, default=2, help="nombre max de requêtes indicateurs par symbole")
    parser.add_argument("--max-indicators-per-request", type=int, default=4, help="nombre max d'indicateurs par requête")
    parser.add_argument("--max-model-calls-per-cycle", type=int, default=25, help="fusible coût: appels LLM max par cycle")
    parser.add_argument(
        "--decision-timeout-s",
        type=int,
        default=_env_int("CASYS_DECISION_TIMEOUT_S", 900),
        help="plafond de sécurité du temps de décision LLM (s) ; spark n'est PAS coupé avant, et un timeout ne déclenche PAS de fallback (HOLD fail-safe)",
    )
    parser.add_argument(
        "--decision-batch-size",
        type=int,
        default=_env_int("CASYS_DECISION_BATCH_SIZE", DEFAULT_DECISION_BATCH_SIZE),
        help="nombre max de symboles par appel LLM décideur (défaut/env CASYS_DECISION_BATCH_SIZE: 5)",
    )
    parser.add_argument(
        "--decision-batch-parallelism",
        type=int,
        default=_env_int("CASYS_DECISION_BATCH_PARALLELISM", DEFAULT_DECISION_BATCH_PARALLELISM),
        help="nombre max d'appels LLM décideur lancés en parallèle (défaut/env CASYS_DECISION_BATCH_PARALLELISM: 3)",
    )
    parser.add_argument(
        "--agent-tools",
        action=argparse.BooleanOptionalAction,
        default=_env_int("CASYS_AGENT_TOOLS_ENABLED", 0) == 1,
        help="tournée d'outils domaine pour le LLM (défaut/env CASYS_AGENT_TOOLS_ENABLED: 0)",
    )
    parser.add_argument(
        "--learning-consolidation-threshold",
        type=int,
        default=_env_int("TRADER_LEARNING_CONSOLIDATION_THRESHOLD", DEFAULT_LEARNING_CONSOLIDATION_THRESHOLD),
        help="nombre de nouveaux learnings bruts avant consolidation",
    )
    parser.add_argument(
        "--consolidator-acpx-bin",
        default=os.getenv("TRADER_CONSOLIDATOR_ACPX_BIN", "acpx"),
        help="binaire acpx utilisé par le consolidateur",
    )
    parser.add_argument(
        "--consolidator-acpx-agent",
        default=os.getenv("TRADER_CONSOLIDATOR_ACPX_AGENT", consolidator.DEFAULT_CONSOLIDATOR_ACPX_AGENT),
        help="agent acpx du consolidateur (ex: codex, claude, default)",
    )
    parser.add_argument(
        "--consolidator-model",
        default=os.getenv("TRADER_CONSOLIDATOR_MODEL", consolidator.DEFAULT_CONSOLIDATOR_MODEL),
        help="modèle utilisé par le consolidateur",
    )
    parser.add_argument(
        "--consolidator-timeout-s",
        type=int,
        default=_env_int("TRADER_CONSOLIDATOR_TIMEOUT_S", consolidator.DEFAULT_CONSOLIDATOR_TIMEOUT_S),
        help="timeout LLM du consolidateur",
    )
    parser.add_argument("--bootstrap-all", action="store_true", help="ignore les timers au démarrage et force tous les symboles")
    parser.add_argument("--ib-host", default=os.getenv("CASYS_IB_HOST", DEFAULT_IB_HOST), help="host IB Gateway/TWS")
    parser.add_argument("--ib-port", type=int, default=_env_int("CASYS_IB_PORT", DEFAULT_IB_PORT), help="port API IB")
    parser.add_argument(
        "--ib-client-id",
        type=int,
        default=_env_int("CASYS_IB_CLIENT_ID", DEFAULT_IB_CLIENT_ID),
        help="clientId IB utilisé par le daemon",
    )
    parser.add_argument(
        "--ib-attach-retry-seconds",
        type=float,
        default=300.0,
        help="secondes minimales entre deux tentatives de rattachement IB lazy en profil paper",
    )
    parser.add_argument(
        "--data-profile",
        default=os.getenv("TRADER_DATA_PROFILE"),
        help="profil de routing data (paper|prod). Override config/data_sources.yaml. Env: TRADER_DATA_PROFILE",
    )
    parser.add_argument(
        "--commission-model",
        default=os.getenv("TRADER_COMMISSION_MODEL", "ibkr"),
        choices=["none", "ibkr"],
        help="modèle de frais appliqué au SimBroker live (défaut/env TRADER_COMMISSION_MODEL: ibkr)",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.ib_attach_retry_seconds) or args.ib_attach_retry_seconds <= 0:
        parser.error("--ib-attach-retry-seconds doit être > 0")
    if args.decision_batch_size <= 0:
        parser.error("--decision-batch-size doit être > 0")
    if args.decision_batch_parallelism <= 0:
        parser.error("--decision-batch-parallelism doit être > 0")
    now = now_fn or (lambda: datetime.now(timezone.utc))
    sleep = sleep_fn or time.sleep

    from .logging_setup import setup_logging
    # Niveau console pilotable via CASYS_LOG_LEVEL (.env/CLI), défaut INFO.
    # DEBUG ressort le détail fetch par-symbole (source_skipped/stale) sinon muet.
    _log_level = logging.getLevelNamesMapping().get(
        os.getenv("CASYS_LOG_LEVEL", "INFO").upper(), logging.INFO
    )
    setup_logging(level=_log_level)
    dry_run = not args.live
    commission_model = commission_model_from_name(args.commission_model)

    # Identité daemon : revendiquer le pid file en premier (avant tout _write_status).
    # Refus si un daemon vivant le détient déjà — un doublon qui écrase puis supprime
    # daemon.pid à son arrêt rend le daemon légitime inarrêtable depuis le cockpit.
    from .cockpit_supervisor import claim_pid_file, release_pid_file

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    news_feed.set_default_news_archive(STATE_DIR / "news_items")
    _pid_file = STATE_DIR / "daemon.pid"
    if not claim_pid_file(pid_file=_pid_file, pid=os.getpid()):
        log.error("daemon déjà vivant (pid file %s) — refus de démarrer un doublon", _pid_file)
        return 1

    # D-c : rotation mensuelle des ledgers JSONL au démarrage, avant la boucle
    # et avant toute création de DecisionLedgerStore (le cache d'IDs sera
    # initialisé sur le fichier vif déjà tourné).
    _archive_dir = STATE_DIR / "archive"
    _rotation_now = now()
    for _rot_path, _rot_ts_key in (
        (STATE_DIR / decision_ledger.DEFAULT_LEDGER_FILENAME, "cycle_ts"),
        (STATE_DIR / "events.jsonl", "ts"),
    ):
        try:
            _rot_result = ledger_rotation.rotate_monthly(
                _rot_path, _archive_dir, now=_rotation_now, ts_key=_rot_ts_key
            )
            if _rot_result["archived"]:
                log.info(
                    "[rotation] %s : archived=%d kept=%d files=%s",
                    _rot_path.name,
                    _rot_result["archived"],
                    _rot_result["kept"],
                    _rot_result["files"],
                )
        except Exception as _rot_exc:  # noqa: BLE001
            log.warning("[rotation] échec sur %s : %s", _rot_path.name, _rot_exc)

    sched = scheduler.Scheduler(STATE_DIR / "scheduler.json")
    log.info("daemon démarré (dry_run=%s, once=%s)", dry_run, args.once)
    log.info(
        "[config] decision_batch_parallelism=%d batch_size=%d max_model_calls_per_cycle=%d decision_timeout_s=%d agent_tools=%s",
        args.decision_batch_parallelism,
        args.decision_batch_size,
        args.max_model_calls_per_cycle,
        args.decision_timeout_s,
        args.agent_tools,
    )
    bootstrap = args.bootstrap_all

    data_source = None
    _composite_available: dict[str, object] = {}
    _ib_attach_backoff: IBAttachBackoff | None = None
    _data_sources_cfg = ROOT / "config" / "data_sources.yaml"
    _use_composite = _data_sources_cfg.exists()

    # F3 : valider la config UNE FOIS avant la boucle — fail-fast explicite
    # si le fichier est malformé, le profil inconnu, ou une source invalide.
    # Cette étape ne fait aucune connexion réseau ; les MarketError ici
    # remontent directement (pas avalées par le except interne de la boucle).
    _composite_routes: list[dict] = []
    _composite_profile: str = ""
    if _use_composite:
        _composite_profile_override = args.data_profile or None
        _composite_routes, _composite_profile = parse_data_sources_config(
            _data_sources_cfg,
            profile_override=_composite_profile_override,
            known_source_names=frozenset({"yfinance", "ib"}),
        )
        log.info("data_source config validée: profil=%s", _composite_profile)

    try:
        while True:
            sleep_seconds: float | None = None
            stop_after_iteration = False
            try:
                if data_source is None:
                    if _use_composite:
                        # Config présente : construire composite.
                        # IB obligatoire en profil prod — si down, cycle sauté.
                        # IB optionnel en profil paper — si down, on continue sans lui.
                        _ib_source: IBDataSource | None = None
                        _ib_required = (_composite_profile == "prod")
                        # market_data_type : 1 (live) en prod, 3 (delayed) en paper.
                        # Type 3 = live quand la souscription n'est pas requise (ex. FX
                        # IDEALPRO), delayed sinon — c'est le comportement IB par défaut.
                        _mdt = 1 if _composite_profile == "prod" else 3
                        try:
                            _ib_obj = connect_ib(
                                args.ib_host, args.ib_port, args.ib_client_id,
                                market_data_type=_mdt,
                            )
                            try:
                                _ib_source = IBDataSource(
                                    _ib_obj,
                                    reconnect_factory=lambda: connect_ib(
                                        args.ib_host, args.ib_port, args.ib_client_id,
                                        market_data_type=_mdt,
                                    ),
                                )
                            except Exception as _ib_ds_exc:  # noqa: BLE001 - frontière config/source IB
                                _disconnect_quietly(_ib_obj)
                                if isinstance(_ib_ds_exc, market.MarketError):
                                    raise
                                raise market.MarketError(
                                    "ib_connect_failed",
                                    f"IBDataSource init failed: {type(_ib_ds_exc).__name__}: {_ib_ds_exc}",
                                ) from _ib_ds_exc
                        except market.MarketError as _ib_exc:
                            if _ib_required:
                                # Prod : IB obligatoire — remonter l'erreur pour que
                                # le cycle soit sauté et réessayé au prochain réveil.
                                raise
                            log.warning(
                                "IB indisponible, profil composite sans IB (%s): %s",
                                _ib_exc.code, _ib_exc.context,
                            )
                        available: dict[str, object] = {"yfinance": YFinanceDataSource()}
                        if _ib_source is not None:
                            available["ib"] = _ib_source
                        elif not _ib_required:
                            _ib_attach_backoff = IBAttachBackoff(
                                retry_after=timedelta(seconds=args.ib_attach_retry_seconds)
                            )
                            _ib_attach_backoff.record_failure(now())
                        _composite_available = available
                        data_source = CompositeDataSource(
                            routes=_composite_routes,
                            sources=available,
                        )
                        log.info(
                            "data_source=composite profil=%s sources=%s",
                            _composite_profile, sorted(available),
                        )
                    else:
                        # Comportement actuel inchangé (rétrocompat, config absente).
                        # market_data_type=3 explicite (delayed, comportement existant).
                        ib = connect_ib(
                            args.ib_host, args.ib_port, args.ib_client_id,
                            market_data_type=3,
                        )
                        data_source = IBDataSource(
                            ib,
                            reconnect_factory=lambda: connect_ib(
                                args.ib_host, args.ib_port, args.ib_client_id,
                                market_data_type=3,
                            ),
                        )
                loop_now = now()
                if (
                    _use_composite
                    and _composite_profile == "paper"
                    and data_source is not None
                    and _ib_attach_backoff is not None
                    and "ib" not in _composite_available
                    and _ib_attach_backoff.due(loop_now)
                ):
                    try:
                        _ib_obj = connect_ib(
                            args.ib_host, args.ib_port, args.ib_client_id,
                            market_data_type=3,
                            attempts=1,
                            backoff_seconds=0.0,
                        )
                    except market.MarketError as _ib_exc:
                        _ib_attach_backoff.record_failure(loop_now)
                        log.warning(
                            "ib_attach: IB toujours indisponible (%s): %s",
                            _ib_exc.code,
                            _ib_exc.context,
                        )
                    else:
                        try:
                            _ib_source = IBDataSource(
                                _ib_obj,
                                reconnect_factory=lambda: connect_ib(
                                    args.ib_host, args.ib_port, args.ib_client_id,
                                    market_data_type=3,
                                ),
                            )
                        except Exception as _ib_ds_exc:  # noqa: BLE001 - frontière config/source IB
                            _disconnect_quietly(_ib_obj)
                            _ib_attach_backoff.record_failure(loop_now)
                            log.warning(
                                "ib_attach: construction source IB échouée (%s): %s",
                                type(_ib_ds_exc).__name__,
                                _ib_ds_exc,
                            )
                        else:
                            _composite_available = {**_composite_available, "ib": _ib_source}
                            data_source = CompositeDataSource(
                                routes=_composite_routes,
                                sources=_composite_available,
                            )
                            _ib_attach_backoff.record_success()
                            log.info("ib_attach: IB rattaché en cours de session")
                # Veille à deux niveaux — hot-lists par marché (D10). À chaque cycle :
                # recalcule les venues dont la session vient de clôturer (1 scan radar),
                # puis compose l'univers actif = sticky ∪ union(marchés ouverts) et l'écrit
                # SI changé. Pas de cron externe ; état par venue persisté → rattrapage au
                # redémarrage. Fail-safe : n'interrompt jamais le cycle.
                from .rotation_venues import tick as _rotation_tick
                from .radar_config import load_radar_params as _load_radar_params
                try:
                    _radar_params = _load_radar_params(ROOT / "config")
                    _override_fn = None
                    if _radar_params.override_enabled:
                        from .rotation_wiring import build_llm_override_fn as _build_override
                        _override_fn = _build_override()
                    # market_context v1 : peuplé depuis le cache de régime si disponible
                    from .rotation_wiring import build_market_context_from_regime as _build_mctx
                    _market_context = None
                    _regime_cache_path = STATE_DIR / "last_regime.json"
                    if _regime_cache_path.exists():
                        try:
                            import json as _json
                            _cached_regime = _json.loads(_regime_cache_path.read_text(encoding="utf-8"))
                            _market_context = _build_mctx(_cached_regime)
                        except Exception:  # noqa: BLE001
                            pass
                    _rotation_tick(
                        ROOT / "config",
                        STATE_DIR,
                        loop_now.isoformat(),
                        override_fn=_override_fn,
                        market_context=_market_context,
                    )
                except Exception:  # noqa: BLE001 — la rotation ne doit pas faire tomber le daemon
                    log.exception("rotation tick (D10) échouée")
                symbols = _load_yaml(ROOT / "config" / "universe.yaml")["symbols"]
                sched.reconcile_universe(symbols)
                indicator_triggers = (
                    []
                    if args.once or bootstrap
                    else _scan_indicator_watches(symbols, sched=sched, now=loop_now, data_source=data_source)
                )
                due_symbols = _select_due_symbols(symbols, sched=sched, once=args.once, bootstrap=bootstrap, now=loop_now)
                bootstrap = False
                if not due_symbols:
                    report = run_cycle(
                        dry_run=dry_run,
                        now=loop_now,
                        symbols_filter=[],
                        sched=sched,
                        data_source=data_source,
                        default_wake_minutes=args.default_wake_minutes,
                        min_wake_minutes=args.min_wake_minutes,
                        max_wake_minutes=args.max_wake_minutes,
                        max_context_requests_per_symbol=args.max_context_requests_per_symbol,
                        max_indicators_per_request=args.max_indicators_per_request,
                        max_model_calls_per_cycle=args.max_model_calls_per_cycle,
                        indicator_triggers=indicator_triggers,
                        learning_consolidation_threshold=args.learning_consolidation_threshold,
                        consolidator_acpx_bin=args.consolidator_acpx_bin,
                        consolidator_acpx_agent=args.consolidator_acpx_agent,
                        consolidator_model=args.consolidator_model,
                        consolidator_timeout_s=args.consolidator_timeout_s,
                        decision_timeout_s=args.decision_timeout_s,
                        decision_batch_size=args.decision_batch_size,
                        decision_batch_parallelism=args.decision_batch_parallelism,
                        commission_model=commission_model,
                        agent_tools_enabled=args.agent_tools,
                    )
                    if report.get("planned_exits") or report.get("decisions") or report.get("exit_watch_triggers"):
                        log.debug("cycle actif sans symbole dû: %s", json.dumps(report, ensure_ascii=False))
                        STATE_DIR.mkdir(parents=True, exist_ok=True)
                        (STATE_DIR / "last_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
                        _append_cycle_history(report)
                    wait = sched.seconds_until_wake(symbols)
                    sleep_seconds = min(wait, args.poll)
                    log.debug("aucun symbole dû — pause %.0fs", sleep_seconds)
                else:
                    report = run_cycle(
                        dry_run=dry_run,
                        now=loop_now,
                        symbols_filter=due_symbols,
                        sched=sched,
                        data_source=data_source,
                        default_wake_minutes=args.default_wake_minutes,
                        min_wake_minutes=args.min_wake_minutes,
                        max_wake_minutes=args.max_wake_minutes,
                        max_context_requests_per_symbol=args.max_context_requests_per_symbol,
                        max_indicators_per_request=args.max_indicators_per_request,
                        max_model_calls_per_cycle=args.max_model_calls_per_cycle,
                        indicator_triggers=indicator_triggers,
                        learning_consolidation_threshold=args.learning_consolidation_threshold,
                        consolidator_acpx_bin=args.consolidator_acpx_bin,
                        consolidator_acpx_agent=args.consolidator_acpx_agent,
                        consolidator_model=args.consolidator_model,
                        consolidator_timeout_s=args.consolidator_timeout_s,
                        decision_timeout_s=args.decision_timeout_s,
                        decision_batch_size=args.decision_batch_size,
                        decision_batch_parallelism=args.decision_batch_parallelism,
                        commission_model=commission_model,
                        agent_tools_enabled=args.agent_tools,
                    )
                    log.debug("cycle: %s", json.dumps(report, ensure_ascii=False))
                    STATE_DIR.mkdir(parents=True, exist_ok=True)
                    (STATE_DIR / "last_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

                    # Historique d'équité : une ligne JSON compacte par cycle actif (append).
                    _append_cycle_history(report)

                    if args.once:
                        stop_after_iteration = True
                    else:
                        wait = sched.seconds_until_wake(symbols)
                        sleep_seconds = min(wait, args.poll)
                        log.info("[sleep] next_check_in=%.0fs next_due_in=%.0fs", sleep_seconds, wait)
                consume_failed_sources = getattr(data_source, "consume_failed_sources", None)
                failed_sources = consume_failed_sources() if callable(consume_failed_sources) else {}
                ib_failure = failed_sources.get("ib") if isinstance(failed_sources, dict) else None
                if (
                    _use_composite
                    and _composite_profile == "paper"
                    and "ib" in _composite_available
                    and ib_failure is not None
                    and _is_connection_market_error(ib_failure)
                ):
                    _disconnect_quietly(_composite_available["ib"])
                    _composite_available = {
                        name: source
                        for name, source in _composite_available.items()
                        if name != "ib"
                    }
                    data_source = CompositeDataSource(
                        routes=_composite_routes,
                        sources=_composite_available,
                    )
                    if _ib_attach_backoff is None:
                        _ib_attach_backoff = IBAttachBackoff(
                            retry_after=timedelta(seconds=args.ib_attach_retry_seconds)
                        )
                    _ib_attach_backoff.record_failure(loop_now)
                    log.warning("ib_attach: IB détaché après échec source, profil paper dégradé")
            except market.MarketError as exc:
                if data_source is not None:
                    _disconnect_quietly(data_source)
                    data_source = None
                if _use_composite:
                    # En mode composite la source est déjà construite — une MarketError
                    # ici vient du cycle lui-même (ex: all_sources_failed). On reset
                    # pour reconstruire au prochain tour.
                    log.warning("cycle échoué (%s): %s", exc.code, exc.context)
                else:
                    log.warning("IB indisponible, cycle sauté (%s): %s", exc.code, exc.context)
                _write_status(
                    "ib_connection_failed",
                    current_symbol=None,
                    error_code=exc.code,
                    error_context=exc.context,
                )
                if args.once:
                    stop_after_iteration = True
                else:
                    sleep_seconds = args.poll

            if stop_after_iteration:
                break
            if sleep_seconds is not None:
                sleep(sleep_seconds)
    finally:
        if data_source is not None:
            _disconnect_quietly(data_source)
        # Suppression du pid file au shutdown propre — seulement s'il contient
        # encore NOTRE pid (jamais celui d'un successeur, cf bug Maj+X cockpit).
        try:
            release_pid_file(pid_file=_pid_file, pid=os.getpid())
        except Exception:  # noqa: BLE001 — best-effort, ne jamais bloquer la sortie
            pass


if __name__ == "__main__":
    main()
