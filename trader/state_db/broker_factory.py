"""make_broker — sélection du backend broker via CASYS_STATE_BACKEND.

Valeurs acceptées (insensibles à la casse) :
    "json"   (défaut) → SimBroker JSON, comportement strictement inchangé.
    "sqlite"          → SqliteBroker sur StateDb (migration one-shot idempotente au boot).

Usage (daemon.py) ::

    from trader.state_db.broker_factory import make_broker

    broker = make_broker(
        state_dir=STATE_DIR,
        starting_cash=starting_equity,
        commission_model=commission_model,
        backend=os.getenv("CASYS_STATE_BACKEND", "json"),
    )

La valeur inconnue lève ValueError explicite (AX : fast-fail, machine-readable).
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_VALID_BACKENDS = ("json", "sqlite")


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
        from trader.tools.execution import SimBroker  # import local — pas de circular dep

        log.debug("[broker_factory] backend=json → SimBroker(%s)", state_dir / "broker.json")
        return SimBroker(
            state_dir / "broker.json",
            starting_cash=starting_cash,
            commission_model=commission_model,
        )

    if backend == "sqlite":
        from trader.state_db.connection import StateDb
        from trader.state_db.migrations import import_broker_from_json
        from trader.state_db.broker_store import SqliteBroker

        db_path = state_dir / "casys.db"
        json_path = state_dir / "broker.json"

        log.debug("[broker_factory] backend=sqlite → StateDb(%s)", db_path)
        db = StateDb(db_path)
        import_broker_from_json(db, json_path, starting_cash=starting_cash)
        broker = SqliteBroker(db, commission_model=commission_model, json_path=json_path)
        # Rattrape un shadow stale/absent depuis SQLite au boot (crash entre COMMIT et shadow write)
        broker.regenerate_shadow()
        return broker

    raise ValueError(
        f"CASYS_STATE_BACKEND inconnu : {backend!r}. Valeurs acceptées : {_VALID_BACKENDS}"
    )
