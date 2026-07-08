"""Allocation du portefeuille — calcul PUR (aucune I/O, aucune dépendance infra).

Prend des positions (structurellement `symbol`/`quantity`/`avg_price`) et un
mapping symbole→famille, produit une décomposition région › famille › symbole
dimensionnée par le notional engagé (|qty| × prix d'entrée). Sert aussi bien le
reporting (dashboard d'allocation) que l'allocateur portefeuille dormant (#17) :
la logique « qui pèse combien, où » vit ici, une seule fois, testable.

La région se dérive du préfixe de famille — convention déjà posée dans le
catalog sémantique (`eu_`/`us_`/`tw_`), voir `trader.domain.semantic.catalog`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, runtime_checkable

__all__ = ["PositionLike", "AllocationRow", "region_of_family", "allocation_rows", "notional_by"]

_REGION_BY_PREFIX: dict[str, str] = {"eu": "EU", "us": "US", "tw": "TW"}
_UNKNOWN_FAMILY = "?inconnu?"
_UNKNOWN_REGION = "AUTRE"


@runtime_checkable
class PositionLike(Protocol):
    """Contrat minimal d'une position — satisfait structurellement par l'infra."""

    symbol: str
    quantity: float
    avg_price: float


@dataclass(frozen=True)
class AllocationRow:
    """Une position décomposée pour l'analyse d'allocation."""

    region: str
    family: str
    symbol: str
    side: str  # "long" | "short"
    quantity: float
    notional: float  # |quantity| × avg_price — capital engagé au coût d'entrée


def region_of_family(family: str) -> str:
    """Région d'une famille via son préfixe (`eu_`→EU, `us_`→US, `tw_`→TW)."""
    prefix = family.split("_", 1)[0].lower() if family else ""
    return _REGION_BY_PREFIX.get(prefix, _UNKNOWN_REGION)


def allocation_rows(
    positions: Iterable[PositionLike],
    sym2family: Mapping[str, str],
) -> list[AllocationRow]:
    """Décompose chaque position en `AllocationRow` (région/famille/notional/side).

    Le notional est |quantity| × avg_price : le capital *engagé* au coût d'entrée,
    indépendant du prix courant (donc déterministe, aucun fetch marché). Les
    positions nulles éventuelles sont ignorées.
    """
    rows: list[AllocationRow] = []
    for pos in positions:
        if pos.quantity == 0:
            continue
        family = sym2family.get(pos.symbol, _UNKNOWN_FAMILY)
        rows.append(
            AllocationRow(
                region=region_of_family(family),
                family=family,
                symbol=pos.symbol,
                side="long" if pos.quantity > 0 else "short",
                quantity=pos.quantity,
                notional=abs(pos.quantity) * pos.avg_price,
            )
        )
    return rows


def notional_by(rows: Iterable[AllocationRow], *keys: str) -> dict[tuple[str, ...], float]:
    """Somme le notional groupé par les attributs `keys` (ex: ``"region","family"``).

    Renvoie ``{clé_tuple: notional}``. Primitive d'agrégation pure réutilisable
    (concentration par région, par famille, exposition long/short…).
    """
    out: dict[tuple[str, ...], float] = defaultdict(float)
    for row in rows:
        out[tuple(getattr(row, k) for k in keys)] += row.notional
    return dict(out)
