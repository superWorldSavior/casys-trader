"""Factories d'état — sélection du backend via CASYS_STATE_BACKEND.

Factories disponibles :
    make_broker            → SimBroker (json) ou SqliteBroker (sqlite)
    make_trade_plan_store  → TradePlanStore (json) ou SqliteTradePlanStore (sqlite)
    make_scheduler         → Scheduler (json) ou SqliteScheduler (sqlite)

Valeurs backend acceptées (insensibles à la casse) :
    "json"   (défaut) → stores JSON, comportement strictement inchangé.
    "sqlite"          → stores SQLite (migration one-shot idempotente au boot).

Usage (daemon.py) ::

    from trader.infrastructure.state_db.broker_factory import make_broker, make_trade_plan_store, make_scheduler

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
    sched = make_scheduler(
        state_dir=STATE_DIR,
        backend=os.getenv("CASYS_STATE_BACKEND", "json"),
    )

Les valeurs inconnues lèvent ValueError explicite (AX : fast-fail, machine-readable).
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_VALID_BACKENDS = ("json", "sqlite")


def bootstrap_state_backend(
    *,
    state_dir: Path,
    starting_cash: float,
    commission_model,
    backend: str = "json",
) -> None:
    """Amorce ordonné du backend SQLite : migrations + import JSON + 3 shadows.

    Doit être appelé UNE FOIS en début de process (dans main()), AVANT toute
    lecture d'état ou rotation, pour garantir que les shadows JSON sont frais et
    que les 3 stores partagent la même connexion SQLite (via open_state_db).

    Si ``backend != "sqlite"``, cette fonction est un no-op strict (aucun fichier
    créé, aucune connexion ouverte).

    Idempotent : peut être appelé plusieurs fois sans erreur ni double-import
    (les sentinels state_imports + open_state_db assurent l'idempotence).

    Args:
        state_dir:        répertoire d'état (ex. ROOT / "state").
        starting_cash:    cash initial si aucun état broker n'existe.
        commission_model: CommissionModel injecté dans SqliteBroker.
        backend:          "json" (défaut, no-op) ou "sqlite".
    """
    state_dir = Path(state_dir)
    if backend.lower() != "sqlite":
        return

    from trader.infrastructure.state_db.connection import open_state_db
    from trader.infrastructure.state_db.migrations import (
        import_broker_from_json,
        import_trade_plans_from_json,
        import_scheduler_from_json,
    )
    from trader.infrastructure.state_db.broker_store import SqliteBroker
    from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore
    from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

    db_path = state_dir / "casys.db"
    db = open_state_db(db_path)

    # integrity_check — signal d'exploitation (log uniquement, ne crashe pas)
    ic_results = db.integrity_check()
    if ic_results != ["ok"]:
        log.error(
            "[state_db] integrity_check ÉCHEC %s: %s",
            db_path,
            "; ".join(ic_results),
        )

    # 1. Migrations + imports JSON (idempotents via sentinels state_imports)
    import_broker_from_json(db, state_dir / "broker.json", starting_cash=starting_cash)
    import_trade_plans_from_json(db, state_dir / "trade_plans.json")
    import_scheduler_from_json(db, state_dir / "scheduler.json")

    # 2. Régénère les 3 shadows depuis SQLite (rattrape crash entre COMMIT et shadow write)
    SqliteBroker(db, commission_model=commission_model, json_path=state_dir / "broker.json").regenerate_shadow()
    SqliteTradePlanStore(db, json_path=state_dir / "trade_plans.json").regenerate_shadow()
    SqliteScheduler(db, json_path=state_dir / "scheduler.json").regenerate_shadow()

    log.info("[bootstrap] state_backend sqlite amorcé (shadows régénérés)")


def make_broker(
    *,
    state_dir: Path,
    starting_cash: float,
    commission_model,
    backend: str = "json",
):
    """Construit et retourne un Broker (SimBroker ou SqliteBroker) selon *backend*.

    Args:
        state_dir:        répertoire d'état (ex. ROOT / "state").
        starting_cash:    cash initial si aucun état n'existe.
        commission_model: CommissionModel à injecter.
        backend:          "json" (défaut) ou "sqlite". Toute autre valeur → ValueError.

    Returns:
        SimBroker  si backend == "json".
        SqliteBroker si backend == "sqlite".

    Raises:
        ValueError: backend inconnu.
    """
    state_dir = Path(state_dir)
    backend = backend.lower()

    if backend == "json":
        from trader.execution.broker import SimBroker  # import local — pas de circular dep

        log.debug("[broker_factory] backend=json → SimBroker(%s)", state_dir / "broker.json")
        return SimBroker(
            state_dir / "broker.json",
            starting_cash=starting_cash,
            commission_model=commission_model,
        )

    if backend == "sqlite":
        from trader.infrastructure.state_db.connection import open_state_db
        from trader.infrastructure.state_db.migrations import import_broker_from_json
        from trader.infrastructure.state_db.broker_store import SqliteBroker

        db_path = state_dir / "casys.db"
        json_path = state_dir / "broker.json"

        log.debug("[broker_factory] backend=sqlite → open_state_db(%s)", db_path)
        db = open_state_db(db_path)
        import_broker_from_json(db, json_path, starting_cash=starting_cash)
        broker = SqliteBroker(db, commission_model=commission_model, json_path=json_path)
        # Rattrape un shadow stale/absent depuis SQLite au boot (crash entre COMMIT et shadow write)
        broker.regenerate_shadow()
        return broker

    raise ValueError(
        f"CASYS_STATE_BACKEND inconnu : {backend!r}. Valeurs acceptées : {_VALID_BACKENDS}"
    )


def make_trade_plan_store(
    *,
    state_dir: Path,
    backend: str = "json",
):
    """Construit et retourne un TradePlanStore selon *backend*.

    Args:
        state_dir: répertoire d'état (ex. ROOT / "state").
        backend:   "json" (défaut) ou "sqlite". Toute autre valeur → ValueError.

    Returns:
        TradePlanStore       si backend == "json".
        SqliteTradePlanStore si backend == "sqlite".

    Raises:
        ValueError: backend inconnu.
    """
    state_dir = Path(state_dir)
    backend = backend.lower()

    if backend == "json":
        from trader.planning.trade_plan import TradePlanStore  # import local

        log.debug(
            "[broker_factory] trade_plan backend=json → TradePlanStore(%s)",
            state_dir / "trade_plans.json",
        )
        return TradePlanStore(state_dir / "trade_plans.json")

    if backend == "sqlite":
        from trader.infrastructure.state_db.connection import open_state_db
        from trader.infrastructure.state_db.migrations import import_trade_plans_from_json
        from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore

        db_path = state_dir / "casys.db"
        json_path = state_dir / "trade_plans.json"

        log.debug(
            "[broker_factory] trade_plan backend=sqlite → open_state_db(%s)", db_path
        )
        db = open_state_db(db_path)
        import_trade_plans_from_json(db, json_path)
        store = SqliteTradePlanStore(db, json_path=json_path)
        # Rattrape un shadow stale/absent depuis SQLite au boot
        store.regenerate_shadow()
        return store

    raise ValueError(
        f"CASYS_STATE_BACKEND inconnu : {backend!r}. Valeurs acceptées : {_VALID_BACKENDS}"
    )


def make_scheduler(
    *,
    state_dir: Path,
    backend: str = "json",
):
    """Construit et retourne un Scheduler (Scheduler ou SqliteScheduler) selon *backend*.

    Args:
        state_dir: répertoire d'état (ex. ROOT / "state").
        backend:   "json" (défaut) ou "sqlite". Toute autre valeur → ValueError.

    Returns:
        Scheduler        si backend == "json".
        SqliteScheduler  si backend == "sqlite".

    Raises:
        ValueError: backend inconnu.
    """
    state_dir = Path(state_dir)
    backend = backend.lower()

    if backend == "json":
        from trader.planning.scheduler import Scheduler  # import local — pas de circular dep  # noqa: PLC0415

        log.debug(
            "[broker_factory] scheduler backend=json → Scheduler(%s)",
            state_dir / "scheduler.json",
        )
        return Scheduler(state_dir / "scheduler.json")

    if backend == "sqlite":
        from trader.infrastructure.state_db.connection import open_state_db  # noqa: PLC0415
        from trader.infrastructure.state_db.migrations import import_scheduler_from_json  # noqa: PLC0415
        from trader.infrastructure.state_db.scheduler_store import SqliteScheduler  # noqa: PLC0415

        db_path = state_dir / "casys.db"
        json_path = state_dir / "scheduler.json"

        log.debug(
            "[broker_factory] scheduler backend=sqlite → open_state_db(%s)", db_path
        )
        db = open_state_db(db_path)
        import_scheduler_from_json(db, json_path)
        store = SqliteScheduler(db, json_path=json_path)
        # Rattrape un shadow stale/absent depuis SQLite au boot
        store.regenerate_shadow()
        return store

    raise ValueError(
        f"CASYS_STATE_BACKEND inconnu : {backend!r}. Valeurs acceptées : {_VALID_BACKENDS}"
    )
