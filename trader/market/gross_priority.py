"""Ordre d'exécution déterministe des décisions pour une admission gross équitable.

Principe : on ne clampe RIEN (la liberté de sizing reste à l'agent). On se contente
d'ordonner l'exécution du cycle pour que le RiskGate — qui admet ou rejette chaque
ordre à pleine taille — arbitre au mérite plutôt que selon l'ordre arbitraire de la
liste :

- les réducteurs (CLOSE/REDUCE) passent d'abord → ils libèrent de la marge gross ;
- puis les ouvertures, par conviction décroissante → les meilleures idées sont
  servies en premier ; quand la marge est épuisée, ce sont les plus basse-conviction
  qui se font rejeter (déterministe, reproductible), pas les dernières de la liste ;
- le reste (HOLD, etc.) ensuite — il ne consomme pas de gross.

Primitive pure (zéro I/O, zéro hasard/temps). Spec :
docs/superpowers/specs/2026-06-30-gross-budget-allocator-design.md
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_REDUCING_INTENTS = frozenset({"CLOSE", "REDUCE"})
_OPENING_INTENTS = frozenset({"OPEN_LONG", "OPEN_SHORT", "REVERSE", "ADD"})


@dataclass(frozen=True)
class PriorityItem:
    symbol: str
    intent: str
    confidence: float


def gross_execution_order(items: list[PriorityItem]) -> list[str]:
    """Ordonne les symboles : réducteurs, puis ouvertures par conviction, puis reste.

    Clé déterministe `(phase, −conviction si ouverture sinon 0, symbole)`.
    """

    def _rank(item: PriorityItem) -> tuple[int, float, str]:
        if item.intent in _REDUCING_INTENTS:
            return (0, 0.0, item.symbol)
        if item.intent in _OPENING_INTENTS:
            # Confiance non finie (NaN/inf) — possible en paper, gate confiance off :
            # la traiter comme la plus basse conviction. Sinon les comparaisons NaN
            # (toujours fausses) rendraient le tri dépendant de l'ordre d'entrée.
            conf = item.confidence if math.isfinite(item.confidence) else float("-inf")
            return (1, -conf, item.symbol)
        return (2, 0.0, item.symbol)

    return [item.symbol for item in sorted(items, key=_rank)]
