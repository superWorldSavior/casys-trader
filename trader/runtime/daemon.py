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
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from trader.agent import client as codex_client
from trader.agent import llm
from trader.agent import memory as agent_memory
from trader.agent.context import build_market_cockpit, resolve_indicator_requests
from trader.agent.learnings import recall_provider
from trader.application.decide import (
    planner_batch,
)
from trader.application.execute import (
    order_admission,
    queue_dispatch as execute_queue_dispatch,  # noqa: F401 - legacy daemon facade
    risk_capacity,
)
from trader.application.exit import (
    planned_exits as planned_exits_service,
    exit_bars as exit_bars_service,
    armed_plans,
)
from trader.application.cycle import (
    infra_holds,
    market_snapshot,
)
from trader.application.record import (
    decision_entries,
    plan_review,
    confidence_feedback,
    gross_feedback,
)
from trader.application.record.decision_recorder import DecisionRecorder
from trader.application.execute.cycle_decision import (
    DecisionExecutionContext,
    DecisionExecutionState,
    _OPENING_INTENTS,
    _RELATIVE_ORDER_INTENTS,
    execute_one_cycle_decision as _execute_one_cycle_decision,
)
from trader.execution.risk import RiskGate, RiskLimits
from trader.agent.learnings import consolidator
from trader.agent.learnings import raw_store as raw_learnings
from trader.agent.learnings import store as recall_store_mod
from trader.market import family_regime, fx, macro_calendar, macro_series
from trader.market import execution_eligibility as execution_eligibility_service
from trader.market import market_data as market
from trader.market import volatility as reference_volatility_service
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
    TradePlan,
)
from trader.planning.protocols import SchedulerLike
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
    news_macro_runtime,
    queue_runtime,
    runtime_shutdown,
    universe_intelligence_runtime,
    worker_cycle_context as worker_cycle_context_runtime,
)
from trader.runtime.ib_attach import IBAttachBackoff
from trader.runtime.state_writer import RuntimeStateWriter
from trader.execution import portfolio
from trader.execution.contracts import Order
from trader.execution.broker import (
    SimBroker as SimBroker,
    commission_model_from_name,
    round_trip_cost,
)
from trader.execution.protocols import CommissionModel
from trader.infrastructure.state_db.broker_factory import (
    bootstrap_state_backend,
    make_broker,
    make_scheduler,
    make_trade_plan_store,
)
from trader.planning.indicator_watch import is_armed_plan

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"

log = logging.getLogger("casys-trader")

# Drapeau d'échec définitif du store recall (db corrompu/verrouillé à l'ouverture).
# Posé à True dès le premier échec ; ne pas réessayer à chaque cycle pour éviter
# de logguer la même erreur indéfiniment. Réinitialisable dans les tests.
_RECALL_STORE_FAILED: bool = False

_PLAN_ENTRY_THESIS_MAX_CHARS = 500
_LAST_LLM_REVIEW_KEYS = ("ts", "verdict", "action", "intent", "llm_provider", "llm_model")


def evaluate_plan(*args: object, **kwargs: object) -> object:
    """Legacy daemon monkeypatch hook for planned-exit evaluation."""
    return planned_exits_service.evaluate_plan(*args, **kwargs)


def summarize_gross_rejections(decisions: list[dict]) -> dict | None:
    """Résume les ouvertures recalées faute de marge gross sur un cycle.

    Feedback léger réinjecté au cycle suivant (les sessions LLM sont stateless) :
    l'agent voit qu'il a collectivement sur-proposé contre le plafond gross
    partagé et peut être plus sélectif. Retourne None s'il n'y a rien à signaler.
    """
    return gross_feedback.summarize_gross_rejections(decisions)


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

@dataclass
class CycleProcessState:
    # D7 étage A — dernier passage LLM par (state_dir, symbole), pour la revue
    # périodique garantie du gate de pertinence. Volatile : reset au restart.
    last_llm_at: dict[tuple[str, str], object] = field(default_factory=dict)
    # Feedback gross d'un cycle au suivant (process daemon long-vivant, comme
    # last_llm_at). Keyé par STATE_DIR. Best-effort : vidé au redémarrage.
    last_gross_rejections: dict[str, dict | None] = field(default_factory=dict)


_DEFAULT_CYCLE_PROCESS_STATE = CycleProcessState()
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


def _has_open_trade_plans(*, state_dir: Path, backend: str) -> bool:
    plan_store = make_trade_plan_store(state_dir=state_dir, backend=backend)
    return bool(plan_store.open_plans())


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
        return order_admission.resolve_position_aware_decision(decision, pos_qty)
    return decision


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


def _global_plans_summary(sched: SchedulerLike | None, now: datetime) -> list[dict]:
    if sched is None:
        return []
    try:
        summaries: list[dict] = []
        for watch in sched.active_indicator_watches(now=now):
            armed = is_armed_plan(watch)
            item = {
                "symbol": watch.get("symbol"),
                "id": watch.get("id"),
                "kind": "armed" if armed else "wake",
            }
            if armed:
                order = watch.get("order")
                if isinstance(order, dict) and order.get("intent") is not None:
                    item["intent"] = order.get("intent")
            summaries.append(item)
        return summaries
    except Exception:
        log.warning(
            "[plans_summary] échec construction résumé global (conscience d'état dégradée)",
            exc_info=True,
        )
        return []


def _bounded_plan_text(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    return str(value)[:max_chars]


def _compact_last_llm_review(review: dict | None) -> dict | None:
    if not isinstance(review, dict):
        return None
    compact = {
        key: review[key]
        for key in _LAST_LLM_REVIEW_KEYS
        if key in review and review[key] is None or isinstance(review.get(key), str)
    }
    return compact or None


def _plan_to_context_dict(plan: TradePlan) -> dict:
    return {
        "id": plan.id,
        "symbol": plan.symbol,
        "side": plan.side,
        "entry_price": plan.entry_price,
        "hard_stop_price": plan.hard_stop_price,
        "take_profits": [take_profit.model_dump() for take_profit in plan.take_profits],
        "remaining_quantity": plan.remaining_quantity,
        "last_llm_review": _compact_last_llm_review(plan.last_llm_review),
        "entry_thesis": _bounded_plan_text(plan.entry_thesis, max_chars=_PLAN_ENTRY_THESIS_MAX_CHARS),
    }


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
    return recall_provider.build_recall_provider(
        store,
        lambda: now,  # provider per-cycle : la borne temporelle reste celle du cycle
        embedder=embedder,
        log_warning=log.warning,
    )


def _build_base_context(
    *,
    cycle_id: str,
    now: datetime,
    symbols: list[str],
    snap: object,
    portfolio_fee_estimator: Callable[[str, float, float, float], float | None] | None,
    risk_cfg: dict,
    prices: dict[str, float],
    broker: object,
    gross: float,
    gate_limits: RiskLimits,
    rate_for_symbol: Callable[[str], float],
    cockpit: dict,
    stale_market_data: dict,
    sched: SchedulerLike | None,
    state_dir: Path,
    root: Path,
    attribution_payload: dict,
    meta_performance_payload: dict,
    consolidated_learnings_store: object,
    learnings_store: object,
    max_learnings_in_context: int,
    daily_bars_by_symbol: dict,
    active_families: object,
    requestable_indicator_ids: object,
) -> dict:
    return {
        "now": cycle_id,
        "now_human": market.human_clock(now),
        "market_clocks": market.market_clocks(now, symbols),
        "portfolio": snap.as_context(fee_estimator=portfolio_fee_estimator),
        "risk_limits": risk_cfg,
        "risk_capacity": risk_capacity.risk_capacity_context(
            symbols=[symbol for symbol in symbols if symbol in prices],
            prices=prices,
            broker=broker,
            gross_exposure=gross,
            limits=gate_limits,
            equity=snap.equity,
            rate_of=rate_for_symbol,
            currency_of=fx.currency_for,
        ),
        "semantic": {
            "requestable_indicator_ids": requestable_indicator_ids,
        },
        "cockpit": cockpit,
        "stale_market_data": stale_market_data,
        "active_plans_summary": _global_plans_summary(sched, now),
        # KPI live injectés pour que l'agent décideur pilote sa performance.
        "kpis": live_kpis.compute_live_kpis(state_dir),
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
            guardrails=consolidator.load_guardrails(root / "mandate" / "guardrails.json"),
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


def run_cycle(
    *,
    dry_run: bool,
    now: datetime | None = None,
    symbols_filter: list[str] | None = None,
    sched: SchedulerLike | None = None,
    data_source: object,
    default_wake_minutes: float = 30.0,
    min_wake_minutes: float | None = None,
    max_wake_minutes: float | None = None,
    max_context_requests_per_symbol: int = 2,
    max_indicators_per_request: int = len(DEFAULT_INDICATORS),
    max_model_calls_per_cycle: int = 25,
    max_market_data_age_minutes: float = DEFAULT_MAX_MARKET_DATA_AGE_MINUTES,
    runtime_interval: str = DEFAULT_RUNTIME_INTERVAL,
    runtime_lookback: str = DEFAULT_RUNTIME_LOOKBACK,
    max_learnings_in_context: int = 10,
    indicator_triggers: list[dict] | None = None,
    wake_reasons: list[dict] | None = None,
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
    worker_cycle_context: object | None = None,
    process_state: CycleProcessState | None = None,
) -> dict:
    """Exécute UN cycle. Retourne un rapport structuré (machine-readable)."""
    process_state = process_state or _DEFAULT_CYCLE_PROCESS_STATE
    now = now or datetime.now(timezone.utc)
    cycle_id = now.isoformat()
    universe_cfg = _load_yaml(ROOT / "config" / "universe.yaml")
    risk_cfg = _load_yaml(ROOT / "config" / "risk.yaml")
    # Pin/ban cockpit appliqués à la LECTURE (via l'adaptateur rotation) : le
    # ban prend effet dès le cycle suivant, même si la rotation n'a pas réécrit
    # universe.yaml. Une position ouverte bannie reste gérée.
    universe_cfg = {
        **(universe_cfg or {}),
        "symbols": market_rotation_runtime.load_effective_universe(
            ROOT / "config" / "universe.yaml", ROOT / "state"
        ),
    }
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
    wake_reasons = wake_reasons or []
    wake_reasons_by_symbol: dict[str, list[dict]] = {}
    for reason in wake_reasons:
        symbol = str(reason.get("symbol") or "")
        if symbol:
            wake_reasons_by_symbol.setdefault(symbol, []).append(reason)
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
        backend=os.getenv("CASYS_STATE_BACKEND", "sqlite"),
    )
    plan_store = make_trade_plan_store(
        state_dir=STATE_DIR,
        backend=os.getenv("CASYS_STATE_BACKEND", "sqlite"),
    )
    _cycle_raw_open_plans: tuple[object, ...] = ()
    _cycle_open_plan_rows: tuple[dict, ...] = ()
    if worker_cycle_context is not None:
        _cycle_raw_open_plans = tuple(plan_store.open_plans())
        _cycle_open_plan_rows = tuple(_plan_to_context_dict(plan) for plan in _cycle_raw_open_plans)
    gate = RiskGate(RiskLimits.from_dict(risk_cfg))
    # Paper/exploration : si False, une ouverture SANS hard_stop n'est plus rejetée
    # (stop optionnel, position bornée par les seuls fusibles notionnels). Défaut
    # True = guardrail D6 préservé (live-safe). Voir spec exploration-basse-confiance.
    require_hard_stop = bool(risk_cfg.get("require_hard_stop", True))
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
            "ts": cycle_id,
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
        execution_eligibility_builder=execution_eligibility_service.build_execution_eligibility,
        exit_bars_fetcher=lambda **kwargs: exit_bars_service.fetch_exit_bars_for_open_plans(
            **kwargs,
            fallback_interval=DEFAULT_RUNTIME_INTERVAL,
            exit_interval=EXIT_CHECK_INTERVAL,
            exit_lookback=EXIT_CHECK_LOOKBACK,
        ),
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
    if worker_cycle_context is not None:
        worker_cycle_context.publish(
            worker_cycle_context_runtime.WorkerCycleContext(
                cycle_id=cycle_id,
                as_of=cycle_id,
                open_plans=worker_cycle_context_runtime.OpenPlansSnapshot(
                    rows=_cycle_open_plan_rows,
                    raw_plans=_cycle_raw_open_plans,
                ),
                exit_validation=worker_cycle_context_runtime.ExitValidationInputs(
                    bars_by_symbol=dict(tradable_bars_by_symbol),
                    prices_by_symbol=dict(prices),
                ),
            )
        )
    _log_cycle_progress(
        "[market] loaded ok=%d missing=%d",
        len(prices),
        max(0, len(symbols) - len(prices)),
    )
    if stale_market_data:
        _log_cycle_progress("[market] stale symbols=%s", sorted(stale_market_data))

    planned_exits = planned_exits_service.apply_planned_exits(
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
        runtime_interval=DEFAULT_RUNTIME_INTERVAL,
        exit_check_interval=EXIT_CHECK_INTERVAL,
        exit_check_window_bars=EXIT_CHECK_WINDOW_BARS,
        append_model_performance=_append_model_performance,
        evaluate_plan_fn=evaluate_plan,
        clamp_exit_quantity_fn=order_admission.clamp_exit_quantity,
        execution_blocked_reason_fn=execution_eligibility_service.execution_blocked_reason,
        plan_snapshot_fn=planned_exits_service.plan_snapshot,
    )
    if planned_exits:
        _log_cycle_progress("[exit] planned exits=%d", len(planned_exits))

    exit_watch_triggers = cycle_scheduling.scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol=tradable_bars_by_symbol,
        symbols=tradable_symbols,
        now=now,
        dry_run=dry_run,
        bars_interval=runtime_interval,
        data_source=data_source,
        is_connection_market_error=_is_connection_market_error,
        log_warning=log.warning,
        append_event=_append_event,
        log_cycle_progress=_log_cycle_progress,
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
    base_context = _build_base_context(
        cycle_id=cycle_id,
        now=now,
        symbols=symbols,
        snap=snap,
        portfolio_fee_estimator=portfolio_fee_estimator,
        risk_cfg=risk_cfg,
        prices=prices,
        broker=broker,
        gross=gross,
        gate_limits=gate.limits,
        rate_for_symbol=_rate,
        cockpit=cockpit,
        stale_market_data=stale_market_data,
        sched=sched,
        state_dir=STATE_DIR,
        root=ROOT,
        attribution_payload=attribution_payload,
        meta_performance_payload=meta_performance_payload,
        consolidated_learnings_store=consolidated_learnings_store,
        learnings_store=learnings_store,
        max_learnings_in_context=max_learnings_in_context,
        daily_bars_by_symbol=daily_bars_by_symbol,
        active_families=active_families,
        requestable_indicator_ids=DEFAULT_INDICATORS,
    )
    try:
        regime_families = base_context["regime_families"]
        _runtime_state_writer().write_json_state(
            "last_regime.json",
            {
                "schema_version": 1,
                "as_of": cycle_id,
                "coverage": {
                    "status": "partial" if regime_families else "missing",
                    "basis": "active_tradable_universe",
                    "symbols_with_daily_bars": len(daily_bars_by_symbol),
                    "family_count": len(regime_families),
                },
                "regime_families": regime_families,
            },
        )
    except Exception as exc:  # noqa: BLE001 - advisory context must not break decisions
        log.warning("family regime snapshot write failed: %s", exc)

    # Feedback léger du cycle précédent : si des ouvertures ont été recalées faute
    # de marge gross, on le signale à l'agent (marge partagée entre tous les
    # symboles) — présent seulement si non vide.
    _gross_feedback = process_state.last_gross_rejections.get(str(STATE_DIR))
    if _gross_feedback:
        base_context["gross_budget_feedback"] = _gross_feedback

    report: dict = {
        "ts": cycle_id,
        "dry_run": dry_run,
        "code_version": code_version.current_code_version(ROOT),
        "symbols_due": symbols_to_decide,
        "planned_exits": planned_exits,
        "exit_watch_triggers": exit_watch_triggers,
        "indicator_triggers": indicator_triggers,
        "wake_reasons": wake_reasons,
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
        brief_ref_provider=lambda symbol, now: news_macro_runtime.brief_ref_for_symbol(
            state_dir=STATE_DIR,
            symbol=symbol,
            at=now,
        ),
        recall_store=_recall_store,
        merge_gate_feedback=confidence_feedback.merge_gate_feedback,
        model_calls_used_getter=lambda: model_calls_used,
        agent_trace_path=STATE_DIR / "agent_trace.log",
    )
    record_decision = recorder.record

    if sched is not None:
        cycle_scheduling.ensure_default_wake(sched, now=now, default_wake_minutes=default_wake_minutes)

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
        reference_volatility_for_symbol=reference_volatility_service.reference_volatility_for_symbol,
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
        last_llm_at=process_state.last_llm_at,
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
        append_event=_append_event,
        append_model_performance=_append_model_performance,
        logger=log,
    )

    if queue_decide_enabled and task_ledger is not None:
        # Mode file : enfile 1 tâche par symbole et collecte via polling.
        # Le pool DecidePool tourne en arrière-plan (démarré dans main()).
        from trader.application.decide.planner_batch import (
            build_symbol_facts,
        )
        from trader.application.decide.queue_dispatch import iter_decide_results_via_queue
        from trader.application.decide.recent_decisions import recent_decisions_by_symbol
        _last_review = plan_review.last_review_by_symbol(plan_store, decidable)
        # Push anti-répétition : N dernières décisions authentiques par symbole (même
        # sans position ouverte, là où _last_review ne couvre que les plans ouverts).
        # Une seule lecture du ledger, groupée.
        _recent_decisions = recent_decisions_by_symbol(decision_ledger_store, symbols=decidable)
        _active_watches = planner_batch._active_watch_summaries_by_symbol(
            sched=sched, symbols=decidable, now=now,
        )
        symbol_facts_by_sym = {
            sym: {
                "indicator_triggers": triggers_by_symbol.get(sym, []),
                "wake_reasons": wake_reasons_by_symbol.get(sym, []),
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
            cycle_id=cycle_id,
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
        decisions_by_symbol, model_calls_used = planner_batch.batch_decide(
            decidable=decidable,
            mandate=mandate_txt,
            memory=memory_txt,
            shared_context=base_context,
            triggers_by_symbol=triggers_by_symbol,
            wake_reasons_by_symbol=wake_reasons_by_symbol,
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
            last_review_by_symbol=plan_review.last_review_by_symbol(plan_store, decidable),
            market_context_by_symbol=execution_eligibility,
            decision_timeout_s=decision_timeout_s,
            decision_batch_size=decision_batch_size,
            decision_batch_parallelism=decision_batch_parallelism,
            agent_tools_enabled=agent_tools_enabled,
            learnings_recall_provider=_recall_provider,
            indicator_request_resolver=resolve_indicator_requests,
            event_appender=_append_event,
        )
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

        decision = decisions_by_symbol.get(sym)
        if execution_state.opening_batch_timed_out and decision is not None and decision.intent in _OPENING_INTENTS:
            undecided_symbols.add(sym)
            execution_state.deferred_opening_symbols.add(sym)

        # FIX 2 : les symboles non décidés ce cycle (skippés dead/budget côté
        # decide queue, ou ouvertures retenues après timeout d'ouverture côté
        # execute queue) sont EXCLUS du fallback HOLD synthétique — ils seront
        # redécidés au prochain cycle. Le mode batch garde son comportement
        # historique (HOLD no_decision_in_batch) via undecided_symbols vide.
        if sym in undecided_symbols:
            _log_cycle_progress(
                "[decision %d/%d] %s queue_decide_deferred — aucun HOLD synthétique",
                index, len(symbols_to_decide), sym,
            )
            _append_event("queue_decide_deferred", symbol=sym, cycle_id=cycle_id)
            continue

        decision = decision or codex_client.Decision.hold(sym, "no_decision_in_batch")
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

    # Revue effective seulement si le modèle a réellement statué ET si la
    # décision n'a pas été reportée par le stop de batch d'ouvertures. Les
    # ouvertures différées doivent rester périodic_review au cycle suivant, pas
    # quiet_gate pendant 4h.
    for sym in decidable:
        if sym in execution_state.deferred_opening_symbols:
            continue
        decision = decisions_by_symbol.get(sym)
        if decision is not None and decision_entries.counts_as_llm_review(decision):
            process_state.last_llm_at[(str(STATE_DIR), sym)] = now

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
        gross_rejection_cache=process_state.last_gross_rejections,
        summarize_gross_rejections=summarize_gross_rejections,
        collect_macro=macro_series.maybe_collect,
        state_backend=os.getenv("CASYS_STATE_BACKEND", "sqlite"),
        write_current_report=_write_current_report,
        append_event=_append_event,
        logger=log,
    )

    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="casys-trader daemon (boucle runtime)")
    parser.add_argument("--live", action="store_true", help="exécute réellement les ordres (défaut: dry-run)")
    parser.add_argument("--once", action="store_true", help="un seul cycle puis sortie")
    parser.add_argument("--poll", type=float, default=30.0, help="secondes entre deux vérifications du scheduler")
    parser.add_argument("--default-wake-minutes", type=float, default=30.0, help="cadence globale par défaut")
    parser.add_argument("--min-wake-minutes", type=float, default=None, help="borne basse optionnelle du réveil agent (défaut: aucune)")
    parser.add_argument("--max-wake-minutes", type=float, default=None, help="borne haute optionnelle du réveil agent (défaut: aucune — l'agent est autonome)")
    parser.add_argument("--max-context-requests-per-symbol", type=int, default=2, help="nombre max de requêtes indicateurs par symbole")
    parser.add_argument(
        "--max-indicators-per-request",
        type=int,
        default=len(DEFAULT_INDICATORS),
        help="cap opérateur optionnel ; par défaut tout le catalogue d'indicateurs est retourné",
    )
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
    return parser


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
    parser = _build_arg_parser()
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
    _state_backend = os.getenv("CASYS_STATE_BACKEND", "sqlite")
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
    process_state = _DEFAULT_CYCLE_PROCESS_STATE
    cycle_run = run_cycle

    def _run_cycle_with_process_state(**kwargs):
        return cycle_run(**kwargs, process_state=process_state)

    # Ref partagée vers le data_source courant : les workers de file la lisent via
    # make_indirect_get_bars(handle.get) — jamais de capture de l'objet (remplacé en run).
    _ds_handle = data_source_runtime.DataSourceHandle()
    _worker_cycle_context = worker_cycle_context_runtime.WorkerCycleContextHandle()

    # Services du tour d'outils grain-1 (spec queue tool-round §4, issue #2) :
    # construits au boot, consommés par le handler decide quand agent_tools_enabled.
    _decide_tool_services = queue_runtime.build_decide_tool_services(
        get_data_source=_ds_handle.get,
        worker_cycle_context=_worker_cycle_context,
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
    _news_macro_runner = news_macro_runtime.NewsMacroAnalysisRunner()
    _universe_intelligence_runner = (
        universe_intelligence_runtime.UniverseIntelligenceRunner()
    )

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
                # Pin/ban cockpit appliqués à la lecture (parité run_cycle) :
                # un ban retire les réveils du scheduler dès la prochaine boucle.
                symbols = market_rotation_runtime.load_effective_universe(
                    ROOT / "config" / "universe.yaml", STATE_DIR
                )
                sched.reconcile_universe(symbols)
                expired_watches = cycle_scheduling.expire_indicator_watches(
                    sched,
                    now=loop_now,
                    append_event=_append_event,
                    log_info=log.info,
                )
                wake_reasons = cycle_scheduling.wake_reasons_from_expired_watches(expired_watches, now=loop_now)
                indicator_triggers = (
                    []
                    if args.once or bootstrap
                    else cycle_scheduling.scan_indicator_watches(
                        symbols,
                        sched=sched,
                        now=loop_now,
                        data_source=data_source,
                        is_connection_market_error=_is_connection_market_error,
                        log_warning=log.warning,
                        append_event=_append_event,
                        log_cycle_progress=_log_cycle_progress,
                    )
                )
                due_symbols = cycle_scheduling.select_due_symbols(
                    symbols,
                    sched=sched,
                    once=args.once,
                    bootstrap=bootstrap,
                    now=loop_now,
                )
                bootstrap = False
                state_backend = os.getenv("CASYS_STATE_BACKEND", "sqlite")
                protection_cycle_due = bool(
                    not due_symbols
                    and _has_open_trade_plans(state_dir=STATE_DIR, backend=state_backend)
                )
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
                    wake_reasons=wake_reasons,
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
                    worker_cycle_context=_worker_cycle_context,
                )
                if not due_symbols and not protection_cycle_due:
                    wait = sched.seconds_until_wake(symbols)
                    sleep_seconds = args.poll if wait <= 0 else min(wait, args.poll)
                    model_cap = None if _queue_decide_enabled else args.max_model_calls_per_cycle
                    _write_status(
                        "idle_waiting_for_wake",
                        current_symbol=None,
                        symbols_due=[],
                        symbols_total=0,
                        decisions_done=0,
                        dry_run=dry_run,
                        model_calls_used=0,
                        max_model_calls_per_cycle=model_cap,
                        next_due_in_seconds=wait,
                    )
                    log.debug("aucun symbole dû — pause %.0fs", sleep_seconds)
                else:
                    symbols_filter = due_symbols if due_symbols else []
                    report = cycle_dispatch.dispatch_run_cycle(
                        run_cycle_fn=_run_cycle_with_process_state,
                        context=cycle_context,
                        now=loop_now,
                        symbols_filter=symbols_filter,
                    )
                    log.debug("cycle: %s", json.dumps(report, ensure_ascii=False))
                    cycle_reporting.persist_cycle_report(report, writer=_runtime_state_writer())

                    if args.once:
                        stop_after_iteration = True
                    else:
                        wait = sched.seconds_until_wake(symbols)
                        sleep_seconds = min(wait, args.poll)
                        log.info("[sleep] next_check_in=%.0fs next_due_in=%.0fs", sleep_seconds, wait)
                if not args.once:
                    # Déclenché après le cycle afin que l'analyste voie aussi le
                    # dernier snapshot macro collecté par la finalisation. Les
                    # deux runners restent non bloquants et coalescent les
                    # triggers reçus pendant un appel LLM.
                    _news_macro_runner.trigger(
                        config_dir=ROOT / "config",
                        state_dir=STATE_DIR,
                        loop_now=loop_now,
                        logger=log,
                    )
                    _universe_intelligence_runner.trigger(
                        config_dir=ROOT / "config",
                        state_dir=STATE_DIR,
                        loop_now=loop_now,
                        logger=log,
                    )
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
            universe_intelligence_runner=_universe_intelligence_runner,
            news_macro_runner=_news_macro_runner,
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
