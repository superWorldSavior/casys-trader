"""Factories d'état paper — SQLite canonique, adaptateurs JSON explicites.

Factories disponibles :
    make_broker            → SqliteBroker (défaut) ou SimBroker (json explicite)
    make_trade_plan_store  → SqliteTradePlanStore (sqlite)
    make_scheduler         → SqliteScheduler (défaut) ou Scheduler (json explicite)

Valeurs backend acceptées (insensibles à la casse) :
    "sqlite" (défaut) → stores SQLite (migration one-shot idempotente au boot).
    "json"             → broker/scheduler legacy explicites ; plans JSON supprimés.

Usage (daemon.py) ::

    from trader.infrastructure.state_db.broker_factory import make_broker, make_trade_plan_store, make_scheduler

    broker = make_broker(
        state_dir=STATE_DIR,
        starting_cash=starting_equity,
        commission_model=commission_model,
        backend="sqlite",
    )
    plan_store = make_trade_plan_store(
        state_dir=STATE_DIR,
        backend="sqlite",
    )
    sched = make_scheduler(
        state_dir=STATE_DIR,
        backend="sqlite",
    )

Les valeurs inconnues lèvent ValueError explicite (AX : fast-fail, machine-readable).
"""
from __future__ import annotations

import logging
from pathlib import Path

from trader.infrastructure.state_db.sim_broker import SimBroker

log = logging.getLogger(__name__)

_VALID_BACKENDS = ("json", "sqlite")
CANONICAL_STATE_BACKEND = "sqlite"


def bootstrap_state_backend(
    *,
    state_dir: Path,
    starting_cash: float,
    commission_model,
    backend: str = CANONICAL_STATE_BACKEND,
) -> None:
    """Amorce ordonné du backend SQLite : migrations + import JSON.

    Doit être appelé UNE FOIS en début de process (dans main()), AVANT toute
    lecture d'état ou rotation, pour garantir que les stores partagent la même
    connexion SQLite (via open_state_db).

    Si ``backend != "sqlite"``, cette fonction est un no-op strict (aucun fichier
    créé, aucune connexion ouverte).

    Idempotent : peut être appelé plusieurs fois sans erreur ni double-import
    (les sentinels state_imports + open_state_db assurent l'idempotence).

    Args:
        state_dir:        répertoire d'état (ex. ROOT / "state").
        starting_cash:    cash initial si aucun état broker n'existe.
        commission_model: CommissionModel injecté dans SqliteBroker.
        backend:          "sqlite" (défaut) ou "json" explicite (no-op).
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

    log.info("[bootstrap] state_backend sqlite amorcé")


def make_broker(
    *,
    state_dir: Path,
    starting_cash: float,
    commission_model,
    backend: str = CANONICAL_STATE_BACKEND,
):
    """Construit et retourne un Broker (SimBroker ou SqliteBroker) selon *backend*.

    Args:
        state_dir:        répertoire d'état (ex. ROOT / "state").
        starting_cash:    cash initial si aucun état n'existe.
        commission_model: CommissionModel à injecter.
        backend:          "sqlite" (défaut) ou "json" explicite. Toute autre valeur → ValueError.

    Returns:
        SimBroker  si backend == "json".
        SqliteBroker si backend == "sqlite".

    Raises:
        ValueError: backend inconnu.
    """
    state_dir = Path(state_dir)
    backend = backend.lower()

    if backend == "json":
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

        log.debug("[broker_factory] backend=sqlite → open_state_db(%s)", db_path)
        db = open_state_db(db_path)
        import_broker_from_json(db, state_dir / "broker.json", starting_cash=starting_cash)
        return SqliteBroker(db, commission_model=commission_model)

    raise ValueError(
        f"backend d'état inconnu : {backend!r}. Valeurs acceptées : {_VALID_BACKENDS}"
    )


def make_trade_plan_store(
    *,
    state_dir: Path,
    backend: str = "sqlite",
):
    """Construit et retourne le store de plans selon *backend*.

    Args:
        state_dir: répertoire d'état (ex. ROOT / "state").
        backend:   "sqlite" (défaut). "json" fast-fail. Toute autre valeur → ValueError.

    Returns:
        SqliteTradePlanStore si backend == "sqlite".

    Raises:
        ValueError: backend inconnu.
    """
    state_dir = Path(state_dir)
    backend = backend.lower()

    if backend == "json":
        raise NotImplementedError(
            "trade plan JSON backend supprimé; utiliser le backend sqlite"
        )

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
        return SqliteTradePlanStore(db)

    raise ValueError(
        f"backend d'état inconnu : {backend!r}. Valeurs acceptées : {_VALID_BACKENDS}"
    )


def make_scheduler(
    *,
    state_dir: Path,
    backend: str = CANONICAL_STATE_BACKEND,
):
    """Construit et retourne un Scheduler (Scheduler ou SqliteScheduler) selon *backend*.

    Args:
        state_dir: répertoire d'état (ex. ROOT / "state").
        backend:   "sqlite" (défaut) ou "json" explicite. Toute autre valeur → ValueError.

    Returns:
        Scheduler        si backend == "json".
        SqliteScheduler  si backend == "sqlite".

    Raises:
        ValueError: backend inconnu.
    """
    state_dir = Path(state_dir)
    backend = backend.lower()

    if backend == "json":
        from trader.infrastructure.state_db.scheduler_json import Scheduler  # noqa: PLC0415

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

        log.debug(
            "[broker_factory] scheduler backend=sqlite → open_state_db(%s)", db_path
        )
        db = open_state_db(db_path)
        import_scheduler_from_json(db, state_dir / "scheduler.json")
        return SqliteScheduler(db)

    raise ValueError(
        f"backend d'état inconnu : {backend!r}. Valeurs acceptées : {_VALID_BACKENDS}"
    )
