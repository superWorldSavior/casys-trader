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
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .agent_context import build_market_cockpit, resolve_indicator_requests
from . import attribution, codex_client, stats
from .exit_engine import evaluate_plan
from .features import DEFAULT_INDICATORS
from .indicator_watch import (
    evaluate_indicator_watches,
    normalize_indicator_watch,
    watch_market_requests,
)
from .risk import RiskGate, RiskLimits
from .tools import market, memory as memory_mod, portfolio, scheduler
from .tools.execution import Order, SimBroker
from .trade_plan import (
    InvalidExitPlanError,
    TradePlanStore,
    create_trade_plan,
    create_trade_plan_from_order,
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


def _write_json_state(filename: str, payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / filename).write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def _write_status(phase: str, **payload: object) -> None:
    _write_json_state(
        "daemon_status.json",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
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


def _kill_switch_active() -> bool:
    return (ROOT / "KILL").exists()


def _bounded_wake_minutes(value: float, *, minimum: float, maximum: float) -> float:
    return min(max(float(value), minimum), maximum)


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
) -> bool:
    position = broker.positions().get(symbol)
    if position is None or position.quantity == 0:
        return False
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
    return True


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


def _apply_planned_exits(
    *,
    broker: SimBroker,
    plan_store: TradePlanStore,
    prices: dict[str, float],
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
        evaluation = evaluate_plan(plan, price=price, now=now)
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
            entries.append(
                {
                    "symbol": plan.symbol,
                    "side": evaluation.signal.side,
                    "quantity": clamped_quantity,
                    "requested_quantity": requested_quantity,
                    "reason": exit_block_reason or "zero_exit_quantity",
                    "price": price,
                    "executed": False,
                    "dry_run": dry_run,
                }
            )
            continue

        order = Order(
            symbol=evaluation.signal.symbol,
            side=evaluation.signal.side,
            quantity=clamped_quantity,
            rationale=evaluation.signal.reason,
        )
        fill = broker.submit(order, price, now.isoformat(), dry_run=dry_run)
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
                    price=price,
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
            bars_by_key[key] = market.get_bars(symbol, lookback=lookback, interval=interval)
        except market.MarketError as exc:
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
            bars_by_key[(symbol, interval)] = market.get_bars(symbol, lookback=lookback, interval=interval)
        except market.MarketError as exc:
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


def run_cycle(
    *,
    dry_run: bool,
    now: datetime | None = None,
    symbols_filter: list[str] | None = None,
    sched: scheduler.Scheduler | None = None,
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
    learnings_store = memory_mod.LearningsStore(STATE_DIR / "learnings.jsonl")

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
    _log_cycle_progress("[market] loading bars symbols=%d interval=%s", len(symbols), runtime_interval)
    for sym in symbols:
        try:
            bars = market.get_bars(sym, lookback=runtime_lookback, interval=runtime_interval)
        except market.MarketError as e:
            log.warning("données indisponibles %s: %s", sym, e.code)
            continue
        if not bars:  # défensif : get_bars lève normalement sur vide
            log.warning("données vides %s", sym)
            continue
        bars_by_symbol[sym] = bars
        prices[sym] = bars[-1].close
        # La fraîcheur EST le garde « marché live » : une dernière barre trop
        # vieille / imparsable => stale => exclue du tradable (pas de fill sur
        # données mortes hors-séance ou gelées).
        freshness = market.assess_freshness(bars, now=now, max_age_minutes=max_market_data_age_minutes)
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

    tradable_prices = {symbol: price for symbol, price in prices.items() if symbol not in stale_market_data}
    tradable_symbols = [symbol for symbol in symbols if symbol not in stale_market_data]
    tradable_bars_by_symbol = {
        symbol: bars
        for symbol, bars in bars_by_symbol.items()
        if symbol not in stale_market_data
    }

    planned_exits = _apply_planned_exits(
        broker=broker,
        plan_store=plan_store,
        prices=tradable_prices,
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

    # Contexte cross-asset partagé : tout l'univers est visible à chaque décision
    # (l'edge de l'agent = relations entre symboles, pas un graphe isolé).
    cockpit = build_market_cockpit(
        tradable_bars_by_symbol,
        symbols=tradable_symbols,
        prices=tradable_prices,
        window=48,
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
        # Boucle de feedback : les learnings que l'agent a écrits aux réveils
        # précédents (store machine-owned, borné), réinjectés pour qu'il s'appuie
        # sur ses propres observations.
        "learnings": learnings_store.recent(limit=max_learnings_in_context),
    }

    report: dict = {
        "ts": now.isoformat(),
        "dry_run": dry_run,
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
        report["decisions"].append(decision_entry)
        report["model_calls_used"] = model_calls_used
        refresh_report_portfolio()
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

    for index, sym in enumerate(symbols_to_decide, start=1):
        stale_data = stale_market_data.get(sym)
        if stale_data is not None:
            _log_cycle_progress(
                "[decision %d/%d] %s skipped stale_market_data reason=%s age=%s",
                index,
                len(symbols_to_decide),
                sym,
                stale_data.get("stale_reason"),
                stale_data.get("data_age_minutes"),
            )
            if sched is not None:
                sched.set_symbol_next_wake_in(sym, minutes=default_wake_minutes, now=now)
            record_decision(
                {
                    "symbol": sym,
                    "action": "HOLD",
                    "qty": 0.0,
                    "confidence": 0.0,
                    "rationale": "stale_market_data",
                    "next_wake_in_minutes": default_wake_minutes,
                    "intent": "HOLD",
                    "trade_plan_created": False,
                    "executed": False,
                    "reason": "stale_market_data",
                    **stale_data,
                }
            )
            continue
        if sym not in prices:
            _log_cycle_progress("[decision %d/%d] %s skipped no_price", index, len(symbols_to_decide), sym)
            continue
        ctx = {
            **base_context,
            "symbol": sym,
            "indicator_triggers": triggers_by_symbol.get(sym, []),
        }  # le symbole à décider ce tour
        if model_calls_used >= max_model_calls_per_cycle:
            _log_cycle_progress(
                "[decision %d/%d] %s skipped model_call_budget_exhausted (%d/%d)",
                index,
                len(symbols_to_decide),
                sym,
                model_calls_used,
                max_model_calls_per_cycle,
            )
            entry = {
                "symbol": sym,
                "action": "HOLD",
                "qty": 0.0,
                "confidence": 0.0,
                "rationale": "model_call_budget_exhausted",
                "next_wake_in_minutes": None,
                "intent": "HOLD",
                "trade_plan_created": False,
                "executed": False,
                "reason": "model_call_budget_exhausted",
            }
            record_decision(entry)
            continue

        _log_cycle_progress(
            "[decision %d/%d] %s start price=%s calls=%d/%d",
            index,
            len(symbols_to_decide),
            sym,
            round(prices[sym], 6),
            model_calls_used,
            max_model_calls_per_cycle,
        )
        _write_status(
            "deciding_symbol",
            current_symbol=sym,
            decisions_done=len(report["decisions"]),
            symbols_total=len(symbols_to_decide),
            model_calls_used=model_calls_used,
            max_model_calls_per_cycle=max_model_calls_per_cycle,
        )
        _append_event("deciding_symbol", symbol=sym)
        response = codex_client.decide(
            symbol=sym,
            mandate=mandate_txt,
            memory=memory_txt,
            context=ctx,
            allow_context_request=True,
        )
        model_calls_used += 1
        if isinstance(response, codex_client.ContextResearchRequest):
            _log_cycle_progress(
                "[decision %d/%d] %s requested_context n=%d calls=%d/%d",
                index,
                len(symbols_to_decide),
                sym,
                len(response.requests),
                model_calls_used,
                max_model_calls_per_cycle,
            )
            _write_status(
                "resolving_context",
                current_symbol=sym,
                decisions_done=len(report["decisions"]),
                symbols_total=len(symbols_to_decide),
                model_calls_used=model_calls_used,
                requested=len(response.requests),
            )
            research = resolve_indicator_requests(
                response.requests,
                tradable_bars_by_symbol,
                symbols=tradable_symbols,
                max_requests=max_context_requests_per_symbol,
                max_indicators=max_indicators_per_request,
                cached_interval=runtime_interval,
                cached_lookback=runtime_lookback,
            )
            _append_event(
                "context_resolved",
                symbol=sym,
                requested=len(response.requests),
                resolved=len(research["requests"]),
            )
            if model_calls_used >= max_model_calls_per_cycle:
                decision = codex_client.Decision.hold(sym, "model_call_budget_exhausted_after_context")
            else:
                ctx = {
                    **base_context,
                    "symbol": sym,
                    "indicator_triggers": triggers_by_symbol.get(sym, []),
                    "research": research,
                    # Sessions jetables : le 2e exec n'a pas l'historique du 1er.
                    # On lui repasse la rationale de la demande pour qu'il reprenne
                    # son fil de raisonnement au lieu de repartir de zéro.
                    "prior_rationale": response.rationale,
                }
                _log_cycle_progress(
                    "[decision %d/%d] %s final_call research=%d",
                    index,
                    len(symbols_to_decide),
                    sym,
                    len(research["requests"]),
                )
                response = codex_client.decide(
                    symbol=sym,
                    mandate=mandate_txt,
                    memory=memory_txt,
                    context=ctx,
                    allow_context_request=False,
                )
                model_calls_used += 1
                decision = response if isinstance(response, codex_client.Decision) else codex_client.Decision.hold(sym, "context_loop_blocked")
        else:
            decision = response
        _log_cycle_progress(
            "[decision %d/%d] %s result action=%s qty=%s intent=%s wake=%s confidence=%.2f provider=%s model=%s fallback=%s calls=%d/%d",
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
            model_calls_used,
            max_model_calls_per_cycle,
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
                 "confidence": decision.confidence, "rationale": decision.rationale,
                 "next_wake_in_minutes": next_wake_in_minutes,
                 "intent": decision.intent,
                 "llm_provider": decision.llm_provider,
                 "llm_model": decision.llm_model,
                 "llm_fallback_reason": decision.llm_fallback_reason,
                 "llm_error": decision.llm_error,
                 "learning": decision.learning,
                 "trade_plan_created": False,
                 "indicator_watch_created": False}

        pending_indicator_watch = None
        if decision.indicator_watch and sched is not None:
            pending_indicator_watch = normalize_indicator_watch(decision.indicator_watch, owner_symbol=sym, now=now)

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

        order = Order(symbol=sym, side=decision.action, quantity=effective_quantity, rationale=decision.rationale)
        cur_pos_value = (pos.quantity * prices[sym]) if pos else 0.0
        verdict = gate.check(
            order,
            prices[sym],
            current_position_value=cur_pos_value,
            gross_exposure=gross,
            equity=snap.equity,
            allow_risk_reduction=decision.intent in {"REDUCE", "CLOSE"},
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
                    entry["trade_plan_created"] = _create_plan_for_final_position(
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
    parser.add_argument("--bootstrap-all", action="store_true", help="ignore les timers au démarrage et force tous les symboles")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry_run = not args.live
    sched = scheduler.Scheduler(STATE_DIR / "scheduler.json")
    log.info("daemon démarré (dry_run=%s, once=%s)", dry_run, args.once)
    bootstrap = args.bootstrap_all

    while True:
        loop_now = datetime.now(timezone.utc)
        symbols = _load_yaml(ROOT / "config" / "universe.yaml")["symbols"]
        indicator_triggers = [] if args.once or bootstrap else _scan_indicator_watches(symbols, sched=sched, now=loop_now)
        due_symbols = _select_due_symbols(symbols, sched=sched, once=args.once, bootstrap=bootstrap, now=loop_now)
        bootstrap = False
        if not due_symbols:
            report = run_cycle(
                dry_run=dry_run,
                symbols_filter=[],
                sched=sched,
                default_wake_minutes=args.default_wake_minutes,
                min_wake_minutes=args.min_wake_minutes,
                max_wake_minutes=args.max_wake_minutes,
                max_context_requests_per_symbol=args.max_context_requests_per_symbol,
                max_indicators_per_request=args.max_indicators_per_request,
                max_model_calls_per_cycle=args.max_model_calls_per_cycle,
                indicator_triggers=indicator_triggers,
            )
            if report.get("planned_exits") or report.get("decisions") or report.get("exit_watch_triggers"):
                log.info("cycle actif sans symbole dû: %s", json.dumps(report, ensure_ascii=False))
                (STATE_DIR / "last_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
                _append_cycle_history(report)
            wait = sched.seconds_until_wake(symbols)
            log.info("aucun symbole dû — pause %.0fs", min(wait, args.poll))
            time.sleep(min(wait, args.poll))
            continue

        report = run_cycle(
            dry_run=dry_run,
            symbols_filter=due_symbols,
            sched=sched,
            default_wake_minutes=args.default_wake_minutes,
            min_wake_minutes=args.min_wake_minutes,
            max_wake_minutes=args.max_wake_minutes,
            max_context_requests_per_symbol=args.max_context_requests_per_symbol,
            max_indicators_per_request=args.max_indicators_per_request,
            max_model_calls_per_cycle=args.max_model_calls_per_cycle,
            indicator_triggers=indicator_triggers,
        )
        log.info("cycle: %s", json.dumps(report, ensure_ascii=False))
        (STATE_DIR / "last_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

        # Historique d'équité : une ligne JSON compacte par cycle actif (append).
        _append_cycle_history(report)

        if args.once:
            break

        wait = sched.seconds_until_wake(symbols)
        log.info("[sleep] next_check_in=%.0fs next_due_in=%.0fs", min(wait, args.poll), wait)
        time.sleep(min(wait, args.poll))


if __name__ == "__main__":
    main()
