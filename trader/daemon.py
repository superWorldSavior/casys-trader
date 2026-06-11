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
import json
import logging
import math
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .agent_context import build_market_cockpit, resolve_indicator_requests
from . import attribution, code_version, codex_client, consolidator, decision_ledger, family_regime, relevance_gate, stats
from .exit_engine import evaluate_plan
from .features import DEFAULT_INDICATORS
from .indicator_watch import (
    armed_order_price_coherent,
    build_indicator_watch,
    evaluate_indicator_watches,
    watch_market_requests,
)
from .risk import RiskGate, RiskLimits
from .tools import market, memory as memory_mod, portfolio, scheduler
from .tools.execution import Order, SimBroker
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
    validate_exit_plan,
)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

log = logging.getLogger("casys-trader")

_VALID_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE", "HOLD"}
_ACTION_INTENTS = {
    "BUY": {"OPEN_LONG", "REDUCE", "CLOSE", "REVERSE"},
    "SELL": {"OPEN_SHORT", "REDUCE", "CLOSE", "REVERSE"},
}
# Barres fines (15m) pour coller à la cadence scalping (réveils 5-30 min) et avoir
# un prix qui bouge intra-heure. La fraîcheur sur ces barres sert de garde
# « marché live » (UTC, sans logique de fuseau). ~40 min ≈ tolérance de 2 barres + délai.
DEFAULT_RUNTIME_INTERVAL = "15m"
DEFAULT_RUNTIME_LOOKBACK = "5d"
DEFAULT_MAX_MARKET_DATA_AGE_MINUTES = 40.0
COCKPIT_DAILY_LOOKBACK = "1y"
COCKPIT_DAILY_INTERVAL = "1d"
COCKPIT_DAILY_MAX_AGE_MINUTES = 48.0 * 60.0
DEFAULT_IB_HOST = "127.0.0.1"
DEFAULT_IB_PORT = 4002
DEFAULT_IB_CLIENT_ID = 17
DEFAULT_LEARNING_CONSOLIDATION_THRESHOLD = consolidator.DEFAULT_CONSOLIDATION_THRESHOLD

# D7 étage A — dernier passage LLM par (state_dir, symbole), pour la revue
# périodique garantie du gate de pertinence. Volatile : reset au restart.
_LAST_LLM_AT: dict[tuple[str, str], object] = {}
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


def _gross_exposure(broker: SimBroker, prices: dict[str, float]) -> float:
    return sum(
        abs(pos.quantity * prices.get(symbol, 0.0))
        for symbol, pos in broker.positions().items()
    )


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
            }
            if plan.trailing_stop is not None
            else None
        ),
        "max_hold_minutes": plan.max_hold_minutes,
        "filled_take_profits": list(plan.filled_take_profits),
        "high_watermark": plan.high_watermark,
        "low_watermark": plan.low_watermark,
    }


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

        # Use fill_price from the signal when available (intra-bar stop/TP detection).
        # fill_price is conservative: never better than the stop/TP level.
        effective_fill_price = evaluation.signal.fill_price if evaluation.signal.fill_price is not None else price

        order = Order(
            symbol=evaluation.signal.symbol,
            side=evaluation.signal.side,
            quantity=clamped_quantity,
            rationale=evaluation.signal.reason,
        )
        fill = broker.submit(order, effective_fill_price, now.isoformat(), dry_run=dry_run)
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
                latest = portfolio.snapshot(broker, lambda s: price_map.get(s, 0.0), starting_equity)
                _append_model_performance(
                    ts=fill.ts,
                    symbol=plan.symbol,
                    action=evaluation.signal.side,
                    intent="PLANNED_EXIT",
                    exit_reason=evaluation.signal.reason,
                    source_plan_id=plan.id,
                    quantity=clamped_quantity,
                    price=effective_fill_price,
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
    decision_timeout_s: int = 900,
) -> tuple[dict[str, codex_client.Decision], int]:
    """Décide TOUS les symboles dus en UN appel batch (contexte partagé envoyé une
    seule fois). Gère le round-trip REQUEST_CONTEXT en batch : les symboles qui
    demandent un complément sont résolus puis re-décidés en un 2e batch. Isolation
    per-élément assurée par codex_client.parse_batch. `max_model_calls` est le fusible
    coût (un batch = 1 appel ; le round-trip en ajoute 1). Retourne (décisions, n_appels)."""
    if not decidable:
        return {}, 0
    if max_model_calls < 1:
        return {sym: codex_client.Decision.hold(sym, "model_call_budget_exhausted") for sym in decidable}, 0
    def _symbol_facts(sym: str) -> dict:
        # Faits calculés par le code (pas des consignes en prose) : âge réel des
        # prix et état de la séance de la place du symbole. Âge inconnu = None.
        age = data_age_by_symbol.get(sym)
        return {
            "data_age_m": None if age is None else int(round(age)),
            "session": market.session_snapshot(sym, now=now),
        }

    per_symbol = {
        sym: {"indicator_triggers": triggers_by_symbol.get(sym, []), **_symbol_facts(sym)}
        for sym in decidable
    }
    responses = codex_client.decide_batch(
        symbols=decidable,
        mandate=mandate,
        memory=memory,
        shared_context=shared_context,
        per_symbol=per_symbol,
        allow_context_request=True,
        timeout_s=decision_timeout_s,
    )
    calls = 1
    decisions: dict[str, codex_client.Decision] = {}
    need: dict[str, codex_client.ContextResearchRequest] = {}
    for sym, resp in responses.items():
        if isinstance(resp, codex_client.ContextResearchRequest):
            need[sym] = resp
        else:
            decisions[sym] = resp

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
        responses2 = codex_client.decide_batch(
            symbols=list(need),
            mandate=mandate,
            memory=memory,
            shared_context=shared_context,
            per_symbol=per_symbol2,
            allow_context_request=False,
            timeout_s=decision_timeout_s,
        )
        calls += 1
        for sym in need:
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
            log.warning(
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
) -> dict:
    """Exécute UN cycle. Retourne un rapport structuré (machine-readable)."""
    now = now or datetime.now(timezone.utc)
    universe_cfg = _load_yaml(ROOT / "config" / "universe.yaml")
    risk_cfg = _load_yaml(ROOT / "config" / "risk.yaml")

    symbols: list[str] = universe_cfg["symbols"]
    symbols_to_decide = symbols
    if symbols_filter is not None:
        wanted = set(symbols_filter)
        symbols_to_decide = [symbol for symbol in symbols if symbol in wanted]
    starting_equity = float(universe_cfg.get("starting_cash", 100_000))
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

    broker = SimBroker(STATE_DIR / "broker.json", starting_cash=starting_equity)
    plan_store = TradePlanStore(STATE_DIR / "trade_plans.json")
    gate = RiskGate(RiskLimits.from_dict(risk_cfg))
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

    daily_bars_by_symbol: dict[str, list] = {}
    daily_max_age_minutes = max(max_market_data_age_minutes, COCKPIT_DAILY_MAX_AGE_MINUTES)
    for sym in tradable_symbols:
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
            freshness = market.assess_freshness(
                daily_bars,
                now=now,
                max_age_minutes=daily_max_age_minutes,
            )
        except Exception as exc:  # noqa: BLE001 - daily cockpit data is optional
            log.warning("daily freshness failed %s: %s", sym, exc)
            continue
        if not freshness.fresh:
            log.warning("daily data stale %s: %s", sym, freshness.reason)
            continue
        daily_bars_by_symbol[sym] = daily_bars

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

    snap = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity)
    gross = sum(abs(h.market_value) for h in snap.holdings)

    active_families = family_regime.families_for_universe(tradable_symbols)
    # Contexte cross-asset partagé : tout l'univers est visible à chaque décision
    # (l'edge de l'agent = relations entre symboles, pas un graphe isolé).
    cockpit = build_market_cockpit(
        tradable_bars_by_symbol,
        symbols=tradable_symbols,
        prices=tradable_prices,
        window=48,
        daily_bars_by_symbol=daily_bars_by_symbol,
    )
    base_context = {
        "now": now.isoformat(),
        "portfolio": snap.as_context(),
        "risk_limits": risk_cfg,
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
        "attribution": attribution.compute_attribution(STATE_DIR),
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
                for sym, bars in tradable_bars_by_symbol.items()
            },
            active_families,
        ),
    }

    report: dict = {
        "ts": now.isoformat(),
        "dry_run": dry_run,
        "code_version": code_version.current_code_version(ROOT),
        "symbols_due": symbols_to_decide,
        "planned_exits": planned_exits,
        "exit_watch_triggers": exit_watch_triggers,
        "indicator_triggers": indicator_triggers,
        "decisions": [],
        "portfolio": snap.as_context(),
        "prices": {s: round(p, 4) for s, p in prices.items()},
        "stale_market_data": stale_market_data,
        "model_calls_used": 0,
    }

    def refresh_report_portfolio() -> None:
        latest = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity)
        report["portfolio"] = latest.as_context()

    _write_current_report(report)
    mandate_txt, memory_txt = mem.read_mandate(), mem.read_memory()
    model_calls_used = 0

    def record_decision(decision_entry: dict) -> None:
        # Boucle de feedback : l'agent possède ses learnings ; l'infra les persiste
        # (machine-owned, borné) AVEC le résultat de la décision (executed/reason/
        # dry_run) pour qu'on puisse juger si l'agent fait les bons choix, puis les
        # réinjecte au prochain réveil.
        note = decision_entry.get("learning")
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

    # Batch stateless : UN appel modèle pour tous les symboles dus & frais (le
    # contexte partagé n'est envoyé qu'une fois, au lieu de N). Les symboles stale
    # / sans prix ne sont pas décidés (HOLD ci-dessous).
    decidable = [s for s in symbols_to_decide if s not in stale_market_data and s in prices]

    # Plans armés (D7 étage B) : un trigger EXECUTE_ORDER s'exécute SANS appel
    # LLM — le scénario a été validé à l'armement, le gate de risque déterministe
    # reste le fusible à l'exécution. Annulation si le prix au déclenchement a
    # déjà franchi le hard_stop (position instantanément stoppable) ou si stale.
    armed_decisions: dict[str, codex_client.Decision] = {}
    armed_plan_ids: dict[str, str] = {}
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
        elif not armed_order_price_coherent(order, price=prices[sym]):
            cancel_reason = "armed_plan_cancelled:stop_incoherent"
        if cancel_reason is not None:
            # Scénario invalidé = événement : on n'exécute pas en aveugle, on
            # réveille le planificateur AVEC le contexte (trigger annoté), il
            # re-décide (re-armer autrement, ou laisser).
            _log_cycle_progress("[armed_plan] %s %s plan=%s — réveil planificateur", sym, cancel_reason, plan_id)
            _append_event("armed_plan_cancelled", symbol=sym, plan_id=plan_id, reason=cancel_reason)
            trigger["armed_cancelled"] = cancel_reason
            continue
        armed_decisions[sym] = codex_client.Decision(
            symbol=sym,
            action=str(order["action"]),
            quantity=float(order["qty"]),
            confidence=float(order["confidence"]),
            rationale=f"armed_plan:{plan_id} — {order.get('rationale') or ''}".strip(" —"),
            intent=str(order["intent"]),
            exit_plan=order.get("exit_plan"),
        )
        armed_plan_ids[sym] = plan_id
        _log_cycle_progress("[armed_plan] %s déclenché plan=%s — exécution sans LLM", sym, plan_id)
    # les symboles armés ont déjà leur décision : pas d'appel LLM, pas de gate
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
    held_symbols = {h.symbol for h in snap.holdings if h.quantity}
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
    decisions_by_symbol, model_calls_used = _batch_decide(
        decidable=decidable,
        mandate=mandate_txt,
        memory=memory_txt,
        shared_context=base_context,
        triggers_by_symbol=triggers_by_symbol,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
        tradable_symbols=tradable_symbols,
        runtime_interval=runtime_interval,
        runtime_lookback=runtime_lookback,
        max_context_requests_per_symbol=max_context_requests_per_symbol,
        max_indicators_per_request=max_indicators_per_request,
        max_model_calls=max_model_calls_per_cycle,
        now=now,
        data_age_by_symbol=data_age_by_symbol,
        decision_timeout_s=decision_timeout_s,
    )
    # revue effective seulement si le modèle a réellement statué (review Codex :
    # un échec/budget à 0 ne doit pas compter comme revue périodique)
    for sym in decidable:
        if sym in decisions_by_symbol:
            _LAST_LLM_AT[(str(STATE_DIR), sym)] = now
    if armed_decisions:
        decisions_by_symbol = {**decisions_by_symbol, **armed_decisions}
    _log_cycle_progress("[batch] decided=%d model_calls=%d", len(decisions_by_symbol), model_calls_used)

    gated_set = set(gated_symbols)
    for index, sym in enumerate(symbols_to_decide, start=1):
        if sym in gated_set:
            # Déjà tracé quiet_gate — ne pas générer un second HOLD
            # "no_decision_in_batch" (doublon ledger, faux HOLD agent).
            # NB : un plan armé annulé n'est PAS ici — il passe au LLM (voulu).
            continue
        stale_data = stale_market_data.get(sym)
        if stale_data is not None:
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
            if is_first_stale:
                _log_cycle_progress(
                    "[decision %d/%d] %s stale_market_data (streak=1) reason=%s age=%s wake=%.0fmin",
                    index,
                    len(symbols_to_decide),
                    sym,
                    stale_data.get("stale_reason"),
                    stale_data.get("data_age_minutes"),
                    wake_minutes,
                )
                record_decision(
                    {
                        "symbol": sym,
                        "action": "HOLD",
                        "qty": 0.0,
                        "confidence": 0.0,
                        "rationale": "stale_market_data",
                        "next_wake_in_minutes": wake_minutes,
                        "intent": "HOLD",
                        "trade_plan_created": False,
                        "executed": False,
                        "reason": "stale_market_data",
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
        _log_cycle_progress(
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

        entry = {"symbol": sym, "action": decision.action, "qty": effective_quantity,
                 **({"armed_plan_id": armed_plan_ids[sym]} if sym in armed_plan_ids else {}),
                 "confidence": decision.confidence, "rationale": decision.rationale,
                 "next_wake_in_minutes": next_wake_in_minutes,
                 "next_wake_requested": decision.next_wake_in_minutes,
                 "context_request": decision.context_request,
                 "intent": decision.intent,
                 "llm_provider": decision.llm_provider,
                 "llm_model": decision.llm_model,
                 "llm_fallback_reason": decision.llm_fallback_reason,
                 "llm_error": decision.llm_error,
                 "learning": decision.learning,
                 "trade_plan_created": False,
                 "indicator_watch_created": False,
                 "indicator_watch_requested": bool(decision.indicator_watch),
                 "indicator_watch_rejections": [],
                 "data_source": runtime_data_source_by_sym.get(sym)}

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
            if sched is None:
                return
            if next_wake_in_minutes is not None:
                sched.set_symbol_next_wake_in(sym, minutes=next_wake_in_minutes, now=now)
            else:
                sched.clear_symbol_next_wake(sym)
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

        def apply_default_schedule_after_blocked() -> None:
            if sched is not None:
                sched.clear_symbol_next_wake(sym)

        if decision.action == "HOLD" or effective_quantity == 0:
            _log_cycle_progress("[decision %d/%d] %s hold", index, len(symbols_to_decide), sym)
            apply_decision_schedule()
            record_decision({**entry, "executed": False, "reason": "hold"})
            continue

        invalid_intent = _invalid_intent_reason(decision)
        if invalid_intent is not None:
            _log_cycle_progress("[decision %d/%d] %s blocked %s", index, len(symbols_to_decide), sym, invalid_intent)
            apply_default_schedule_after_blocked()
            record_decision({**entry, "executed": False, "reason": invalid_intent})
            continue

        if decision.exit_plan and decision.intent in {"OPEN_LONG", "OPEN_SHORT", "REVERSE"}:
            try:
                validate_exit_plan(decision.exit_plan)
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
            hard_stop_price = _hard_stop_price(decision.exit_plan)
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
            hard_stop_price = _hard_stop_price(decision.exit_plan)
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
                if pure_open:
                    # Guardrail humain rendu déterministe (mandate/guardrails.json,
                    # D6 du registre) : pas d'ouverture sans hard_stop, quelle que
                    # soit la confiance. REVERSE reste tracé non bloqué (dette connue).
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
                if pure_open:
                    max_risk_quantity = gate.max_quantity_at_risk(
                        snap.equity,
                        prices[sym],
                        hard_stop_price,
                    )
                    if effective_quantity > max_risk_quantity:
                        entry.setdefault("requested_qty", effective_quantity)
                        effective_quantity = max_risk_quantity
                        entry["qty"] = effective_quantity
                        entry["risk_clamped"] = True
                    risk_quantity = effective_quantity
                _set_entry_risk_metrics(
                    entry,
                    quantity=risk_quantity,
                    stop_distance=open_stop_distance,
                    equity=snap.equity,
                )

            if pure_open and effective_quantity == 0:
                _log_cycle_progress("[decision %d/%d] %s hold zero_risk_quantity", index, len(symbols_to_decide), sym)
                apply_default_schedule_after_blocked()
                record_decision({**entry, "executed": False, "reason": "zero_risk_quantity"})
                continue

            if pure_open:
                # Gate de confiance adapté au risque (ouvertures pures uniquement).
                # REDUCE/CLOSE/REVERSE : réduire le risque doit toujours rester possible.
                # Note : le check s'effectue AVANT le clamp max_order_value (plus bas).
                # Si le clamp réduit ensuite la quantité, le risque réel sera plus faible
                # que celui utilisé ici. Direction conservatrice (rejet plus fréquent, jamais
                # plus permissif) — acceptable pour un fusible.
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
        cur_pos_value = (pos.quantity * prices[sym]) if pos else 0.0
        allow_risk_reduction = decision.intent in {"REDUCE", "CLOSE"}
        allow_order_value_clamp = decision.intent in {"OPEN_LONG", "OPEN_SHORT"}
        verdict = gate.check(
            order,
            prices[sym],
            current_position_value=cur_pos_value,
            gross_exposure=gross,
            equity=snap.equity,
            allow_risk_reduction=allow_risk_reduction,
        )
        if not verdict.approved and verdict.code == "order_value_exceeded" and allow_order_value_clamp:
            clamped_quantity = min(effective_quantity, gate.max_order_quantity_at_price(prices[sym]))
            if 0 < clamped_quantity < effective_quantity:
                entry.setdefault("requested_qty", effective_quantity)
                effective_quantity = clamped_quantity
                entry["qty"] = effective_quantity
                if pure_open:
                    _set_entry_risk_metrics(
                        entry,
                        quantity=effective_quantity,
                        stop_distance=open_stop_distance,
                        equity=snap.equity,
                    )
                order = Order(symbol=sym, side=decision.action, quantity=effective_quantity, rationale=decision.rationale)
                verdict = gate.check(
                    order,
                    prices[sym],
                    current_position_value=cur_pos_value,
                    gross_exposure=gross,
                    equity=snap.equity,
                    allow_risk_reduction=allow_risk_reduction,
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

        fill = broker.submit(order, prices[sym], now.isoformat(), dry_run=dry_run)
        if not dry_run:
            gate.record_pass()
            gross = _gross_exposure(broker, prices)
            if fill is not None:
                latest = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity)
                final_position = broker.positions().get(sym)
                _append_model_performance(
                    ts=fill.ts,
                    symbol=sym,
                    action=decision.action,
                    intent=decision.intent,
                    quantity=effective_quantity,
                    price=prices[sym],
                    confidence=decision.confidence,
                    llm_provider=decision.llm_provider or "unknown",
                    llm_model=decision.llm_model or "unknown",
                    llm_fallback_reason=decision.llm_fallback_reason,
                    equity=latest.equity,
                    cash=latest.cash,
                    position_quantity=0.0 if final_position is None else final_position.quantity,
                )
                entry["model_performance_logged"] = True
            if fill is not None and decision.intent in {"CLOSE", "REVERSE"}:
                plan_store.close_symbol(sym)
            if fill is not None and decision.intent == "REDUCE":
                final_position = broker.positions().get(sym)
                plan_store.sync_symbol_quantity(sym, 0.0 if final_position is None else abs(final_position.quantity))
            if (
                fill is not None
                and decision.exit_plan
                and decision.intent in {"OPEN_LONG", "OPEN_SHORT", "REVERSE"}
            ):
                if decision.intent == "REVERSE":
                    created_plan = _create_plan_for_final_position(
                        broker=broker,
                        plan_store=plan_store,
                        symbol=sym,
                        price=prices[sym],
                        opened_at=fill.ts,
                        raw_exit_plan=decision.exit_plan,
                        llm_provider=decision.llm_provider,
                        llm_model=decision.llm_model,
                        llm_fallback_reason=decision.llm_fallback_reason,
                        llm_confidence=decision.confidence,
                    )
                    entry["trade_plan_created"] = created_plan is not None
                    if created_plan is not None:
                        entry["trade_plan"] = _plan_snapshot(created_plan)
                else:
                    plan = create_trade_plan_from_order(
                        symbol=sym,
                        order_side=decision.action,
                        quantity=effective_quantity,
                        entry_price=prices[sym],
                        opened_at=fill.ts,
                        raw_exit_plan=decision.exit_plan,
                        llm_provider=decision.llm_provider,
                        llm_model=decision.llm_model,
                        llm_fallback_reason=decision.llm_fallback_reason,
                        llm_confidence=decision.confidence,
                    )
                    plan_store.upsert(plan)
                    entry["trade_plan_created"] = True
                    entry["trade_plan"] = _plan_snapshot(plan)
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
    )
    if consolidation_result.get("triggered"):
        report["learning_consolidation"] = consolidation_result
        _write_current_report(report)
        _append_event("learning_consolidated", **consolidation_result)
    return report


def main(argv: list[str] | None = None) -> None:
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
        "--data-profile",
        default=os.getenv("TRADER_DATA_PROFILE"),
        help="profil de routing data (paper|prod). Override config/data_sources.yaml. Env: TRADER_DATA_PROFILE",
    )
    args = parser.parse_args(argv)

    from .logging_setup import setup_logging
    setup_logging(level=logging.INFO)
    dry_run = not args.live

    # Identité daemon : revendiquer le pid file en premier (avant tout _write_status).
    # Refus si un daemon vivant le détient déjà — un doublon qui écrase puis supprime
    # daemon.pid à son arrêt rend le daemon légitime inarrêtable depuis le cockpit.
    from .cockpit_supervisor import claim_pid_file, release_pid_file

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _pid_file = STATE_DIR / "daemon.pid"
    if not claim_pid_file(pid_file=_pid_file, pid=os.getpid()):
        log.error("daemon déjà vivant (pid file %s) — refus de démarrer un doublon", _pid_file)
        return 1

    sched = scheduler.Scheduler(STATE_DIR / "scheduler.json")
    log.info("daemon démarré (dry_run=%s, once=%s)", dry_run, args.once)
    bootstrap = args.bootstrap_all

    data_source = None
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
                            _ib_source = IBDataSource(
                                _ib_obj,
                                reconnect_factory=lambda: connect_ib(
                                    args.ib_host, args.ib_port, args.ib_client_id,
                                    market_data_type=_mdt,
                                ),
                            )
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
                loop_now = datetime.now(timezone.utc)
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
                    )
                    if report.get("planned_exits") or report.get("decisions") or report.get("exit_watch_triggers"):
                        log.info("cycle actif sans symbole dû: %s", json.dumps(report, ensure_ascii=False))
                        STATE_DIR.mkdir(parents=True, exist_ok=True)
                        (STATE_DIR / "last_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
                        _append_cycle_history(report)
                    wait = sched.seconds_until_wake(symbols)
                    sleep_seconds = min(wait, args.poll)
                    log.info("aucun symbole dû — pause %.0fs", sleep_seconds)
                else:
                    report = run_cycle(
                        dry_run=dry_run,
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
                    )
                    log.info("cycle: %s", json.dumps(report, ensure_ascii=False))
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
                time.sleep(sleep_seconds)
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
