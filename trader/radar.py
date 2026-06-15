"""Radar Tier-1 : fonctions pures de scoring et de classement."""

from __future__ import annotations


def is_eligible(
    symbol: str,
    *,
    amplitude: float | None,
    hard_exclusions: set[str],
    atr_floor: float,
) -> bool:
    """Retourne si un symbole a assez de data/amplitude et n'est pas exclu."""
    if symbol in hard_exclusions:
        return False
    if amplitude is None:
        return False
    return amplitude >= atr_floor
