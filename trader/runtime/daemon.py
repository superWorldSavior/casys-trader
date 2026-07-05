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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from trader.agent import client as codex_client
from trader.agent import llm
from trader.agent import memory as agent_memory
from trader.agent.context import build_market_cockpit, resolve_indicator_requests
from trader.application import (
    amend_exit as amend_exit_service,
    armed_plans,
    confidence_feedback,
    decision_entries,
    decision_watches,
    entry_context,
    execute_queue_dispatch,
    execute_queue_plan,
    execution_eligibility as execution_eligibility_service,
    fill_outcome,
    fill_plan_effects,
    exit_bars as exit_bars_service,
    gross_feedback,
    infra_holds,
    learnings_recall,
    market_snapshot,
    order_admission,
    plan_review,
    planned_exits as planned_exits_service,
    planner_batch,
    reference_volatility as reference_volatility_service,
    risk_admission,
    risk_capacity,
)
from trader.application.decision_recorder import DecisionRecorder
from trader.execution.risk import RiskGate, RiskLimits
from trader.agent.learnings import consolidator
from trader.agent.learnings import raw_store as raw_learnings
from trader.agent.learnings import store as recall_store_mod
from trader.market import family_regime, fx, macro_calendar, macro_series
from trader.market import market_data as market
from trader.market.data_source import (
    CompositeDataSource,
    YFinanceDataSource,
    parse_data_sources_config,
)
from trader.market.features import DEFAULT_INDICATORS
from trader.market.gross_priority import PriorityItem, gross_execution_order
from trader.market.ib_source import IBDataSource, connect_ib
from trader.market import news_feed
from trader.planning.trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    TradePlanStore,
    resolve_exit_plan,
    validate_exit_plan,
)
from trader.support.metadata import code_version
from trader.reporting.read_models import attribution, live_kpis, meta_performance
from trader.reporting.ledger import decision_ledger
from trader.runtime import (
    cycle_finalization,
    cycle_dispatch,
    cycle_reporting,
    cycle_scheduling,
    data_source_runtime,
    daemon_bootstrap,
    market_rotation_runtime,
    queue_runtime,
    runtime_shutdown,
)
from trader.runtime.ib_attach import IBAttachBackoff
from trader.runtime.state_writer import RuntimeStateWriter
from trader.planning import scheduler
from trader.execution import portfolio
from trader.execution.contracts import Order
from trader.execution.broker import (
    SimBroker,
    commission_model_from_name,
    round_trip_cost,
)
from trader.execution.ports import CommissionModel
from trader.infrastructure.state_db.broker_factory import (
    bootstrap_state_backend,
    make_broker,
    make_scheduler,
    make_trade_plan_store,
)

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"

log = logging.getLogger("casys-trader")

# Drapeau d'échec définitif du store recall (db corrompu/verrouillé à l'ouverture).
# Posé à True dès le premier échec ; ne pas réessayer à chaque cycle pour éviter
# de logguer la même erreur indéfiniment. Réinitialisable dans les tests.
_RECALL_STORE_FAILED: bool = False

_OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "REVERSE", "ADD"}
_PURE_OPEN_INTENTS = {"OPEN_LONG", "OPEN_SHORT"}
_RISK_GUARDED_OPENING_INTENTS = {"OPEN_LONG", "OPEN_SHORT", "ADD"}
_RELATIVE_ORDER_INTENTS = order_admission.RELATIVE_ORDER_INTENTS


def _llm_exit_reason_for_intent(intent: str) -> str | None:
    """Libellé déterministe pour les sorties pilotées par le LLM."""
    return fill_outcome.llm_exit_reason_for_intent(intent)


def evaluate_plan(*args: object, **kwargs: object) -> object:
    """Legacy daemon monkeypatch hook for planned-exit evaluation."""
    return planned_exits_service.evaluate_plan(*args, **kwargs)


# Budget de polling pour execute_order via la file (secondes).
# L'exécution est locale et synchrone (pool in-process) — 10 s est généreux.
_EXECUTE_POLL_BUDGET_S: float = 10.0


@dataclass
class DecisionExecutionState:
    snap: object
    gross: float


@dataclass(frozen=True)
class DecisionExecutionContext:
    now: datetime
    min_wake_minutes: float | None
    max_wake_minutes: float | None
    macro_next: dict | None
    broker: object
    plan_store: object
    gate: RiskGate
    sched: scheduler.Scheduler | None
    prices: dict[str, float]
    execution_eligibility: dict[str, dict]
    tradable_bars_by_symbol: dict[str, list]
    data_age_by_symbol: dict[str, float]
    runtime_data_source_by_sym: dict[str, object]
    armed_plan_ids: dict[str, str]
    armed_plan_orders: dict[str, dict]
    armed_reference_volatilities: dict[str, float | None]
    held_symbols: set[str]
    cockpit: dict
    runtime_interval: str
    starting_equity: float
    require_hard_stop: bool
    dry_run: bool
    queue_execute_enabled: bool
    execute_ledger: object
    record_decision: Callable[[dict], None]
    rate_for_symbol: Callable[[str], float]


def summarize_gross_rejections(decisions: list[dict]) -> dict | None:
    """Résume les ouvertures recalées faute de marge gross sur un cycle.

    Feedback léger réinjecté au cycle suivant (les sessions LLM sont stateless) :
    l'agent voit qu'il a collectivement sur-proposé contre le plafond gross
    partagé et peut être plus sélectif. Retourne None s'il n'y a rien à signaler.
    """
    return gross_feedback.summarize_gross_rejections(decisions)


def _counts_as_llm_review(decision: codex_client.Decision) -> bool:
    return decision_entries.counts_as_llm_review(decision)


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
DEFAULT_DECISION_BATCH_SIZE = planner_batch.DEFAULT_DECISION_BATCH_SIZE
DEFAULT_DECISION_BATCH_PARALLELISM = planner_batch.DEFAULT_DECISION_BATCH_PARALLELISM

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


def _runtime_state_writer() -> RuntimeStateWriter:
    return RuntimeStateWriter(STATE_DIR)


def _write_json_state(filename: str, payload: dict) -> None:
    _runtime_state_writer().write_json_state(filename, payload)


def _write_status(phase: str, **payload: object) -> None:
    _runtime_state_writer().write_status(phase, **payload)


def _write_current_report(report: dict) -> None:
    _runtime_state_writer().write_current_report(report)


def _append_event(event: str, **payload: object) -> None:
    _runtime_state_writer().append_event(event, **payload)


def _append_model_performance(**payload: object) -> None:
    _runtime_state_writer().append_model_performance(**payload)


def _append_cycle_history(report: dict) -> None:
    _runtime_state_writer().append_cycle_history(report)


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


def _bounded_wake_minutes(
    value: float, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    """Réveil demandé par l'agent, borné SEULEMENT si une borne est fournie.

    Par défaut (minimum/maximum=None) la valeur passe intacte : l'agent est
    autonome sur sa cadence de re-décision (les sorties restent vérifiées à
    chaque poll, indépendamment). Les flags --min/--max-wake-minutes réactivent
    un bornage si besoin.
    """
    return cycle_scheduling.bounded_wake_minutes(value, minimum=minimum, maximum=maximum)


def _stale_backoff_wake_minutes(streak: int, *, default_wake_minutes: float) -> float:
    """Next-wake pour un symbole stale avec backoff exponentiel.

    Tous les chemins sont cappés à STALE_BACKOFF_MAX_MINUTES, y compris streak=0.
    Cela évite qu'un --default-wake-minutes élevé (ex. 240) dépasse le cap.

    streak=0 → min(default, cap)
    streak=N → min(default * 2^N, cap)

    Le streak est borné à STALE_BACKOFF_MAX_STREAK avant appel (voir Scheduler)
    pour éviter tout OverflowError sur 2**streak.

    Constantes dans trader/planning/scheduler.py :
      STALE_BACKOFF_BASE_MULTIPLIER = 2
      STALE_BACKOFF_MAX_MINUTES = 120.0
      STALE_BACKOFF_MAX_STREAK = 8
    """
    return cycle_scheduling.stale_backoff_wake_minutes(
        streak,
        default_wake_minutes=default_wake_minutes,
    )


def _ensure_default_wake(
    sched: scheduler.Scheduler,
    *,
    now: datetime,
    default_wake_minutes: float,
) -> None:
    cycle_scheduling.ensure_default_wake(
        sched,
        now=now,
        default_wake_minutes=default_wake_minutes,
    )


def _select_due_symbols(
    symbols: list[str],
    *,
    sched: scheduler.Scheduler,
    once: bool,
    bootstrap: bool,
    now: datetime | None = None,
) -> list[str]:
    return cycle_scheduling.select_due_symbols(
        symbols,
        sched=sched,
        once=once,
        bootstrap=bootstrap,
        now=now,
    )


def _invalid_intent_reason(decision: codex_client.Decision) -> str | None:
    return order_admission.invalid_intent_reason(
        action=decision.action,
        quantity=decision.quantity,
        intent=decision.intent,
    )


def _resolve_position_aware_decision(
    decision: codex_client.Decision,
    position_quantity: float,
) -> codex_client.Decision:
    """Dérive action + qty depuis la position pour CLOSE/REDUCE/REVERSE/ADD sans side.

    Fail-safe absolu : position_quantity == 0 → HOLD.
      - CLOSE/REDUCE/REVERSE sans position → 'nothing_to_close'
      - ADD sans position → 'add_without_position'
    Les intents relatifs repassent toujours par cette dérivation, même si le
    Decision a été construit à la main avec resolve_from_position=False.

    Side :
      - CLOSE/REDUCE/REVERSE → côté OPPOSÉ à la position (clôture/retournement).
      - ADD → côté IDENTIQUE à la position (renforcement dans le même sens).
    """
    return order_admission.resolve_position_aware_decision(decision, position_quantity)


def _resolve_decision_for_execution_routing(
    *,
    symbol: str,
    decision: codex_client.Decision,
    broker: object,
) -> codex_client.Decision:
    """Résout les intents relatifs avant routage stream/buffer."""
    if decision.resolve_from_position or decision.intent in _RELATIVE_ORDER_INTENTS:
        raw_pos = broker.positions().get(symbol)
        pos_qty = raw_pos.quantity if raw_pos is not None else 0.0
        return _resolve_position_aware_decision(decision, pos_qty)
    return decision


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
    return confidence_feedback.merge_gate_feedback(reason, context, note)


def _gross_exposure(
    broker: SimBroker,
    prices: dict[str, float],
    rate_of: Callable[[str], float] | None = None,
) -> float:
    return risk_capacity.gross_exposure(broker, prices, rate_of=rate_of)


def _finite_positive(value: float | None) -> bool:
    return risk_capacity.finite_positive(value)


def _side_capacity_usd(
    *,
    side: str,
    current_position_value: float,
    gross_exposure: float,
    limits: RiskLimits,
    equity: float,
) -> float:
    return risk_capacity.side_capacity_usd(
        side=side,
        current_position_value=current_position_value,
        gross_exposure=gross_exposure,
        limits=limits,
        equity=equity,
    )


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
    return risk_capacity.risk_capacity_context(
        symbols=symbols,
        prices=prices,
        broker=broker,
        gross_exposure=gross_exposure,
        limits=limits,
        equity=equity,
        rate_of=rate_of,
        currency_of=fx.currency_for,
    )


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
    return order_admission.hard_stop_price(raw_exit_plan)


def _hard_stop_wrong_side(
    intent: str | None,
    entry_price: float,
    stop_price: float,
    *,
    action: str | None = None,
) -> bool:
    return order_admission.hard_stop_wrong_side(intent, entry_price, stop_price, action=action)


def _runtime_tool_audit_fields(domain_tools: dict | None) -> dict:
    return decision_entries.runtime_tool_audit_fields(domain_tools)

def _clamp_exit_quantity(
    *,
    intent: str | None,
    action: str,
    quantity: float,
    position_quantity: float,
) -> tuple[float, str | None]:
    return order_admission.clamp_exit_quantity(
        intent=intent,
        action=action,
        quantity=quantity,
        position_quantity=position_quantity,
    )


def _bar_ts_after_plan_open(bar_ts: str, opened_at: str | None) -> bool:
    """Return True when bar_ts >= opened_at (bar started at or after plan entry).

    Parsing failures on either side are treated as True (extremes applied) so
    that old plans without a parseable opened_at keep the current behaviour.
    """
    return exit_bars_service.bar_ts_after_plan_open(bar_ts, opened_at)


def _plan_snapshot(plan: TradePlan) -> dict:
    return planned_exits_service.plan_snapshot(plan)


def _apply_amend_exit(
    *,
    plan_store: TradePlanStore,
    symbol: str,
    amend_exit: dict,
    bars: list | None,
    entry: dict,
) -> None:
    amend_exit_service.apply_amend_exit_to_open_plan(
        plan_store=plan_store,
        symbol=symbol,
        amend_exit=amend_exit,
        bars=bars,
        entry=entry,
    )


def _llm_review_verdict(decision: codex_client.Decision) -> str:
    return plan_review.llm_review_verdict(decision.intent)


def _persist_last_llm_review(
    *,
    plan_store: TradePlanStore,
    symbol: str,
    now: datetime,
    decision: codex_client.Decision,
) -> None:
    plan_review.persist_last_llm_review(
        plan_store=plan_store,
        symbol=symbol,
        now=now,
        decision=decision,
    )


def _last_review_by_symbol(
    plan_store: TradePlanStore, symbols: list[str]
) -> dict[str, dict]:
    """Mapping {symbole: last_llm_review} pour les plans ouverts du périmètre qui
    portent une revue. Narrow contract : on ne passe pas le store entier au batch,
    juste le dernier verdict à réinjecter (continuité de thèse au réveil)."""
    return plan_review.last_review_by_symbol(plan_store, symbols)


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
    return execution_eligibility_service.build_execution_eligibility(
        symbols,
        stale_market_data=stale_market_data,
        prices=prices,
        daily_bars_by_symbol=daily_bars_by_symbol,
        data_age_by_symbol=data_age_by_symbol,
        now=now,
        runtime_interval=runtime_interval,
    )


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
    return execution_eligibility_service.execution_blocked_reason(
        execution_eligibility,
        symbol,
        fail_closed=fail_closed,
    )


def _positive_finite_float(raw: object) -> float | None:
    return reference_volatility_service.positive_finite_float(raw)


def _cockpit_vol_fraction(cockpit: dict, symbol: str) -> float | None:
    return reference_volatility_service.cockpit_vol_fraction(cockpit, symbol)


def _feature_vol_fraction(
    symbol: str,
    tradable_bars_by_symbol: dict[str, list],
) -> float | None:
    return reference_volatility_service.feature_vol_fraction(
        symbol,
        tradable_bars_by_symbol,
    )


def _reference_volatility_for_symbol(
    symbol: str,
    *,
    entry_price: float,
    cockpit: dict,
    tradable_bars_by_symbol: dict[str, list],
) -> float | None:
    return reference_volatility_service.reference_volatility_for_symbol(
        symbol,
        entry_price=entry_price,
        cockpit=cockpit,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
    )


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
    return planned_exits_service.apply_planned_exits(
        broker=broker,
        plan_store=plan_store,
        prices=prices,
        bars_by_symbol=bars_by_symbol,
        bars_intervals_by_symbol=bars_intervals_by_symbol,
        valuation_prices=valuation_prices,
        now=now,
        dry_run=dry_run,
        starting_equity=starting_equity,
        execution_eligibility=execution_eligibility,
        rate_fn=rate_fn,
        runtime_interval=DEFAULT_RUNTIME_INTERVAL,
        exit_check_interval=EXIT_CHECK_INTERVAL,
        exit_check_window_bars=EXIT_CHECK_WINDOW_BARS,
        append_model_performance=_append_model_performance,
        evaluate_plan_fn=evaluate_plan,
        clamp_exit_quantity_fn=_clamp_exit_quantity,
        execution_blocked_reason_fn=_execution_blocked_reason,
        plan_snapshot_fn=_plan_snapshot,
    )


def _exit_watch_cooldown_elapsed(watch: dict, *, now: datetime) -> bool:
    return cycle_scheduling.exit_watch_cooldown_elapsed(watch, now=now)


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
    return cycle_scheduling.scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol=bars_by_symbol,
        symbols=symbols,
        now=now,
        dry_run=dry_run,
        bars_interval=bars_interval,
        data_source=data_source,
        is_connection_market_error=_is_connection_market_error,
        log_warning=log.warning,
        append_event=_append_event,
        log_cycle_progress=_log_cycle_progress,
    )


def _scan_indicator_watches(
    symbols: list[str],
    *,
    sched: scheduler.Scheduler,
    now: datetime,
    data_source: object,
) -> list[dict]:
    return cycle_scheduling.scan_indicator_watches(
        symbols,
        sched=sched,
        now=now,
        data_source=data_source,
        is_connection_market_error=_is_connection_market_error,
        log_warning=log.warning,
        append_event=_append_event,
        log_cycle_progress=_log_cycle_progress,
    )


def _context_request_summary(
    req: codex_client.ContextResearchRequest,
    *,
    resolved: int,
) -> dict:
    return planner_batch._context_request_summary(req, resolved=resolved)


def _active_watch_summaries_by_symbol(
    *,
    sched: scheduler.Scheduler | None,
    symbols: list[str],
    now: datetime,
) -> dict[str, list[dict]]:
    return planner_batch._active_watch_summaries_by_symbol(sched=sched, symbols=symbols, now=now)


def _earliest_active_watch_expiry_iso(
    sched: scheduler.Scheduler, sym: str, *, now: datetime
) -> str | None:
    """Plus proche expiration (ISO) des veilles actives du symbole, ou None.

    Une veille armée EST le mécanisme de réveil du symbole : il doit dormir
    jusqu'à ce qu'elle se déclenche (`_scan_indicator_watches` pose alors un
    réveil immédiat) ou expire — pas retomber sur le défaut global 30 min et
    être re-décidé en aveugle (finding 2026-07-02, confirmé Codex).
    """
    return cycle_scheduling.earliest_active_watch_expiry_iso(sched, sym, now=now)


def _resolve_wake_event(
    event: str,
    sym: str,
    now: datetime,
    macro_next: list[dict],
    next_regular_session_open: Callable,
) -> str | None:
    """Résout un événement calendaire en timestamp ISO absolu.

    Retourne None si l'événement est inconnu, non résolu, ou si les données
    sont absentes. Jamais d'exception : fail-safe total.

    Rôle : set_next_wake = RECONSULTATION (l'agent reprend la main pour
    redécider) — distinct de propose_indicator_watch = PLAN ARMÉ (exécution
    automatique sans reconsulter).
    """
    return cycle_scheduling.resolve_wake_event(
        event,
        sym,
        now,
        macro_next,
        next_regular_session_open,
    )


def _apply_decision_schedule(
    *,
    sched: scheduler.Scheduler | None,
    sym: str,
    now: datetime,
    next_wake_in_minutes: float | None,
    next_wake_iso: str | None = None,
    cancel_watch_ids: list[str],
    pending_indicator_watch: dict | None,
    entry: dict,
) -> None:
    cycle_scheduling.apply_decision_schedule(
        sched=sched,
        sym=sym,
        now=now,
        next_wake_in_minutes=next_wake_in_minutes,
        next_wake_iso=next_wake_iso,
        cancel_watch_ids=cancel_watch_ids,
        pending_indicator_watch=pending_indicator_watch,
        entry=entry,
        append_event=_append_event,
        logger=log,
    )


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
      par défaut, le service applicatif utilise ``learnings.embeddings.embed_texts``.

    Le provider retourne ``{"rows": [...]}`` — le handler re-tronque à ≤ 8.
    Sans OPENAI_API_KEY ou sans query, la recherche tombe en mode FTS5+facettes.
    """
    return learnings_recall.build_recall_provider(
        store,
        lambda: now,  # provider per-cycle : la borne temporelle reste celle du cycle
        embedder=embedder,
        log_warning=log.warning,
    )


def _run_tool_round(*args, **kwargs):
    return planner_batch._run_tool_round(*args, **kwargs)


def _batch_decide(**kwargs):
    kwargs.setdefault("indicator_request_resolver", resolve_indicator_requests)
    kwargs.setdefault("event_appender", _append_event)
    return planner_batch.batch_decide(**kwargs)


def _is_valid_5m_bar(bar: object) -> bool:
    """Retourne True si la barre est utilisable pour les checks de sortie.

    Critères :
    - ts parsable en ISO-8601 (garde temporelle en aval exige un ts valide).
    - high et low sont des flottants finis avec low <= high.
    """
    return exit_bars_service.is_valid_exit_bar(bar)


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
    return exit_bars_service.fetch_exit_bars_for_open_plans(
        plan_store=plan_store,
        data_source=data_source,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
        tradable_prices=tradable_prices,
        now=now,
        fallback_interval=DEFAULT_RUNTIME_INTERVAL,
        exit_interval=EXIT_CHECK_INTERVAL,
        exit_lookback=EXIT_CHECK_LOOKBACK,
    )


def _execute_one_cycle_decision(
    *,
    sym: str,
    index: int,
    total: int,
    decision: codex_client.Decision,
    state: DecisionExecutionState,
    ctx: DecisionExecutionContext,
) -> DecisionExecutionState:
    _res_log = log.debug if decision.action == "HOLD" else log.info
    _res_log(
        "[decision %d/%d] %s result action=%s qty=%s intent=%s wake=%s confidence=%.2f provider=%s model=%s fallback=%s",
        index,
        total,
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
            minimum=ctx.min_wake_minutes,
            maximum=ctx.max_wake_minutes,
        )

    if decision.resolve_from_position or decision.intent in _RELATIVE_ORDER_INTENTS:
        raw_pos = ctx.broker.positions().get(sym)
        pos_qty = raw_pos.quantity if raw_pos is not None else 0.0
        decision = _resolve_position_aware_decision(decision, pos_qty)

    next_wake_event_iso: str | None = None
    if decision.next_wake_event is not None:
        next_wake_event_iso = _resolve_wake_event(
            event=decision.next_wake_event,
            sym=sym,
            now=ctx.now,
            macro_next=ctx.macro_next,
            next_regular_session_open=market.next_regular_session_open,
        )

    effective_quantity = abs(decision.quantity)

    entry = decision_entries.build_decision_entry(
        symbol=sym,
        decision=decision,
        effective_quantity=effective_quantity,
        next_wake_in_minutes=next_wake_in_minutes,
        next_wake_event_iso=next_wake_event_iso,
        armed_plan_id=ctx.armed_plan_ids.get(sym),
        armed_plan_order=ctx.armed_plan_orders.get(sym),
        runtime_data_source=ctx.runtime_data_source_by_sym.get(sym),
    )
    decision_source = str(entry["decision_source"])
    if decision_source == "llm" and sym in ctx.held_symbols and _counts_as_llm_review(decision):
        _persist_last_llm_review(
            plan_store=ctx.plan_store,
            symbol=sym,
            now=ctx.now,
            decision=decision,
        )

    reference_volatility: float | None = None
    runtime_exit_plan = decision.exit_plan
    entry["exit_plan"] = copy.deepcopy(decision.exit_plan) if decision.exit_plan else None
    watch_preparation = decision_watches.prepare_decision_indicator_watch(
        decision.indicator_watch,
        symbol=sym,
        now=ctx.now,
        scheduling_enabled=ctx.sched is not None,
    )
    entry.update(watch_preparation.entry_updates)
    if watch_preparation.rejections:
        log.warning(
            "indicator_watch rejets %s: %d conditions %s",
            sym,
            len(watch_preparation.rejections),
            [r["reason"] for r in watch_preparation.rejections],
        )
    pending_indicator_watch = watch_preparation.pending_watch

    def apply_decision_schedule() -> None:
        _apply_decision_schedule(
            sched=ctx.sched,
            sym=sym,
            now=ctx.now,
            next_wake_in_minutes=next_wake_in_minutes,
            next_wake_iso=next_wake_event_iso,
            cancel_watch_ids=decision.cancel_watch_ids,
            pending_indicator_watch=pending_indicator_watch,
            entry=entry,
        )

    def apply_default_schedule_after_blocked() -> None:
        if ctx.sched is not None:
            ctx.sched.clear_symbol_next_wake(sym)

    if (
        decision.action in {"BUY", "SELL"}
        and effective_quantity == 0
        and decision.risk_pct_target is None
    ):
        _log_cycle_progress("[decision %d/%d] %s blocked zero_quantity_order", index, total, sym)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": "zero_quantity_order"})
        return state

    if decision.action == "HOLD" or (effective_quantity == 0 and decision.risk_pct_target is None):
        apply_decision_schedule()
        if decision.amend_exit:
            _apply_amend_exit(
                plan_store=ctx.plan_store,
                symbol=sym,
                amend_exit=decision.amend_exit,
                bars=ctx.tradable_bars_by_symbol.get(sym),
                entry=entry,
            )
        hold_reason = decision_entries.hold_reason_for_decision(
            decision_source=decision_source,
            rationale=decision.rationale,
        )
        ctx.record_decision({**entry, "executed": False, "reason": hold_reason})
        return state

    invalid_intent = _invalid_intent_reason(decision)
    if invalid_intent is not None:
        _log_cycle_progress("[decision %d/%d] %s blocked %s", index, total, sym, invalid_intent)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": invalid_intent})
        return state

    execution_blocked = _execution_blocked_reason(
        ctx.execution_eligibility,
        sym,
        fail_closed=decision.intent in _OPENING_INTENTS,
    )
    if execution_blocked is not None:
        _log_cycle_progress(
            "[execution] %s ordre bloqué (%s) — watch/wake conservés", sym, execution_blocked
        )
        apply_decision_schedule()
        ctx.record_decision({**entry, "executed": False, "reason": execution_blocked})
        return state

    if runtime_exit_plan and decision.intent in _OPENING_INTENTS:
        if sym in ctx.armed_reference_volatilities:
            reference_volatility = ctx.armed_reference_volatilities[sym]
        else:
            reference_volatility = _reference_volatility_for_symbol(
                sym,
                entry_price=ctx.prices[sym],
                cockpit=ctx.cockpit,
                tradable_bars_by_symbol=ctx.tradable_bars_by_symbol,
            )
        try:
            if decision.intent in _PURE_OPEN_INTENTS and sym not in ctx.armed_plan_ids:
                intent_side = "LONG" if decision.intent == "OPEN_LONG" else "SHORT"
                runtime_exit_plan, _trace = resolve_exit_plan(
                    runtime_exit_plan,
                    entry_price=ctx.prices[sym],
                    side=intent_side,
                    reference_volatility=reference_volatility,
                    bars=ctx.tradable_bars_by_symbol.get(sym),
                )
            elif decision.intent == "ADD":
                intent_side = "LONG" if decision.action == "BUY" else "SHORT"
                runtime_exit_plan, _trace = resolve_exit_plan(
                    runtime_exit_plan,
                    entry_price=ctx.prices[sym],
                    side=intent_side,
                    reference_volatility=reference_volatility,
                    bars=ctx.tradable_bars_by_symbol.get(sym),
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
                total,
                sym,
                exc,
            )
            apply_default_schedule_after_blocked()
            ctx.record_decision({**entry, "executed": False, "reason": f"invalid_exit_plan:{exc}"})
            return state
        hard_stop_price = _hard_stop_price(runtime_exit_plan)
        if (
            decision.intent in _RISK_GUARDED_OPENING_INTENTS
            and hard_stop_price is not None
            and _hard_stop_wrong_side(decision.intent, ctx.prices[sym], hard_stop_price, action=decision.action)
        ):
            _log_cycle_progress(
                "[decision %d/%d] %s blocked invalid_exit_plan:hard_stop_wrong_side",
                index,
                total,
                sym,
            )
            apply_default_schedule_after_blocked()
            ctx.record_decision({**entry, "executed": False, "reason": "invalid_exit_plan:hard_stop_wrong_side"})
            return state

    pos = ctx.broker.positions().get(sym)
    clamped_quantity, exit_block_reason = _clamp_exit_quantity(
        intent=decision.intent,
        action=decision.action,
        quantity=effective_quantity,
        position_quantity=0.0 if pos is None else pos.quantity,
    )
    if exit_block_reason is not None:
        _log_cycle_progress("[decision %d/%d] %s blocked %s", index, total, sym, exit_block_reason)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": exit_block_reason})
        return state
    if clamped_quantity != effective_quantity:
        entry.setdefault("requested_qty", effective_quantity)
        effective_quantity = clamped_quantity
        entry["qty"] = effective_quantity
    if effective_quantity == 0 and decision.risk_pct_target is None:
        _log_cycle_progress("[decision %d/%d] %s hold zero_exit_quantity", index, total, sym)
        apply_default_schedule_after_blocked()
        ctx.record_decision({**entry, "executed": False, "reason": "zero_exit_quantity"})
        return state

    risk_outcome = risk_admission.assess_risk_admission(
        risk_admission.RiskAdmissionRequest(
            action=decision.action,
            intent=decision.intent,
            quantity=effective_quantity,
            price=ctx.prices[sym],
            equity=state.snap.equity,
            confidence=decision.confidence,
            runtime_exit_plan=runtime_exit_plan,
            risk_pct_target=decision.risk_pct_target,
            position_quantity=0.0 if pos is None else pos.quantity,
            position_avg_price=0.0 if pos is None else pos.avg_price,
            require_hard_stop=ctx.require_hard_stop,
            fx_rate=ctx.rate_for_symbol(sym),
        ),
        gate=ctx.gate,
    )
    effective_quantity = risk_outcome.quantity
    entry.update(risk_outcome.entry_updates)
    if not risk_outcome.approved:
        reason = risk_outcome.reason or "risk:rejected"
        if reason == "risk:risk_sizing_needs_stop":
            _log_cycle_progress("[risk] %s rejected code=risk_sizing_needs_stop", sym)
        elif reason == "risk:missing_hard_stop":
            _log_cycle_progress("[risk] %s rejected code=missing_hard_stop", sym)
        elif reason == "risk:risk_per_trade_exceeded":
            _log_cycle_progress(
                "[risk] %s rejected code=risk_per_trade_exceeded qty=%s max_qty=%s",
                sym,
                entry.get("risk_total_position_qty", effective_quantity),
                entry.get("max_risk_qty"),
            )
        elif reason == "zero_risk_quantity":
            _log_cycle_progress("[decision %d/%d] %s hold zero_risk_quantity", index, total, sym)
        elif reason.startswith("risk:"):
            _log_cycle_progress(
                "[risk] %s rejected code=%s confidence=%s",
                sym,
                reason.removeprefix("risk:"),
                decision.confidence,
            )
        else:
            _log_cycle_progress("[decision %d/%d] %s blocked %s", index, total, sym, reason)
        apply_default_schedule_after_blocked()
        blocked_entry = {**entry, "executed": False, "reason": reason}
        if risk_outcome.context is not None:
            blocked_entry["context"] = risk_outcome.context
        ctx.record_decision(blocked_entry)
        return state

    final_risk_outcome = risk_admission.assess_final_risk_gate(
        risk_admission.FinalRiskGateRequest(
            symbol=sym,
            action=decision.action,
            quantity=effective_quantity,
            rationale=decision.rationale,
            intent=decision.intent,
            price=ctx.prices[sym],
            position_quantity=0.0 if pos is None else pos.quantity,
            gross_exposure=state.gross,
            equity=state.snap.equity,
            fx_rate=ctx.rate_for_symbol(sym),
        ),
        gate=ctx.gate,
    )
    order = final_risk_outcome.order
    if not final_risk_outcome.approved:
        _log_cycle_progress(
            "[risk] %s rejected code=%s qty=%s price=%s",
            sym,
            final_risk_outcome.code,
            order.quantity,
            round(ctx.prices[sym], 6),
        )
        apply_default_schedule_after_blocked()
        ctx.record_decision(
            {
                **entry,
                "executed": False,
                "reason": final_risk_outcome.reason,
                "context": final_risk_outcome.context,
            }
        )
        return state

    if ctx.queue_execute_enabled and ctx.execute_ledger is not None:
        _exec_entry_context = None
        if runtime_exit_plan is not None and decision.intent in {"OPEN_LONG", "OPEN_SHORT", "ADD", "REVERSE"}:
            _exec_entry_age = ctx.data_age_by_symbol.get(sym)
            _exec_entry_context = entry_context.build_trade_entry_context(
                price=ctx.prices[sym],
                runtime_interval=ctx.runtime_interval,
                data_age_minutes=_exec_entry_age,
                session_open=bool(market.session_snapshot(sym, now=ctx.now).get("open")),
                daily_as_of=((ctx.execution_eligibility.get(sym) or {}).get("planning") or {}).get("daily_as_of"),
            )
        _exec_plan_payload = execute_queue_plan.build_execute_queue_plan_payload(
            plan_reader=ctx.plan_store,
            symbol=sym,
            action=decision.action,
            intent=decision.intent,
            quantity=effective_quantity,
            price=ctx.prices[sym],
            opened_at=ctx.now.isoformat(),
            runtime_exit_plan=runtime_exit_plan,
            reference_volatility=reference_volatility,
            rationale=decision.rationale,
            entry_context=_exec_entry_context,
            position_quantity=0.0 if pos is None else pos.quantity,
            position_avg_price=0.0 if pos is None else pos.avg_price,
            llm_provider=decision.llm_provider,
            llm_model=decision.llm_model,
            llm_fallback_reason=decision.llm_fallback_reason,
            llm_confidence=decision.confidence,
        )
        _exec_outcome = execute_queue_dispatch.dispatch_execute_order_via_queue(
            ledger=ctx.execute_ledger,
            symbol=sym,
            side=decision.action,
            quantity=effective_quantity,
            rationale=decision.rationale,
            price=ctx.prices[sym],
            ts=ctx.now.isoformat(),
            fx_rate=ctx.rate_for_symbol(sym),
            dry_run=ctx.dry_run,
            plan_to_upsert=_exec_plan_payload.plan_to_upsert,
            symbol_to_close=_exec_plan_payload.symbol_to_close,
            cycle_id=ctx.now.isoformat(),
            intent=decision.intent,
            budget_s=_EXECUTE_POLL_BUDGET_S,
        )
        if _exec_outcome.reason is not None:
            apply_default_schedule_after_blocked()
            ctx.record_decision({**entry, "executed": False, "reason": _exec_outcome.reason})
            return state
        fill = _exec_outcome.fill
    else:
        fill = ctx.broker.submit(
            order,
            ctx.prices[sym],
            ctx.now.isoformat(),
            dry_run=ctx.dry_run,
            fx_rate=ctx.rate_for_symbol(sym),
        )
    if not ctx.dry_run:
        ctx.gate.record_pass()
        state.gross = _gross_exposure(ctx.broker, ctx.prices, rate_of=ctx.rate_for_symbol)
        if fill is not None:
            latest = portfolio.snapshot(
                ctx.broker,
                lambda s: ctx.prices.get(s, 0.0),
                ctx.starting_equity,
                fx_rate_of=ctx.rate_for_symbol,
            )
            final_position = ctx.broker.positions().get(sym)
            fill_accounting = fill_outcome.build_fill_accounting(
                fill=fill,
                symbol=sym,
                action=decision.action,
                intent=decision.intent,
                quantity=effective_quantity,
                price=ctx.prices[sym],
                confidence=decision.confidence,
                llm_provider=decision.llm_provider,
                llm_model=decision.llm_model,
                llm_fallback_reason=decision.llm_fallback_reason,
                equity=latest.equity,
                cash=latest.cash,
                position_quantity=0.0 if final_position is None else final_position.quantity,
            )
            _append_model_performance(**fill_accounting.model_performance)
            entry.update(fill_accounting.entry_updates)
            plan_entry_context: dict = {}
            if runtime_exit_plan and decision.intent in _OPENING_INTENTS:
                _entry_age = ctx.data_age_by_symbol.get(sym)
                plan_entry_context = entry_context.build_trade_entry_context(
                    price=ctx.prices[sym],
                    runtime_interval=ctx.runtime_interval,
                    data_age_minutes=_entry_age,
                    session_open=bool(market.session_snapshot(sym, now=ctx.now).get("open")),
                    daily_as_of=((ctx.execution_eligibility.get(sym) or {}).get("planning") or {}).get("daily_as_of"),
                )
            fill_plan_effects.apply_filled_plan_effects(
                entry=entry,
                broker=ctx.broker,
                plan_store=ctx.plan_store,
                symbol=sym,
                action=decision.action,
                intent=decision.intent,
                quantity=effective_quantity,
                price=ctx.prices[sym],
                opened_at=fill.ts,
                runtime_exit_plan=runtime_exit_plan,
                reference_volatility=reference_volatility,
                llm_provider=decision.llm_provider,
                llm_model=decision.llm_model,
                llm_fallback_reason=decision.llm_fallback_reason,
                llm_confidence=decision.confidence,
                queue_execute_enabled=ctx.queue_execute_enabled,
                entry_thesis=decision.rationale,
                entry_context=plan_entry_context,
            )
        if fill is not None and decision.intent in _OPENING_INTENTS:
            post_entry_wake = market.freshness_budget_minutes(ctx.runtime_interval, grace_minutes=0.0)
            if next_wake_in_minutes is None or next_wake_in_minutes > post_entry_wake:
                next_wake_in_minutes = post_entry_wake
                entry["next_wake_in_minutes"] = next_wake_in_minutes
                entry["post_entry_review_scheduled"] = True
    _log_cycle_progress(
        "[order] %s %s qty=%s price=%s executed=%s plan=%s",
        sym,
        decision.action,
        effective_quantity,
        round(ctx.prices[sym], 6),
        not ctx.dry_run,
        entry["trade_plan_created"],
    )
    apply_decision_schedule()
    ctx.record_decision({**entry, "executed": not ctx.dry_run, "reason": "ok", "price": ctx.prices[sym]})
    return state


def run_cycle(
    *,
    dry_run: bool,
    now: datetime | None = None,
    symbols_filter: list[str] | None = None,
    sched: scheduler.Scheduler | None = None,
    data_source: object,
    default_wake_minutes: float = 30.0,
    min_wake_minutes: float | None = None,
    max_wake_minutes: float | None = None,
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
    queue_decide_enabled: bool = False,
    task_ledger=None,  # TaskLedger | None (task_ledger.db — dédié decide)
    queue_execute_enabled: bool = False,
    execute_ledger=None,  # TaskLedger | None (casys.db — partagé broker/plan/ledger)
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
    from trader.support.config.portfolio import load_starting_cash

    starting_equity = load_starting_cash(ROOT / "config")
    indicator_triggers = indicator_triggers or []
    triggers_by_symbol: dict[str, list[dict]] = {}
    for trigger in indicator_triggers:
        triggers_by_symbol.setdefault(str(trigger.get("symbol")), []).append(trigger)
    model_call_limit_for_status = None if queue_decide_enabled else max_model_calls_per_cycle
    model_call_cap_label = "none(queue)" if queue_decide_enabled else str(max_model_calls_per_cycle)
    _log_cycle_progress(
        "[cycle] start dry_run=%s due=%d model_call_cap=%s",
        dry_run,
        len(symbols_to_decide),
        model_call_cap_label,
    )
    _write_status(
        "cycle_started",
        current_symbol=None,
        symbols_due=symbols_to_decide,
        symbols_total=len(symbols_to_decide),
        decisions_done=0,
        dry_run=dry_run,
        model_calls_used=0,
        max_model_calls_per_cycle=model_call_limit_for_status,
    )
    _append_event("cycle_started", symbols_due=symbols_to_decide, dry_run=dry_run)

    broker = make_broker(
        state_dir=STATE_DIR,
        starting_cash=starting_equity,
        commission_model=commission_model,
        backend=os.getenv("CASYS_STATE_BACKEND", "json"),
    )
    plan_store = make_trade_plan_store(
        state_dir=STATE_DIR,
        backend=os.getenv("CASYS_STATE_BACKEND", "json"),
    )
    gate = RiskGate(RiskLimits.from_dict(risk_cfg))
    # Paper/exploration : si False, une ouverture SANS hard_stop n'est plus rejetée
    # (stop optionnel, position bornée par les seuls fusibles notionnels). Défaut
    # True = guardrail D6 préservé (live-safe). Voir spec exploration-basse-confiance.
    require_hard_stop = bool(risk_cfg.get("require_hard_stop", True))
    gate.start_cycle()
    mem = agent_memory.Memory(ROOT / "mandate" / "mandate.md", ROOT / "mandate" / "memory.md")
    learnings_store = raw_learnings.RawLearningsStore(
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

    snapshot = market_snapshot.build_market_snapshot(
        symbols=symbols,
        data_source=data_source,
        now=now,
        max_market_data_age_minutes=max_market_data_age_minutes,
        runtime_interval=runtime_interval,
        runtime_lookback=runtime_lookback,
        config_dir=ROOT / "config",
        plan_store=plan_store,
        scheduler=sched,
        daily_lookback=COCKPIT_DAILY_LOOKBACK,
        daily_interval=COCKPIT_DAILY_INTERVAL,
        is_connection_market_error=_is_connection_market_error,
        execution_eligibility_builder=_build_execution_eligibility,
        exit_bars_fetcher=_fetch_5m_bars_for_open_plans,
    )
    prices = snapshot.prices
    stale_market_data = snapshot.stale_market_data
    data_age_by_symbol = snapshot.data_age_by_symbol
    runtime_data_source_by_sym = snapshot.runtime_data_source_by_symbol
    daily_bars_by_symbol = snapshot.daily_bars_by_symbol
    tradable_prices = snapshot.tradable_prices
    tradable_symbols = snapshot.tradable_symbols
    tradable_bars_by_symbol = snapshot.tradable_bars_by_symbol
    fx_rate_by_ccy = snapshot.fx_rate_by_ccy
    _rate = snapshot.rate_for_symbol
    execution_eligibility = snapshot.execution_eligibility
    exit_bars_by_symbol = snapshot.exit_bars_by_symbol
    exit_intervals_by_symbol = snapshot.exit_intervals_by_symbol
    _log_cycle_progress(
        "[market] loaded ok=%d missing=%d",
        len(prices),
        max(0, len(symbols) - len(prices)),
    )
    if stale_market_data:
        _log_cycle_progress("[market] stale symbols=%s", sorted(stale_market_data))

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
        "now_human": market.human_clock(now),
        "market_clocks": market.market_clocks(now, symbols),
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
        "kpis": live_kpis.compute_live_kpis(STATE_DIR),
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

    recorder = DecisionRecorder(
        report=report,
        dry_run=dry_run,
        symbols_total=len(symbols_to_decide),
        max_model_calls_per_cycle=model_call_limit_for_status,
        learnings_store=learnings_store,
        decision_ledger_store=decision_ledger_store,
        refresh_report_portfolio=refresh_report_portfolio,
        write_current_report=_write_current_report,
        write_status=_write_status,
        append_event=_append_event,
        news_snapshot=lambda symbol, now: news_feed.news_snapshot(symbol, now=now),
        macro_next=_cycle_macro_next,
        now=now,
        recall_store=_recall_store,
        merge_gate_feedback=_merge_gate_feedback,
        model_calls_used_getter=lambda: model_calls_used,
    )
    record_decision = recorder.record

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
    _has_armed_triggers = any(
        str(trigger.get("on_trigger")) == "EXECUTE_ORDER" and isinstance(trigger.get("order"), dict)
        for trigger in indicator_triggers
    )
    armed_resolution = armed_plans.resolve_armed_plan_triggers(
        indicator_triggers=indicator_triggers,
        symbols_to_decide=symbols_to_decide,
        prices=prices,
        stale_market_data=stale_market_data,
        positions=broker.positions() if _has_armed_triggers else {},
        cockpit=cockpit,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
        reference_volatility_for_symbol=_reference_volatility_for_symbol,
    )
    for progress in armed_resolution.progress_logs:
        _log_cycle_progress(progress.message, *progress.args)
    for event in armed_resolution.events:
        _append_event(event.name, **event.payload)

    armed_decisions = armed_resolution.decisions
    armed_plan_ids = armed_resolution.plan_ids
    armed_plan_orders = armed_resolution.plan_orders
    stale_armed_plans = armed_resolution.stale_plans
    armed_reference_volatilities = armed_resolution.reference_volatilities
    # les symboles armés ont déjà leur décision : pas d'appel LLM, pas de
    # relevance_gate. Le RiskGate déterministe reste appliqué plus bas.
    decidable = [s for s in decidable if s not in armed_decisions]

    # Gate de pertinence (D7 étage A) : ne soumettre au LLM que les réveils
    # demandés par l'agent, les événements, ou la revue périodique garantie.
    # Le polling par défaut sur symbole calme ne consomme pas d'appel modèle.
    quiet_gate = infra_holds.quiet_gate_decisions(
        symbols=decidable,
        now=now,
        state_key=str(STATE_DIR),
        last_llm_at=_LAST_LLM_AT,
        cockpit=cockpit,
        regime_families=base_context["regime_families"],
        active_families=active_families,
        wake_source=sched,
        triggers_by_symbol=triggers_by_symbol,
        held_symbols=held_symbols,
        runtime_data_source_by_sym=runtime_data_source_by_sym,
    )
    gated_symbols = quiet_gate.gated_symbols
    if gated_symbols:
        _log_cycle_progress("[gate] quiet symbols=%s (pas d'appel LLM)", gated_symbols)
        for entry in quiet_gate.entries:
            # Pas de wake par symbole : le gated retombe sur le polling par
            # défaut (un wake posé ici se ferait passer pour un wake agent).
            record_decision(entry)
    decidable = quiet_gate.kept_symbols

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
    # undecided_symbols : symboles non décidés en mode queue (skippés dead/budget).
    # Ils sont EXCLUS du fallback HOLD synthétique
    # ci-dessous (FIX 2). En mode batch, reste vide (comportement inchangé).
    undecided_symbols: set[str] = set()
    decisions_by_symbol: dict[str, codex_client.Decision] = {}
    streamed_decision_symbols: set[str] = set()
    buffered_opening_symbols: set[str] = set()

    execution_state = DecisionExecutionState(snap=snap, gross=gross)
    execution_ctx = DecisionExecutionContext(
        now=now,
        min_wake_minutes=min_wake_minutes,
        max_wake_minutes=max_wake_minutes,
        macro_next=_cycle_macro_next,
        broker=broker,
        plan_store=plan_store,
        gate=gate,
        sched=sched,
        prices=prices,
        execution_eligibility=execution_eligibility,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
        data_age_by_symbol=data_age_by_symbol,
        runtime_data_source_by_sym=runtime_data_source_by_sym,
        armed_plan_ids=armed_plan_ids,
        armed_plan_orders=armed_plan_orders,
        armed_reference_volatilities=armed_reference_volatilities,
        held_symbols=held_symbols,
        cockpit=cockpit,
        runtime_interval=runtime_interval,
        starting_equity=starting_equity,
        require_hard_stop=require_hard_stop,
        dry_run=dry_run,
        queue_execute_enabled=queue_execute_enabled,
        execute_ledger=execute_ledger,
        record_decision=record_decision,
        rate_for_symbol=_rate,
    )

    if queue_decide_enabled and task_ledger is not None:
        # Mode file : enfile 1 tâche par symbole et collecte via polling.
        # Le pool DecidePool tourne en arrière-plan (démarré dans main()).
        from trader.application.planner_batch import (
            _active_watch_summaries_by_symbol,
            build_symbol_facts,
        )
        from trader.application.queue_dispatch import iter_decide_results_via_queue
        from trader.application.recent_decisions import recent_decisions_by_symbol
        _last_review = _last_review_by_symbol(plan_store, decidable)
        # Push anti-répétition : N dernières décisions authentiques par symbole (même
        # sans position ouverte, là où _last_review ne couvre que les plans ouverts).
        # Une seule lecture du ledger, groupée.
        _recent_decisions = recent_decisions_by_symbol(decision_ledger_store, symbols=decidable)
        _active_watches = _active_watch_summaries_by_symbol(
            sched=sched, symbols=decidable, now=now,
        )
        symbol_facts_by_sym = {
            sym: {
                "indicator_triggers": triggers_by_symbol.get(sym, []),
                **build_symbol_facts(
                    sym,
                    data_age_by_symbol=data_age_by_symbol,
                    now=now,
                    active_watches_by_symbol=_active_watches,
                    market_context_by_symbol=execution_eligibility,
                    last_review_by_symbol=_last_review,
                    recent_decisions_by_symbol=_recent_decisions,
                ),
            }
            for sym in decidable
        }
        # Mode queue : l'admission n'est pas capée par appels ; l'itérateur rend
        # chaque symbole dès son état terminal, ResourcePools/AIMD borne la pression provider.
        stream_index_by_symbol = {
            sym: index
            for index, sym in enumerate(symbols_to_decide, start=1)
        }
        for sym, decision, calls in iter_decide_results_via_queue(
            ledger=task_ledger,
            decidable=decidable,
            mandate=mandate_txt,
            memory=memory_txt,
            shared_context=base_context,
            symbol_facts_by_sym=symbol_facts_by_sym,
            decision_timeout_s=decision_timeout_s,
            agent_tools_enabled=agent_tools_enabled,
            cycle_id=now.isoformat(),
            now_fn=time.time,
            # Univers d'analyse du cycle → resolver d'indicateurs du tour d'outils
            # (filtre dur + paires cross-asset, spec §5 W5).
            symbols_universe=analysis_symbols,
        ):
            if decision is None:
                undecided_symbols.add(sym)
                continue
            model_calls_used += calls
            routed_decision = _resolve_decision_for_execution_routing(
                symbol=sym,
                decision=decision,
                broker=broker,
            )
            decisions_by_symbol[sym] = routed_decision
            if _counts_as_llm_review(routed_decision):
                _LAST_LLM_AT[(str(STATE_DIR), sym)] = now
            if routed_decision.intent in _OPENING_INTENTS:
                buffered_opening_symbols.add(sym)
                continue

            execution_state = _execute_one_cycle_decision(
                sym=sym,
                index=stream_index_by_symbol.get(sym, len(streamed_decision_symbols) + 1),
                total=len(symbols_to_decide),
                decision=routed_decision,
                state=execution_state,
                ctx=execution_ctx,
            )
            snap = execution_state.snap
            gross = execution_state.gross
            streamed_decision_symbols.add(sym)
    else:
        # Mode batch classique — comportement STRICTEMENT inchangé (flag off).
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
    # "[decide]" : commun batch/queue (E7 — le libellé [batch] mentait en mode queue).
    _log_cycle_progress("[decide] decided=%d model_calls=%d", len(decisions_by_symbol), model_calls_used)

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
        if sym in streamed_decision_symbols:
            # Déjà exécuté/enregistré au fil de l'eau en mode queue decide.
            continue
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
            stale_hold = infra_holds.stale_market_hold_decision(
                symbol=sym,
                stale_data=stale_data,
                current_streak=streak,
                default_wake_minutes=default_wake_minutes,
                now=now,
                clamp_wake_to_session_open=market.clamp_wake_to_session_open,
                stale_armed_plan=stale_armed_plans.get(sym),
                runtime_data_source=runtime_data_source_by_sym.get(sym),
            )
            wake_minutes = stale_hold.wake_minutes
            new_streak = stale_hold.new_streak
            if sched is not None:
                sched.set_stale_streak(sym, new_streak)
                sched.set_symbol_next_wake_in(sym, minutes=wake_minutes, now=now)

            if stale_hold.entry is not None:
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
                record_decision(stale_hold.entry)
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
                if stale_hold.event is not None:
                    _append_event(stale_hold.event.name, **stale_hold.event.payload)
            continue
        if sym not in prices:
            _log_cycle_progress("[decision %d/%d] %s skipped no_price", index, len(symbols_to_decide), sym)
            continue

        # FIX 2 : en mode queue, les symboles non décidés ce cycle (skippés
        # dead/budget) sont EXCLUS du fallback HOLD synthétique —
        # ils seront redécidés au prochain cycle. Le mode batch garde son comportement
        # (HOLD no_decision_in_batch) via undecided_symbols = set() initialisé plus haut.
        if sym in undecided_symbols:
            _log_cycle_progress(
                "[decision %d/%d] %s queue_decide_deferred — aucun HOLD synthétique",
                index, len(symbols_to_decide), sym,
            )
            _append_event("queue_decide_deferred", symbol=sym, cycle_id=now.isoformat())
            continue

        decision = decisions_by_symbol.get(sym) or codex_client.Decision.hold(sym, "no_decision_in_batch")
        execution_state = _execute_one_cycle_decision(
            sym=sym,
            index=index,
            total=len(symbols_to_decide),
            decision=decision,
            state=execution_state,
            ctx=execution_ctx,
        )
        snap = execution_state.snap
        gross = execution_state.gross

    report["model_calls_used"] = model_calls_used
    refresh_report_portfolio()
    _write_current_report(report)
    if model_call_limit_for_status is None:
        calls_label = f"{model_calls_used} (no cap)"
    else:
        calls_label = f"{model_calls_used}/{model_call_limit_for_status}"
    _log_cycle_progress(
        "[cycle] completed decisions=%d executed=%d calls=%s",
        len(report["decisions"]),
        sum(1 for item in report["decisions"] if item.get("executed")),
        calls_label,
    )
    _write_status(
        "cycle_completed",
        current_symbol=None,
        decisions_done=len(report["decisions"]),
        symbols_total=len(symbols_to_decide),
        model_calls_used=model_calls_used,
        max_model_calls_per_cycle=model_call_limit_for_status,
        last_decision=report["decisions"][-1] if report["decisions"] else None,
    )
    _append_event("cycle_completed", decisions_done=len(report["decisions"]), model_calls_used=model_calls_used)
    cycle_finalization.finalize_cycle(
        state_dir=STATE_DIR,
        now=now,
        report=report,
        decidable_symbols=list(decidable),
        decided_symbols=list(decisions_by_symbol.keys()),
        learning=cycle_finalization.LearningConsolidationRequest(
            raw_store=learnings_store,
            consolidated_store=consolidated_learnings_store,
            threshold=learning_consolidation_threshold,
            acpx_bin=consolidator_acpx_bin,
            acpx_agent=consolidator_acpx_agent,
            model=consolidator_model,
            timeout_s=consolidator_timeout_s,
            attribution=attribution_payload,
            meta_performance=meta_performance_payload,
            consolidate=consolidator.maybe_consolidate,
        ),
        gross_rejection_cache=_LAST_GROSS_REJECTIONS,
        summarize_gross_rejections=summarize_gross_rejections,
        collect_macro=macro_series.maybe_collect,
        shadow_queue_enabled=os.getenv("CASYS_SHADOW_QUEUE_ENABLED", "0") == "1",
        state_backend=os.getenv("CASYS_STATE_BACKEND", "json"),
        write_current_report=_write_current_report,
        append_event=_append_event,
        logger=log,
    )

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
    parser.add_argument("--min-wake-minutes", type=float, default=None, help="borne basse optionnelle du réveil agent (défaut: aucune)")
    parser.add_argument("--max-wake-minutes", type=float, default=None, help="borne haute optionnelle du réveil agent (défaut: aucune — l'agent est autonome)")
    parser.add_argument("--max-context-requests-per-symbol", type=int, default=2, help="nombre max de requêtes indicateurs par symbole")
    parser.add_argument("--max-indicators-per-request", type=int, default=4, help="nombre max d'indicateurs par requête")
    parser.add_argument(
        "--max-model-calls-per-cycle",
        type=int,
        default=25,
        help="cap du mode batch legacy uniquement ; ignoré en mode queue/free-iteration (no call cap)",
    )
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
        help="appels LLM décideur en parallèle par batch ; en mode queue (CASYS_QUEUE_DECIDE_ENABLED=1) ce même flag = nb de workers du pool (défaut/env CASYS_DECISION_BATCH_PARALLELISM: 3)",
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

    from trader.runtime.logging_setup import setup_logging
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
    from trader.runtime.pid_file import claim_pid_file, release_pid_file

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    news_feed.set_default_news_archive(STATE_DIR / "news_items")
    _pid_file = STATE_DIR / "daemon.pid"
    if not claim_pid_file(pid_file=_pid_file, pid=os.getpid()):
        log.error("daemon déjà vivant (pid file %s) — refus de démarrer un doublon", _pid_file)
        return 1

    # Bootstrap ordonné AVANT toute lecture d'état ou rotation runtime :
    # rotation mensuelle JSONL, bootstrap SQLite/shadows, puis scheduler.
    _state_backend = os.getenv("CASYS_STATE_BACKEND", "json")
    _runtime_state = daemon_bootstrap.bootstrap_runtime_state(
        state_dir=STATE_DIR,
        config_dir=ROOT / "config",
        state_backend=_state_backend,
        commission_model=commission_model,
        now=now(),
        logger=log,
        bootstrap_state_backend_fn=bootstrap_state_backend,
        make_scheduler_fn=make_scheduler,
    )
    sched = _runtime_state.scheduler
    log.info("daemon démarré (dry_run=%s, once=%s)", dry_run, args.once)
    log.info(
        "[config] decision_batch_parallelism=%d batch_size=%d batch_max_model_calls=%d queue_call_cap=none decision_timeout_s=%d agent_tools=%s",
        args.decision_batch_parallelism,
        args.decision_batch_size,
        args.max_model_calls_per_cycle,
        args.decision_timeout_s,
        args.agent_tools,
    )
    bootstrap = args.bootstrap_all

    # Ref partagée vers le data_source courant : les workers de file la lisent via
    # make_indirect_get_bars(handle.get) — jamais de capture de l'objet (remplacé en run).
    _ds_handle = data_source_runtime.DataSourceHandle()

    # Services du tour d'outils grain-1 (spec queue tool-round §4, issue #2) :
    # construits au boot, consommés par le handler decide quand agent_tools_enabled.
    _decide_tool_services = queue_runtime.build_decide_tool_services(
        get_data_source=_ds_handle.get,
        learnings_db_path=STATE_DIR / "learnings.db",
        max_context_requests_per_symbol=args.max_context_requests_per_symbol,
        max_indicators_per_request=args.max_indicators_per_request,
        logger=log,
    )

    # Boot des pools queue-via-file. La construction concrète vit côté runtime :
    # - decide : DB dédiée task_ledger.db, workers persistants acpx ;
    # - execute : nécessite sqlite et partage casys.db avec broker/plan/ledger.
    _queue_runtimes = queue_runtime.start_queue_runtimes(
        state_dir=STATE_DIR,
        decide_enabled=_env_int("CASYS_QUEUE_DECIDE_ENABLED", 0) == 1,
        decision_parallelism=args.decision_batch_parallelism,
        decision_batch_size=args.decision_batch_size,
        default_decision_batch_size=DEFAULT_DECISION_BATCH_SIZE,
        codex_client=codex_client,
        execute_enabled_raw=_env_int("CASYS_QUEUE_EXECUTE_ENABLED", 0) == 1,
        state_backend=_state_backend,
        commission_model=commission_model,
        decision_timeout_s=args.decision_timeout_s,
        decide_tool_services=_decide_tool_services,
        logger=log,
    )
    _queue_decide_enabled = _queue_runtimes.decide.enabled
    _task_ledger = _queue_runtimes.decide.ledger
    _decide_pool = _queue_runtimes.decide.pool
    _queue_execute_enabled = _queue_runtimes.execute.enabled
    _execute_ledger = _queue_runtimes.execute.ledger
    _execute_pool = _queue_runtimes.execute.pool

    _data_sources_cfg = ROOT / "config" / "data_sources.yaml"
    _data_source_config = data_source_runtime.load_data_source_config(
        config_path=_data_sources_cfg,
        profile_override=args.data_profile or None,
        logger=log,
        parse_config_fn=parse_data_sources_config,
    )
    # Validé une fois à la frontière (AX #5) : une valeur <1 ferait lever
    # ThrottledDataSource en pleine boucle (crash-loop) — fallback explicite.
    _yf_fetch_concurrency = _env_int("CASYS_YFINANCE_FETCH_CONCURRENCY", 4)
    if _yf_fetch_concurrency < 1:
        log.warning(
            "CASYS_YFINANCE_FETCH_CONCURRENCY=%d invalide (<1) — fallback 4",
            _yf_fetch_concurrency,
        )
        _yf_fetch_concurrency = 4
    _data_source_state = data_source_runtime.DataSourceState(
        data_source=None,
        composite_available={},
        ib_attach_backoff=None,
    )
    data_source = _data_source_state.data_source

    def _adopt_data_source_state(state: data_source_runtime.DataSourceState) -> None:
        """Point de synchro UNIQUE : locals de la boucle + handle partagé des workers.

        Toute réassignation de data_source passe ici — impossible d'oublier le handle.
        """
        nonlocal _data_source_state, data_source
        _data_source_state = state
        data_source = state.data_source
        _ds_handle.set(state.data_source)

    try:
        while True:
            sleep_seconds: float | None = None
            stop_after_iteration = False
            try:
                if data_source is None:
                    _adopt_data_source_state(data_source_runtime.build_data_source(
                        _data_source_config,
                        host=args.ib_host,
                        port=args.ib_port,
                        client_id=args.ib_client_id,
                        attach_retry_seconds=args.ib_attach_retry_seconds,
                        now=now(),
                        connect_ib_fn=connect_ib,
                        logger=log,
                        composite_cls=CompositeDataSource,
                        yfinance_cls=YFinanceDataSource,
                        ib_data_source_cls=IBDataSource,
                        backoff_cls=IBAttachBackoff,
                        disconnect_quietly=_disconnect_quietly,
                        # Anti-429 : borne les fetchs yahoo concurrents (workers de file).
                        # Sans effet sur le cycle (fetchs séquentiels ≤ 1 concurrent).
                        throttle_by_source={"yfinance": _yf_fetch_concurrency},
                    ))
                loop_now = now()
                _adopt_data_source_state(data_source_runtime.maybe_attach_ib(
                    _data_source_state,
                    _data_source_config,
                    host=args.ib_host,
                    port=args.ib_port,
                    client_id=args.ib_client_id,
                    now=loop_now,
                    connect_ib_fn=connect_ib,
                    logger=log,
                    composite_cls=CompositeDataSource,
                    ib_data_source_cls=IBDataSource,
                    disconnect_quietly=_disconnect_quietly,
                ))
                market_rotation_runtime.tick_market_rotation(
                    config_dir=ROOT / "config",
                    state_dir=STATE_DIR,
                    loop_now=loop_now,
                    logger=log,
                )
                symbols = _load_yaml(ROOT / "config" / "universe.yaml")["symbols"]
                sched.reconcile_universe(symbols)
                cycle_scheduling.expire_indicator_watches(
                    sched,
                    now=loop_now,
                    append_event=_append_event,
                    log_info=log.info,
                )
                indicator_triggers = (
                    []
                    if args.once or bootstrap
                    else _scan_indicator_watches(symbols, sched=sched, now=loop_now, data_source=data_source)
                )
                due_symbols = _select_due_symbols(symbols, sched=sched, once=args.once, bootstrap=bootstrap, now=loop_now)
                bootstrap = False
                cycle_context = cycle_dispatch.RunCycleRuntimeContext(
                    dry_run=dry_run,
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
                    queue_decide_enabled=_queue_decide_enabled,
                    task_ledger=_task_ledger,
                    queue_execute_enabled=_queue_execute_enabled,
                    execute_ledger=_execute_ledger,
                )
                if not due_symbols:
                    report = cycle_dispatch.dispatch_run_cycle(
                        run_cycle_fn=run_cycle,
                        context=cycle_context,
                        now=loop_now,
                        symbols_filter=[],
                    )
                    if cycle_reporting.persist_cycle_report(
                        report,
                        writer=_runtime_state_writer(),
                        only_if_active=True,
                    ):
                        log.debug("cycle actif sans symbole dû: %s", json.dumps(report, ensure_ascii=False))
                    wait = sched.seconds_until_wake(symbols)
                    sleep_seconds = min(wait, args.poll)
                    log.debug("aucun symbole dû — pause %.0fs", sleep_seconds)
                else:
                    report = cycle_dispatch.dispatch_run_cycle(
                        run_cycle_fn=run_cycle,
                        context=cycle_context,
                        now=loop_now,
                        symbols_filter=due_symbols,
                    )
                    log.debug("cycle: %s", json.dumps(report, ensure_ascii=False))
                    cycle_reporting.persist_cycle_report(report, writer=_runtime_state_writer())

                    if args.once:
                        stop_after_iteration = True
                    else:
                        wait = sched.seconds_until_wake(symbols)
                        sleep_seconds = min(wait, args.poll)
                        log.info("[sleep] next_check_in=%.0fs next_due_in=%.0fs", sleep_seconds, wait)
                _adopt_data_source_state(data_source_runtime.detach_failed_ib(
                    _data_source_state,
                    _data_source_config,
                    now=loop_now,
                    is_connection_market_error=_is_connection_market_error,
                    disconnect_quietly=_disconnect_quietly,
                    attach_retry_seconds=args.ib_attach_retry_seconds,
                    logger=log,
                    composite_cls=CompositeDataSource,
                    backoff_cls=IBAttachBackoff,
                ))
            except market.MarketError as exc:
                if data_source is not None:
                    _disconnect_quietly(data_source)
                    _adopt_data_source_state(data_source_runtime.DataSourceState(
                        data_source=None,
                        composite_available=_data_source_state.composite_available,
                        ib_attach_backoff=_data_source_state.ib_attach_backoff,
                    ))
                if _data_source_config.use_composite:
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
        # Shutdown best-effort : pools queue, source data, puis suppression du
        # pid file seulement s'il contient encore NOTRE pid (jamais celui d'un
        # successeur, cf bug Maj+X cockpit).
        # Le handle est invalidé AVANT le disconnect : un worker retardataire lit
        # None (-> unavailable) plutôt qu'une source déconnectée.
        _ds_handle.set(None)
        runtime_shutdown.shutdown_runtime_resources(
            decide_pool=_decide_pool,
            execute_pool=_execute_pool,
            data_source=data_source,
            pid_file=_pid_file,
            pid=os.getpid(),
            disconnect_quietly=_disconnect_quietly,
            release_pid_file=release_pid_file,
            logger=log,
        )


if __name__ == "__main__":
    main()
