"""Biais de régime cross-asset par famille thématique (décision D2 du registre).

95 % des moves ratés arrivent en cluster cross-asset (≥2 symboles du même
univers, 84 % cohérents directionnellement) : des régimes risk-on/off lisibles
sur l'univers entier, invisibles en lecture mono-symbole. Ce module calcule la
synthèse directionnelle par famille (`semantic.catalog.FAMILIES`) à partir des
momentums passés ; le code calcule le fait, l'agent juge (code over
instructions). Fonction pure : pas d'I/O, pas d'horloge.
"""

from __future__ import annotations

from .semantic.catalog import FAMILIES

__all__ = ["compute_family_bias", "families_for_universe", "momentum_from_bars"]


def momentum_from_bars(bars: list[object], *, lookback_bars: int = 3) -> float | None:
    """Momentum % signé : close de la dernière barre vs `lookback_bars` en arrière.

    L'appelant choisit l'horizon des barres : sur des barres daily,
    lookback_bars=3 ≈ 3 séances. Retourne None si pas assez de barres.
    """
    if len(bars) < lookback_bars + 1:
        return None
    last = float(bars[-1].close)
    ref = float(bars[-1 - lookback_bars].close)
    if ref == 0:
        return None
    return round((last - ref) / ref * 100.0, 4)


def families_for_universe(symbols: list[str]) -> dict[str, list[str]]:
    """Restreint `catalog.FAMILIES` aux symboles actifs ; garde les familles ≥ 2 membres."""
    active = set(symbols)
    out: dict[str, list[str]] = {}
    for family, members in FAMILIES.items():
        present = [s for s in members if s in active]
        if len(present) >= 2:
            out[family] = present
    return out


def compute_family_bias(
    momentum_by_symbol: dict[str, float | None],
    families: dict[str, list[str]],
    *,
    min_symbols: int = 2,
) -> dict[str, dict]:
    """Synthèse directionnelle par famille depuis des momentums (% signés).

    Un symbole compte `up` si momentum > 0, `down` si < 0 ; nul ou absent =
    ignoré. Une famille n'est retournée que si ≥ `min_symbols` symboles ont un
    momentum signé (seuil cluster D2). Retourne par famille :
    ``{"dir": "up"|"down", "frac": part du sens majoritaire, "up", "down", "n"}``.
    """
    out: dict[str, dict] = {}
    for family, symbols in families.items():
        up = down = 0
        for symbol in symbols:
            momentum = momentum_by_symbol.get(symbol)
            if momentum is None or momentum == 0:
                continue
            if momentum > 0:
                up += 1
            else:
                down += 1
        n = up + down
        if n < min_symbols:
            continue
        majority = max(up, down)
        out[family] = {
            "dir": "up" if up >= down else "down",
            "frac": round(majority / n, 2),
            "up": up,
            "down": down,
            "n": n,
        }
    return out
