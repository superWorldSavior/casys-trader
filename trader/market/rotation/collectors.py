"""rotation_collectors — collecte des symboles « sticky » et override par défaut.

Fonctions exportées :
- sticky_collector(*, positions_fn, plans_fn) -> set[str]
- build_positions_fn(state_dir) -> Callable[[], dict]
- build_plans_fn(state_dir) -> Callable[[], list]
- default_override_fn(payload) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def sticky_collector(
    *,
    positions_fn: Callable[[], dict],
    plans_fn: Callable[[], Iterable],
) -> set[str]:
    """Retourne l'union des symboles à protéger (non-dégradables).

    - Positions dont la quantité est non nulle (long ou short).
    - Symboles référencés par des trade plans ouverts.

    Args:
        positions_fn: Callable sans argument → dict[str, position] où
            chaque position a un attribut ``quantity``.
        plans_fn: Callable sans argument → itérable d'objets ayant
            un attribut ``.symbol``.

    Returns:
        Ensemble des symboles sticky.
    """
    positions = positions_fn()
    sticky = {sym for sym, pos in positions.items() if pos.quantity != 0}

    for plan in plans_fn():
        sticky.add(plan.symbol)

    return sticky


# ---------------------------------------------------------------------------
# PROD wiring — fail-safe si l'état est absent
# ---------------------------------------------------------------------------


def build_positions_fn(state_dir: str | Path) -> Callable[[], dict]:
    """Retourne une closure qui lit les positions depuis SimBroker.

    Si ``broker.json`` est absent ou corrompu, retourne {} sans lever.

    Args:
        state_dir: Répertoire contenant ``broker.json``.

    Returns:
        Callable[[], dict[str, Position]]
    """
    state_dir = Path(state_dir)

    def _positions() -> dict:
        try:
            # Garde-fou : SimBroker crée broker.json si absent (starting_cash défaut).
            # En mode sqlite, le shadow est généré par bootstrap ; en mode json, la
            # création au premier accès serait parasite (cash par défaut incorrect).
            # On retourne {} proprement plutôt que de créer un fichier fantôme.
            if not (state_dir / "broker.json").exists():
                return {}
            from trader.execution.broker import SimBroker

            broker = SimBroker(state_dir / "broker.json")
            return broker.positions()
        except Exception:
            return {}

    return _positions


def build_plans_fn(state_dir: str | Path) -> Callable[[], list]:
    """Retourne une closure qui lit les plans ouverts depuis TradePlanStore.

    Lit ``trade_plans.json`` — le MÊME fichier que celui écrit par le daemon
    (``daemon.py``). Si absent ou corrompu, retourne [] sans lever.

    Args:
        state_dir: Répertoire contenant ``trade_plans.json``.

    Returns:
        Callable[[], list[TradePlan]]
    """
    state_dir = Path(state_dir)

    def _plans() -> list:
        try:
            from trader.planning.trade_plan import TradePlanStore

            store = TradePlanStore(state_dir / "trade_plans.json")
            return store.open_plans()
        except Exception:
            return []

    return _plans


# ---------------------------------------------------------------------------
# Default override — NO-OP (le vrai override LLM viendra plus tard)
# ---------------------------------------------------------------------------


def default_override_fn(payload: Any) -> dict:  # noqa: ANN401
    """Override par défaut : ne modifie rien.

    Args:
        payload: Ignoré.

    Returns:
        ``{"add": [], "remove": []}``
    """
    return {"add": [], "remove": []}
