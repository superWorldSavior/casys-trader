"""Exposition du portefeuille dans le TEMPS — calcul PUR (aucune I/O).

Reconstruit, à partir de l'historique des exécutions (`fills`), l'état des
positions après chaque trade, puis émet le notional USD agrégé par région/famille à
cet instant. C'est la base de la vue « allocation qui monte et descend » (stacked
area) et, plus largement, de toute analyse d'exposition rétrospective.

Le prix de valorisation d'un symbole entre deux de ses fills est son dernier prix
de fill connu (seul prix réel disponible sans fetch marché), converti par le
dernier `fx_rate` du fill quand disponible — donc déterministe.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, runtime_checkable

from trader.domain.portfolio.allocation import region_of_family

__all__ = ["FillLike", "ExposurePoint", "exposure_timeline"]

_POSITION_EPSILON = 1e-9


@runtime_checkable
class FillLike(Protocol):
    """Contrat minimal d'une exécution — satisfait structurellement par l'infra."""

    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: float
    price: float
    ts: str  # ISO 8601, triable


@dataclass(frozen=True)
class ExposurePoint:
    """Notional d'une famille à un instant (après un fill)."""

    ts: str
    region: str
    family: str
    notional: float  # USD approx. si le fill porte fx_rate, sinon devise native


def exposure_timeline(
    fills: Iterable[FillLike],
    sym2family: Mapping[str, str],
) -> list[ExposurePoint]:
    """Série temporelle du notional par famille, reconstruite depuis les fills.

    Rejoue les fills dans l'ordre chronologique (`ts`), maintient la quantité
    cumulée et le dernier prix par symbole, et émet — après chaque fill — le
    notional (|qty cumulée| × dernier prix × dernier fx_rate) de **chaque famille
    active** à cet instant. Les positions soldées (qty=0) sortent de l'agrégat.
    """
    state: dict[str, list[float]] = {}  # symbol -> [cum_qty, last_price, last_fx_rate]
    points: list[ExposurePoint] = []

    for fill in sorted(fills, key=lambda f: f.ts):
        signed = fill.quantity if str(fill.side).upper() == "BUY" else -fill.quantity
        cur = state.setdefault(fill.symbol, [0.0, 0.0, 1.0])
        cur[0] += signed
        cur[1] = fill.price
        cur[2] = float(getattr(fill, "fx_rate", 1.0) or 1.0)

        by_family: dict[str, float] = defaultdict(float)
        for symbol, (qty, price, fx_rate) in state.items():
            if abs(qty) <= _POSITION_EPSILON:
                continue
            by_family[sym2family.get(symbol, "?inconnu?")] += abs(qty) * price * fx_rate

        for family, notional in by_family.items():
            points.append(ExposurePoint(fill.ts, region_of_family(family), family, notional))

    return points
