"""rotation_collectors — collecte des symboles « sticky » et override par défaut.

Fonctions exportées :
- sticky_collector(*, positions_fn, plans_fn, watches_fn, pending_symbols_fn) -> set[str]
- default_override_fn(payload) -> dict
"""

from __future__ import annotations

from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def sticky_collector(
    *,
    positions_fn: Callable[[], dict],
    plans_fn: Callable[[], Iterable],
    watches_fn: Callable[[], Iterable] | None = None,
    pending_symbols_fn: Callable[[], Iterable[str]] | None = None,
) -> set[str]:
    """Retourne l'union des symboles à protéger (non-dégradables).

    - Positions dont la quantité est non nulle (long ou short).
    - Symboles référencés par des trade plans ouverts.
    - Symboles référencés par des indicator watches actives.
    - Symboles avec un replay stale→fresh pending récent.

    Args:
        positions_fn: Callable sans argument → dict[str, position] où
            chaque position a un attribut ``quantity``.
        plans_fn: Callable sans argument → itérable d'objets ayant
            un attribut ``.symbol``.
        watches_fn: Callable optionnel → itérable de mappings ou d'objets
            ayant un symbole.
        pending_symbols_fn: Callable optionnel → itérable de symboles dont
            le replay stale→fresh doit survivre à la rotation.

    Returns:
        Ensemble des symboles sticky.
    """
    positions = positions_fn()
    sticky = {sym for sym, pos in positions.items() if pos.quantity != 0}

    for plan in plans_fn():
        sticky.add(plan.symbol)

    if watches_fn is not None:
        for watch in watches_fn():
            symbol = (
                watch.get("symbol")
                if isinstance(watch, dict)
                else getattr(watch, "symbol", None)
            )
            if symbol:
                sticky.add(str(symbol))

    if pending_symbols_fn is not None:
        sticky.update(str(symbol) for symbol in pending_symbols_fn() if symbol)

    return sticky


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
