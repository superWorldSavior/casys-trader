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
import signal
import sqlite3
import time
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
)
import trader.application.execute.risk_capacity as risk_capacity
from trader.application.exit import (
    planned_exits as planned_exits_service,
    exit_bars as exit_bars_service,
)
from trader.application.cycle import (
    decision_scope,
    infra_holds,
    market_snapshot,
    schedule as cycle_schedule,
)
from trader.application.record import (
    decision_entries,
    confidence_feedback,
    gross_feedback,
)
from trader.application.record.decision_ledger_rows import collect_session_by_symbol
from trader.application.record.decision_recorder import DecisionRecorder
from trader.application.execute.cycle_decision import (
    DecisionExecutionContext,
    DecisionExecutionState,
    _OPENING_INTENTS,
    _RELATIVE_ORDER_INTENTS,
    execute_one_cycle_decision as _execute_one_cycle_decision,
)
from trader.domain.execution.risk_gate import RiskGate
from trader.domain.market import execution_eligibility as execution_eligibility_service
from trader.domain.risk import RiskLimits
from trader.agent.learnings import consolidator
from trader.agent.learnings import raw_store as raw_learnings
from trader.agent.learnings import store as recall_store_mod
from trader.market import family_regime, fx, macro_calendar, macro_series  # noqa: F401
from trader.infrastructure.market_sources import gdelt
from trader.market import market_data as market
from trader.market import volatility as reference_volatility_service
from trader.domain.market.features import DEFAULT_INDICATORS
from trader.domain.market.gross_priority import PriorityItem, gross_execution_order
from trader.infrastructure.market_sources.data_source import (
    CompositeDataSource,
    YFinanceDataSource,
    make_indirect_get_bars,
    parse_data_sources_config,
)
from trader.infrastructure.market_sources.ib_source import IBDataSource, connect_ib
from trader.market import news_feed
from trader.domain.planning.protocols import SchedulerLike
from trader.domain import decision_identity
from trader.domain.process_trace import new_runtime_run_id
from trader.infrastructure.files import decision_ledger
from trader.support.metadata import code_version
from trader.reporting.read_models import attribution, meta_performance
from trader.runtime.agent_cycle_context import (
    build_base_context as _build_base_context,
    global_plans_summary as _global_plans_summary,  # noqa: F401 - legacy daemon hook
    plan_to_context_dict as _plan_to_context_dict,
)
from trader.runtime import (
    agent_trace_runtime,
    cycle_finalization,
    cycle_dispatch,
    cycle_reporting,
    cycle_scheduling,
    company_intelligence_runtime,
    data_source_runtime,
    daemon_bootstrap,
    decision_dispatch_runtime,
    market_rotation_runtime,
    learnings_sync_runtime,
    news_macro_runtime,
    queue_runtime,
    runtime_shutdown,
    universe_intelligence_runtime,
    trader_research_context,
    worker_cycle_context as worker_cycle_context_runtime,
)
from trader.runtime.cycle_process_state import CycleProcessState
from trader.runtime.ib_attach import IBAttachBackoff
from trader.runtime.process_pilot import ProcessPilot, build_process_pilot
from trader.runtime.state_writer import RuntimeStateWriter
from trader.application.portfolio import snapshot as portfolio
from trader.application.execute.fee_estimate import round_trip_cost
from trader.application.execute.protocols import CommissionModel
from trader.domain.contracts import Order
from trader.infrastructure.brokers.commission_models import commission_model_from_name
from trader.infrastructure.state_db.sim_broker import SimBroker as SimBroker  # noqa: F401 - legacy test seam
from trader.infrastructure.state_db.broker_factory import (
    CANONICAL_STATE_BACKEND,
    bootstrap_state_backend,
    make_broker,
    make_scheduler,
    make_trade_plan_store,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
ROOT = _SOURCE_ROOT
STATE_DIR = ROOT / "state"

log = logging.getLogger("casys-trader")

# Drapeau d'échec définitif du store recall (db corrompu/verrouillé à l'ouverture).
# Posé à True dès le premier échec ; ne pas réessayer à chaque cycle pour éviter
# de logguer la même erreur indéfiniment. Réinitialisable dans les tests.
_RECALL_STORE_FAILED: bool = False

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

_DEFAULT_CYCLE_PROCESS_STATE = CycleProcessState()


def _llm_gate_store():
    """Retourne le store SQLite de cadence LLM, ou None hors backend sqlite."""
    from trader.infrastructure.state_db.llm_gate_store import try_open_llm_gate_store

    return try_open_llm_gate_store(STATE_DIR, CANONICAL_STATE_BACKEND)


def _hydrate_last_llm_at(process_state: CycleProcessState) -> None:
    """Recharge ``last_llm_at`` depuis casys.db (no-op si backend != sqlite)."""
    store = _llm_gate_store()
    if store is None:
        return
    process_state.last_llm_at.update(store.load_all())


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


def _process_causes_by_symbol(
    symbols: list[str],
    *,
    triggers_by_symbol: dict[str, list[dict]],
    wake_reasons_by_symbol: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Project the observable wake evidence attached to each admitted symbol."""
    causes_by_symbol: dict[str, list[dict]] = {}
    for symbol in symbols:
        causes: list[dict] = []
        for cause_type, items in (
            ("indicator_trigger", triggers_by_symbol.get(symbol, [])),
            ("wake_reason", wake_reasons_by_symbol.get(symbol, [])),
        ):
            for item in items:
                causes.append(
                    {
                        "type": cause_type,
                        **{
                            field: item[field]
                            for field in ("reason", "watch_id", "on_trigger", "observed_at")
                            if item.get(field) is not None
                        },
                    }
                )
        causes_by_symbol[symbol] = causes or [{"type": "scheduled_due"}]
    return causes_by_symbol


def _decision_readback(
    *,
    decision_ledger_store: object,
    decision_entry: dict,
) -> dict:
    """Reread and compare the exact durable decision row, fail closed."""
    symbol = str(decision_entry.get("symbol") or "")
    decision_id = str(decision_entry.get("decision_id") or "")
    expected_process = {
        "process_instance_id": decision_entry.get("process_instance_id"),
        "attempt_id": decision_entry.get("attempt_id"),
        "runtime_run_id": decision_entry.get("runtime_run_id"),
    }
    expected = {
        "symbol": symbol,
        "decision_id": decision_id,
        "process_decision_id": decision_id,
        **expected_process,
    }
    try:
        durable_row = decision_ledger_store.read_by_decision_id(decision_id)
    except Exception as exc:  # noqa: BLE001 - missing closure proof must stay explicit
        log.warning("decision ledger readback failed decision_id=%s: %s", decision_id, exc)
        return {"status": "unavailable", **expected, "error": type(exc).__name__}

    durable_process = durable_row.get("process") if isinstance(durable_row, dict) else None
    observed = {
        "symbol": durable_row.get("symbol") if isinstance(durable_row, dict) else None,
        "decision_id": durable_row.get("decision_id") if isinstance(durable_row, dict) else None,
        "process_decision_id": (durable_process.get("decision_id") if isinstance(durable_process, dict) else None),
        "process_instance_id": (
            durable_process.get("process_instance_id") if isinstance(durable_process, dict) else None
        ),
        "attempt_id": durable_process.get("attempt_id") if isinstance(durable_process, dict) else None,
        "runtime_run_id": (durable_process.get("runtime_run_id") if isinstance(durable_process, dict) else None),
    }
    if observed == expected:
        return {"status": "verified", **expected}
    return {"status": "mismatch", **expected, "observed": observed}


def _admit_governed_symbols(
    *,
    process_pilot: ProcessPilot,
    symbols: list[str],
    triggers_by_symbol: dict[str, list[dict]],
    wake_reasons_by_symbol: dict[str, list[dict]],
) -> list[str]:
    """Attach process evidence without changing the trader dispatch scope.

    The pilot is observational.  Recovery metadata may affect how its trace is
    eventually closed, but it must never suppress a due symbol or an LLM call.
    Business idempotency and execution reconciliation remain owned by the
    durable decision/execute queues and broker stores.
    """
    process_pilot.admit_many(
        symbols,
        causes_by_symbol=_process_causes_by_symbol(
            symbols,
            triggers_by_symbol=triggers_by_symbol,
            wake_reasons_by_symbol=wake_reasons_by_symbol,
        ),
    )
    return list(symbols)


def _queue_process_identity_by_symbol(
    *,
    process_pilot: ProcessPilot | None,
    symbols: list[str],
) -> dict[str, dict[str, str]]:
    """Return the small immutable join key needed by asynchronous decide tasks.

    This is strictly observational: missing or malformed pilot metadata is not
    a dispatch gate. The full governance bundle remains on the process event
    and durable decision record rather than being copied into every task.
    """
    if process_pilot is None:
        return {}
    fields = ("process_instance_id", "attempt_id", "runtime_run_id")
    identities: dict[str, dict[str, str]] = {}
    for symbol in symbols:
        try:
            candidate = process_pilot.decision_fields(symbol)
        except Exception as exc:  # noqa: BLE001 - pilot evidence must not block dispatch
            log.warning("process pilot identity unavailable symbol=%s: %s", symbol, exc)
            continue
        identity = {
            field: value.strip()
            for field in fields
            if isinstance((value := candidate.get(field)), str) and value.strip()
        }
        if identity:
            identities[symbol] = identity
    return identities


def _build_governed_record_decision(
    *,
    process_pilot: ProcessPilot,
    decision_ledger_store: object,
    record_decision: Callable[[dict], None],
    settled_symbols: set[str],
) -> Callable[[dict], None]:
    """Correlate, persist, reread, then finish one governed process attempt."""

    def record(decision_entry: dict) -> None:
        symbol = str(decision_entry["symbol"])
        decision_entry.update(process_pilot.decision_fields(symbol))
        if decision_entry.get("action") == "HOLD":
            decision_entry.setdefault("admission_status", "not_required")
            decision_entry.setdefault("effect_status", "not_applied")
        record_decision(decision_entry)
        decision_entry["decision_readback"] = _decision_readback(
            decision_ledger_store=decision_ledger_store,
            decision_entry=decision_entry,
        )
        process_pilot.finish_decision(symbol, decision_entry)
        settled_symbols.add(symbol)

    return record


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
    process_pilot: ProcessPilot | None = None,
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
        backend=CANONICAL_STATE_BACKEND,
    )
    plan_store = make_trade_plan_store(
        state_dir=STATE_DIR,
        backend=CANONICAL_STATE_BACKEND,
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

    # Store SQLite dérivé, toujours ouvrable/créable. Le worker de maintenance
    # l'alimente en arrière-plan ; les readers voient les commits via WAL.
    _learnings_db_path = STATE_DIR / "learnings.db"
    _recall_store: recall_store_mod.LearningsStore | None = None
    global _RECALL_STORE_FAILED
    if not _RECALL_STORE_FAILED:
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
        fx_rate_provider=data_source_runtime.build_fx_rate_provider(config_dir=ROOT / "config"),
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

    # Planned exits are a separate deterministic protection mechanism and are
    # explicitly outside the per-symbol decision-process boundary admitted below.
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

    admitted_process_symbols: set[str] = set()
    settled_process_symbols: set[str] = set()
    if process_pilot is not None:
        admitted_process_symbols = set(symbols_to_decide)
        symbols_to_decide = _admit_governed_symbols(
            process_pilot=process_pilot,
            symbols=symbols_to_decide,
            triggers_by_symbol=triggers_by_symbol,
            wake_reasons_by_symbol=wake_reasons_by_symbol,
        )

    snap = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity, fx_rate_of=_rate)
    # Folds must not crash on MissingFxRate; a live position without FX
    # fail-closes to +inf so new opens cannot understate gross.
    gross = risk_capacity.gross_exposure(
        broker, prices, rate_of=snapshot.try_rate_for_symbol
    )
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
    if worker_cycle_context is not None:
        worker_cycle_context.publish_attribution(
            cycle_id=cycle_id,
            attribution=attribution_payload,
        )
    meta_performance_payload = meta_performance.compute_meta_performance(STATE_DIR)
    if _recall_store is not None:
        try:
            # Make the rules actually projected into this cycle citeable.  This
            # also migrates legacy global rules to their stable IDs lazily.
            _recall_store.sync_global_rules(
                [
                    str(rule["rule_id"])
                    for rule in consolidated_learnings_store.read().get("global", [])
                    if isinstance(rule, dict) and rule.get("rule_id")
                ],
                ts=cycle_id,
            )
        except Exception as exc:  # noqa: BLE001 - global MemRL is advisory
            log.warning("global learning rule sync failed: %s", exc)
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
        "session_by_symbol": collect_session_by_symbol(
            symbols_to_decide,
            snapshot=lambda symbol: market.session_snapshot(symbol, now=now),
        ),
        "model_calls_used": 0,
    }

    def refresh_report_portfolio() -> None:
        latest = portfolio.snapshot(broker, lambda s: prices.get(s, 0.0), starting_equity, fx_rate_of=_rate)
        report["portfolio"] = latest.as_context(fee_estimator=portfolio_fee_estimator)

    _write_current_report(report)
    mandate_txt, memory_txt = mem.read_mandate(), mem.read_memory()
    model_call_counter = decision_dispatch_runtime.ModelCallCounter()
    held_symbols = {holding.symbol for holding in snap.holdings if holding.quantity}
    company_context_by_symbol, mandate_context_by_symbol = (
        trader_research_context.load_trader_research_context(
            config_dir=ROOT / "config",
            state_dir=STATE_DIR,
            symbols=symbols_to_decide,
            active_at=now,
            held_symbols=held_symbols,
        )
    )

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
        model_calls_used_getter=lambda: model_call_counter.used,
        agent_trace_appender=agent_trace_runtime.build_agent_trace_appender(
            STATE_DIR / "agent_trace.log"
        ),
        company_context_provider=lambda symbol: company_context_by_symbol.get(symbol),
        mandate_context_provider=lambda symbol: mandate_context_by_symbol.get(symbol),
        learning_ingester=(
            (
                lambda: _recall_store.ingest_jsonl(
                    STATE_DIR / "learnings.jsonl", source="runtime"
                )
            )
            if _recall_store is not None
            else None
        ),
    )
    record_decision = recorder.record
    if process_pilot is not None:
        record_decision = _build_governed_record_decision(
            process_pilot=process_pilot,
            decision_ledger_store=decision_ledger_store,
            record_decision=record_decision,
            settled_symbols=settled_process_symbols,
        )

    if sched is not None:
        cycle_scheduling.ensure_default_wake(sched, now=now, default_wake_minutes=default_wake_minutes)

    has_armed_triggers = any(
        str(trigger.get("on_trigger")) == "EXECUTE_ORDER"
        and isinstance(trigger.get("order"), dict)
        for trigger in indicator_triggers
    )
    prepared_scope = decision_scope.prepare_decision_scope(
        decision_scope.DecisionScopeRequest(
            symbols_to_decide=symbols_to_decide,
            prices=prices,
            stale_market_data=stale_market_data,
            execution_eligibility=execution_eligibility,
            held_symbols=held_symbols,
            indicator_triggers=indicator_triggers,
            positions=broker.positions() if has_armed_triggers else {},
            cockpit=cockpit,
            tradable_bars_by_symbol=tradable_bars_by_symbol,
            daily_bars_by_symbol=daily_bars_by_symbol,
            now=now,
            state_key=str(STATE_DIR),
            last_llm_at=process_state.last_llm_at,
            last_wake_reasons=process_state.last_wake_reasons,
            regime_families=base_context["regime_families"],
            active_families=active_families,
            wake_source=sched,
            triggers_by_symbol=triggers_by_symbol,
            runtime_data_source_by_sym=runtime_data_source_by_sym,
            runtime_interval=runtime_interval,
            daily_interval=COCKPIT_DAILY_INTERVAL,
            reference_volatility_for_symbol=(
                reference_volatility_service.reference_volatility_for_symbol
            ),
        )
    )
    armed_resolution = prepared_scope.armed_resolution
    if sched is not None:
        for watch_id in armed_resolution.consume_watch_ids:
            sched.remove_indicator_watch(watch_id)
    for progress in armed_resolution.progress_logs:
        _log_cycle_progress(progress.message, *progress.args)
    for event in armed_resolution.events:
        _append_event(event.name, **event.payload)

    armed_decisions = armed_resolution.decisions
    armed_plan_ids = armed_resolution.plan_ids
    armed_plan_orders = armed_resolution.plan_orders
    stale_armed_plans = armed_resolution.stale_plans
    armed_reference_volatilities = armed_resolution.reference_volatilities

    quiet_gate = prepared_scope.quiet_gate
    gated_symbols = quiet_gate.gated_symbols
    if gated_symbols:
        _log_cycle_progress("[gate] quiet symbols=%s (pas d'appel LLM)", gated_symbols)
        for entry in quiet_gate.entries:
            entry["schedule_effect"] = cycle_schedule.read_schedule_effect(
                sched,
                sym=str(entry["symbol"]),
                now=now,
                expected_next_wake=None,
            )
            record_decision(entry)

    decidable = prepared_scope.decidable
    _log_cycle_progress(
        "[batch] deciding symbols=%d/%d", len(decidable), len(symbols_to_decide)
    )
    _write_status(
        "deciding_batch",
        current_symbol=None,
        decisions_done=0,
        symbols_total=len(symbols_to_decide),
        batch_size=len(decidable),
    )
    analysis_bars_by_symbol = prepared_scope.analysis_bars_by_symbol
    analysis_timeframe_by_symbol = prepared_scope.analysis_timeframe_by_symbol
    analysis_symbols = prepared_scope.analysis_symbols
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
        decision_id_for_symbol=lambda symbol: decision_identity.decision_id(
            str(report["ts"]), len(report["decisions"]), symbol
        ),
        process_identity_for_symbol=(
            process_pilot.decision_fields if process_pilot is not None else lambda _symbol: {}
        ),
        rate_for_symbol=_rate,
        append_event=_append_event,
        append_model_performance=_append_model_performance,
        logger=log,
    )

    dispatch_result = decision_dispatch_runtime.dispatch_decisions(
        decision_dispatch_runtime.DecisionDispatchRequest(
            queue_decide_enabled=queue_decide_enabled,
            task_ledger=task_ledger,
            decidable=decidable,
            mandate=mandate_txt,
            memory=memory_txt,
            shared_context=base_context,
            triggers_by_symbol=triggers_by_symbol,
            wake_reasons_by_symbol=wake_reasons_by_symbol,
            analysis_bars_by_symbol=analysis_bars_by_symbol,
            analysis_timeframe_by_symbol=analysis_timeframe_by_symbol,
            analysis_symbols=analysis_symbols,
            data_age_by_symbol=data_age_by_symbol,
            execution_eligibility=execution_eligibility,
            now=now,
            scheduler=sched,
            plan_store=plan_store,
            decision_ledger_store=decision_ledger_store,
            decision_timeout_s=decision_timeout_s,
            agent_tools_enabled=agent_tools_enabled,
            cycle_id=cycle_id,
            symbols_to_decide=symbols_to_decide,
            broker=broker,
            execution_state=execution_state,
            execution_context=execution_ctx,
            runtime_interval=runtime_interval,
            runtime_lookback=runtime_lookback,
            max_context_requests_per_symbol=max_context_requests_per_symbol,
            max_indicators_per_request=max_indicators_per_request,
            max_model_calls=max_model_calls_per_cycle,
            decision_batch_size=decision_batch_size,
            decision_batch_parallelism=decision_batch_parallelism,
            learnings_recall_provider=_recall_provider,
            indicator_request_resolver=resolve_indicator_requests,
            event_appender=_append_event,
            opening_intents=_OPENING_INTENTS,
            model_call_counter=model_call_counter,
            now_fn=time.time,
            company_context_by_symbol=company_context_by_symbol,
            mandate_context_by_symbol=mandate_context_by_symbol,
            learning_feedback_provider=(
                _recall_store.feedback_by_decision_ids
                if _recall_store is not None
                else None
            ),
            process_identity_by_symbol=_queue_process_identity_by_symbol(
                process_pilot=process_pilot,
                symbols=decidable,
            ),
        ),
        resolve_decision_for_routing=_resolve_decision_for_execution_routing,
        execute_decision=_execute_one_cycle_decision,
    )
    decisions_by_symbol = dispatch_result.decisions_by_symbol
    model_calls_used = dispatch_result.model_calls_used
    undecided_symbols = dispatch_result.undecided_symbols
    streamed_decision_symbols = dispatch_result.streamed_decision_symbols
    execution_state = dispatch_result.execution_state
    snap = execution_state.snap
    gross = execution_state.gross
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
            expected_stale_wake = None
            if sched is not None:
                sched.set_stale_streak(sym, new_streak)
                expected_stale_wake = sched.set_symbol_next_wake_in(
                    sym,
                    minutes=wake_minutes,
                    now=now,
                )
            schedule_effect = cycle_schedule.read_schedule_effect(
                sched,
                sym=sym,
                now=now,
                expected_next_wake=expected_stale_wake,
            )

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
                stale_hold.entry["schedule_effect"] = schedule_effect
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
                if process_pilot is not None:
                    process_pilot.defer(
                        sym,
                        outcome_code="stale_backoff",
                        effect_status=("verified" if schedule_effect.get("status") == "verified" else "unknown"),
                        effect_refs=(
                            {
                                "type": "schedule_readback",
                                "symbol": sym,
                                **schedule_effect,
                            },
                        ),
                    )
                    settled_process_symbols.add(sym)
            continue
        if sym not in prices:
            _log_cycle_progress("[decision %d/%d] %s skipped no_price", index, len(symbols_to_decide), sym)
            if process_pilot is not None:
                process_pilot.defer(sym, outcome_code="no_price")
                settled_process_symbols.add(sym)
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
            if process_pilot is not None:
                process_pilot.defer(sym, outcome_code="queue_decide_deferred")
                settled_process_symbols.add(sym)
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
    llm_gate_store = _llm_gate_store()
    for sym in decidable:
        if sym in execution_state.deferred_opening_symbols:
            continue
        decision = decisions_by_symbol.get(sym)
        if decision is not None and decision_entries.counts_as_llm_review(decision):
            process_state.last_llm_at[(str(STATE_DIR), sym)] = now
            if llm_gate_store is not None:
                llm_gate_store.record(str(STATE_DIR), sym, now)
            process_state.last_wake_reasons[(str(STATE_DIR), sym)] = (
                quiet_gate.persistent_reasons.get(sym, ())
            )

    report["model_calls_used"] = model_calls_used
    if process_pilot is not None:
        for symbol in sorted(admitted_process_symbols - settled_process_symbols):
            process_pilot.defer(symbol, outcome_code="cycle_completed_without_decision")
            settled_process_symbols.add(symbol)
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
            curation_provider=_recall_store,
        ),
        gross_rejection_cache=process_state.last_gross_rejections,
        summarize_gross_rejections=summarize_gross_rejections,
        collect_macro=macro_series.maybe_collect,
        collect_gdelt=gdelt.maybe_collect,
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

    # SIGTERM (systemd stop, kill) doit dérouler le finally comme le SIGINT du
    # superviseur : sans handler Python termine net — pools non joints, WAL non
    # flushé, pid file jamais libéré.
    def _sigterm_to_exit(signum: int, frame: object) -> None:  # noqa: ARG001
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _sigterm_to_exit)

    # Bootstrap ordonné AVANT toute lecture d'état ou rotation runtime :
    # rotation mensuelle JSONL, bootstrap SQLite canonique, puis scheduler.
    _runtime_state = daemon_bootstrap.bootstrap_runtime_state(
        state_dir=STATE_DIR,
        config_dir=ROOT / "config",
        commission_model=commission_model,
        now=now(),
        logger=log,
        bootstrap_state_backend_fn=bootstrap_state_backend,
        make_scheduler_fn=make_scheduler,
    )
    sched = _runtime_state.scheduler
    process_pilot = build_process_pilot(
        repo_root=_SOURCE_ROOT,
        state_dir=STATE_DIR,
        runtime_run_id=new_runtime_run_id(),
        clock=now,
    )
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
    _hydrate_last_llm_at(process_state)
    cycle_run = run_cycle

    def _run_cycle_with_process_state(**kwargs):
        return cycle_run(
            **kwargs,
            process_state=process_state,
            process_pilot=process_pilot,
        )

    # Ref partagée vers le data_source courant : les workers de file la lisent via
    # make_indirect_get_bars(handle.get) — jamais de capture de l'objet (remplacé en run).
    _ds_handle = data_source_runtime.DataSourceHandle()
    _worker_cycle_context = worker_cycle_context_runtime.WorkerCycleContextHandle()
    _learning_sync_runner = learnings_sync_runtime.LearningSyncRunner(
        state_dir=STATE_DIR,
        get_bars=make_indirect_get_bars(_ds_handle.get),
        logger=log,
    )

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
        state_backend=CANONICAL_STATE_BACKEND,
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

    def _on_company_brief_written(event: dict) -> None:
        universe_intelligence_runtime.trigger_company_brief_refresh(
            event,
            runner=_universe_intelligence_runner,
            config_dir=ROOT / "config",
            state_dir=STATE_DIR,
            loop_now=datetime.now(timezone.utc),
            logger=log,
        )

    _company_intelligence_runner = company_intelligence_runtime.CompanyIntelligenceRuntime(
        config_dir=ROOT / "config",
        state_dir=STATE_DIR,
        logger=log,
        on_brief_written=_on_company_brief_written,
    )
    _company_intelligence_runner.trigger(
        scope="current",
        depth="screen",
        trigger="daemon_boot",
    )
    _company_intelligence_runner.trigger(
        scope="active",
        depth="deep",
        trigger="daemon_boot_deep",
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
                protection_cycle_due = bool(
                    not due_symbols
                    and _has_open_trade_plans(
                        state_dir=STATE_DIR,
                        backend=CANONICAL_STATE_BACKEND,
                    )
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
                    _company_intelligence_runner.trigger(
                        scope="current",
                        depth="screen",
                        trigger="post_cycle",
                        as_of=loop_now,
                    )
                    _company_intelligence_runner.trigger(
                        scope="active",
                        depth="deep",
                        trigger="selected_deep",
                        as_of=loop_now,
                    )
                    _universe_intelligence_runner.trigger(
                        config_dir=ROOT / "config",
                        state_dir=STATE_DIR,
                        loop_now=loop_now,
                        logger=log,
                        venues=universe_intelligence_runtime.VENUES,
                    )
                    _learning_sync_runner.trigger(reason="post_cycle")
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
            learning_sync_runner=_learning_sync_runner,
            company_intelligence_runner=_company_intelligence_runner,
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
