"""Runtime bootstrap for decide and execute task queues."""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class LoggerLike(Protocol):
    def info(self, *args: object) -> None: ...
    def warning(self, *args: object) -> None: ...


class RecoverableLedger(Protocol):
    path: object

    def recover_on_boot(self, *, now_ms: int) -> None: ...


class StartablePool(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


NowMs = Callable[[], int]
_DEFAULT_DECIDE_LEASE_MS = 1_800_000
_DECIDE_SESSION_LEASE_MARGIN_FACTOR = 1.25


def default_now_ms() -> int:
    return int(time.time() * 1000)


def _default_logger() -> logging.Logger:
    return logging.getLogger("casys-trader")


def _decide_session_lease_ms(
    *,
    decision_timeout_s: int,
    max_rounds: int,
    backend_count: int,
) -> int:
    timeout_s = max(int(decision_timeout_s), 1)
    rounds = max(int(max_rounds), 1)
    backends = max(int(backend_count), 1)
    # Pire cas par backend : open=timeout+30, prompts=(max_rounds+1)*(timeout+15),
    # close=15. Le runner peut recommencer depuis zero sur chaque backend.
    per_backend_s = (timeout_s + 30) + (rounds + 1) * (timeout_s + 15) + 15
    worst_case_s = backends * per_backend_s
    return max(_DEFAULT_DECIDE_LEASE_MS, int(worst_case_s * _DECIDE_SESSION_LEASE_MARGIN_FACTOR * 1000))


def _default_make_decide_handler(
    *,
    codex_client: object,
    tool_services: object = None,
    session_backends: list | None = None,
) -> object:
    from trader.application.decide_handler import make_decide_handler

    return make_decide_handler(
        codex_client=codex_client,
        tool_services=tool_services,
        session_backends=session_backends,
    )


def _default_make_execute_order_handler(**kwargs: object) -> object:
    from trader.application.execute_order_handler import make_execute_order_handler

    return make_execute_order_handler(**kwargs)


def _default_task_ledger_cls() -> type:
    from trader.infrastructure.queue.ledger import TaskLedger

    return TaskLedger


def _default_resource_pools_cls() -> type:
    from trader.infrastructure.queue.pools import ResourcePools

    return ResourcePools


def _default_decide_pool_cls() -> type:
    from trader.infrastructure.queue.decide_pool import DecidePool

    return DecidePool


def _default_open_state_db(path: Path) -> object:
    from trader.infrastructure.state_db.connection import open_state_db

    return open_state_db(path)


def _default_sqlite_broker_cls() -> type:
    from trader.infrastructure.state_db.broker_store import SqliteBroker

    return SqliteBroker


def _default_sqlite_plan_store_cls() -> type:
    from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore

    return SqliteTradePlanStore


@dataclass(frozen=True)
class DecideQueueFactories:
    task_ledger_cls: type | None = None
    resource_pools_cls: type | None = None
    decide_pool_cls: type | None = None
    make_decide_handler: Callable[..., object] | None = None

    @classmethod
    def defaults(cls) -> "DecideQueueFactories":
        return cls(
            task_ledger_cls=_default_task_ledger_cls(),
            resource_pools_cls=_default_resource_pools_cls(),
            decide_pool_cls=_default_decide_pool_cls(),
            make_decide_handler=_default_make_decide_handler,
        )


@dataclass(frozen=True)
class ExecuteQueueFactories:
    open_state_db: Callable[[Path], object] | None = None
    sqlite_broker_cls: type | None = None
    sqlite_plan_store_cls: type | None = None
    task_ledger_cls: type | None = None
    resource_pools_cls: type | None = None
    decide_pool_cls: type | None = None
    make_execute_order_handler: Callable[..., object] | None = None

    @classmethod
    def defaults(cls) -> "ExecuteQueueFactories":
        return cls(
            open_state_db=_default_open_state_db,
            sqlite_broker_cls=_default_sqlite_broker_cls(),
            sqlite_plan_store_cls=_default_sqlite_plan_store_cls(),
            task_ledger_cls=_default_task_ledger_cls(),
            resource_pools_cls=_default_resource_pools_cls(),
            decide_pool_cls=_default_decide_pool_cls(),
            make_execute_order_handler=_default_make_execute_order_handler,
        )


@dataclass(frozen=True)
class DecideQueueRuntime:
    enabled: bool
    ledger: RecoverableLedger | None = None
    pool: StartablePool | None = None


@dataclass(frozen=True)
class ExecuteQueueRuntime:
    enabled: bool
    db: object | None = None
    broker: object | None = None
    plan_store: object | None = None
    ledger: RecoverableLedger | None = None
    pool: StartablePool | None = None


@dataclass(frozen=True)
class QueueRuntimes:
    decide: DecideQueueRuntime
    execute: ExecuteQueueRuntime


def start_decide_queue(
    *,
    enabled: bool,
    state_dir: Path,
    parallelism: int,
    decision_batch_size: int,
    default_decision_batch_size: int,
    codex_client: object,
    decision_timeout_s: int = 900,
    tool_services: object = None,
    now_ms_fn: NowMs = default_now_ms,
    logger: LoggerLike | None = None,
    factories: DecideQueueFactories | None = None,
) -> DecideQueueRuntime:
    if not enabled:
        return DecideQueueRuntime(enabled=False)

    log = logger or _default_logger()
    resolved = factories or DecideQueueFactories.defaults()
    task_ledger_cls = resolved.task_ledger_cls or _default_task_ledger_cls()
    resource_pools_cls = resolved.resource_pools_cls or _default_resource_pools_cls()
    decide_pool_cls = resolved.decide_pool_cls or _default_decide_pool_cls()
    make_decide_handler = resolved.make_decide_handler or _default_make_decide_handler

    session_backends = None
    lease_ms = _DEFAULT_DECIDE_LEASE_MS
    if tool_services is not None:
        from trader.agent import llm

        router = llm.build_default_router_from_env(spark_model=codex_client.DEFAULT_MODEL)
        session_backends = [b for b in router.backends if isinstance(b, llm.AcpxBackend)]
        if not session_backends:
            raise RuntimeError(
                "[queue_decide] aucun AcpxBackend : le tour d'outils en file requiert un transport acpx "
                "(vérifier TRADER_ACPX_BIN / provider spark)"
            )
        if not any(shutil.which(b.acpx_bin) for b in session_backends):
            raise RuntimeError(
                "[queue_decide] acpx introuvable sur le PATH pour le tour d'outils en file "
                "(vérifier TRADER_ACPX_BIN)"
            )
        lease_ms = _decide_session_lease_ms(
            decision_timeout_s=decision_timeout_s,
            max_rounds=getattr(tool_services, "max_rounds", 1),
            backend_count=len(session_backends),
        )

    ledger = task_ledger_cls(state_dir / "task_ledger.db")
    ledger.recover_on_boot(now_ms=now_ms_fn())
    pools = resource_pools_cls({"acpx": parallelism})
    pool = decide_pool_cls(
        ledger=ledger,
        pools=pools,
        handlers={
            "decide": make_decide_handler(
                codex_client=codex_client,
                tool_services=tool_services,
                session_backends=session_backends,
            )
        },
        num_workers=parallelism,
        now_fn=time.time,
        lease_ms=lease_ms,
    )
    pool.start()
    log.info(
        "[queue_decide] pool démarré num_workers=%d db=%s",
        parallelism,
        ledger.path,
    )
    if decision_batch_size != default_decision_batch_size:
        log.warning(
            "[queue_decide] CASYS_DECISION_BATCH_SIZE=%d IGNORÉ en mode queue "
            "(grain-symbole : 1 tâche = 1 symbole, pas de chunk). Sans effet.",
            decision_batch_size,
        )
    return DecideQueueRuntime(enabled=True, ledger=ledger, pool=pool)


def start_execute_queue(
    *,
    enabled_raw: bool,
    state_backend: str,
    state_dir: Path,
    commission_model: object,
    now_ms_fn: NowMs = default_now_ms,
    logger: LoggerLike | None = None,
    factories: ExecuteQueueFactories | None = None,
) -> ExecuteQueueRuntime:
    log = logger or _default_logger()
    enabled = enabled_raw and state_backend.lower() == "sqlite"
    if enabled_raw and not enabled:
        log.warning("[queue_execute] CASYS_QUEUE_EXECUTE_ENABLED=1 ignoré — requiert CASYS_STATE_BACKEND=sqlite")
    if not enabled:
        return ExecuteQueueRuntime(enabled=False)

    resolved = factories or ExecuteQueueFactories.defaults()
    open_state_db = resolved.open_state_db or _default_open_state_db
    sqlite_broker_cls = resolved.sqlite_broker_cls or _default_sqlite_broker_cls()
    sqlite_plan_store_cls = resolved.sqlite_plan_store_cls or _default_sqlite_plan_store_cls()
    task_ledger_cls = resolved.task_ledger_cls or _default_task_ledger_cls()
    resource_pools_cls = resolved.resource_pools_cls or _default_resource_pools_cls()
    decide_pool_cls = resolved.decide_pool_cls or _default_decide_pool_cls()
    make_execute_order_handler = resolved.make_execute_order_handler or _default_make_execute_order_handler

    db = open_state_db(state_dir / "casys.db")
    broker = sqlite_broker_cls(db, commission_model=commission_model, json_path=state_dir / "broker.json")
    plan_store = sqlite_plan_store_cls(db, json_path=state_dir / "trade_plans.json")
    ledger = task_ledger_cls(db)
    ledger.recover_on_boot(now_ms=now_ms_fn())
    pool = decide_pool_cls(
        ledger=ledger,
        pools=resource_pools_cls({"portfolio": 1}),
        handlers={
            "execute_order": make_execute_order_handler(
                db=db,
                broker=broker,
                plan_store=plan_store,
                ledger=ledger,
            )
        },
        num_workers=1,
        now_fn=time.time,
    )
    pool.start()
    log.info("[queue_execute] pool démarré db=%s", ledger.path)
    return ExecuteQueueRuntime(
        enabled=True,
        db=db,
        broker=broker,
        plan_store=plan_store,
        ledger=ledger,
        pool=pool,
    )


def build_decide_tool_services(
    *,
    get_data_source: Callable[[], object],
    learnings_db_path: Path,
    max_context_requests_per_symbol: int,
    max_indicators_per_request: int,
    max_rounds: int = 1,
    logger: LoggerLike | None = None,
) -> object | None:
    """Construit les ToolRoundServices du pool decide au boot (spec §4).

    - ``get_bars`` : indirection vers le data_source COURANT (handle) — jamais de
      capture (la ref est remplacée en cours de run).
    - recall : LearningsStore ouvert au boot si ``learnings.db`` existe (SQLite
      locké, thread-safe) ; ``now_fn`` dynamique — la borne temporelle suit
      chaque appel. ⚠️ si la db n'existe pas ENCORE au boot, recall restera
      indisponible jusqu'au prochain redémarrage (limitation documentée).

    Retourne ``None`` si ``max_rounds < 1`` (garde-fou config).
    """
    from datetime import datetime, timezone

    from trader.agent.learnings.store import LearningsStore
    from trader.application.decide_one import ToolRoundServices
    from trader.application.learnings_recall import build_recall_provider
    from trader.market.data_source import make_indirect_get_bars

    log = logger or _default_logger()
    if max_rounds < 1:
        log.warning("[queue_decide] tool max_rounds=%d invalide (<1) — outils désactivés", max_rounds)
        return None

    recall_provider = None
    if learnings_db_path.exists():
        try:
            store = LearningsStore(str(learnings_db_path))
            recall_provider = build_recall_provider(
                store,
                lambda: datetime.now(timezone.utc),
                log_warning=log.warning,
            )
        except Exception as exc:  # noqa: BLE001 — recall optionnel, jamais bloquant au boot
            log.warning("[queue_decide] LearningsStore indisponible (%s) — recall désactivé", exc)
    else:
        log.info("[queue_decide] learnings.db absent au boot — recall indisponible ce run")

    return ToolRoundServices(
        get_bars=make_indirect_get_bars(get_data_source),
        learnings_recall_provider=recall_provider,
        max_context_requests_per_symbol=max_context_requests_per_symbol,
        max_indicators_per_request=max_indicators_per_request,
        max_rounds=max_rounds,
    )


def start_queue_runtimes(
    *,
    state_dir: Path,
    decide_enabled: bool,
    decision_parallelism: int,
    decision_batch_size: int,
    default_decision_batch_size: int,
    codex_client: object,
    execute_enabled_raw: bool,
    state_backend: str,
    commission_model: object,
    decision_timeout_s: int = 900,
    decide_tool_services: object = None,
    now_ms_fn: NowMs = default_now_ms,
    logger: LoggerLike | None = None,
) -> QueueRuntimes:
    decide = start_decide_queue(
        enabled=decide_enabled,
        state_dir=state_dir,
        parallelism=decision_parallelism,
        decision_batch_size=decision_batch_size,
        default_decision_batch_size=default_decision_batch_size,
        codex_client=codex_client,
        decision_timeout_s=decision_timeout_s,
        tool_services=decide_tool_services,
        now_ms_fn=now_ms_fn,
        logger=logger,
    )
    execute = start_execute_queue(
        enabled_raw=execute_enabled_raw,
        state_backend=state_backend,
        state_dir=state_dir,
        commission_model=commission_model,
        now_ms_fn=now_ms_fn,
        logger=logger,
    )
    return QueueRuntimes(decide=decide, execute=execute)
