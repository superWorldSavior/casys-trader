"""Pure scheduling policy helpers."""

from __future__ import annotations

# Backoff stale - constantes explicites (AX : pas de defaults magiques)
STALE_BACKOFF_BASE_MULTIPLIER: int = 2
STALE_BACKOFF_MAX_MINUTES: float = 120.0
# Streak max persisté : au-delà le wake est déjà cappé (2^2 * 30min = 120min).
# Borne défensive pour éviter OverflowError sur float (2**9999) et garder
# scheduler.json lisible. Valeur 8 = marge confortable au-delà du cap réel.
STALE_BACKOFF_MAX_STREAK: int = 8


def stale_backoff_wake_minutes(streak: int, *, default_wake_minutes: float) -> float:
    """Return the stale-data backoff wake delay, capped defensively."""
    if streak == 0:
        return min(default_wake_minutes, STALE_BACKOFF_MAX_MINUTES)
    if streak >= STALE_BACKOFF_MAX_STREAK:
        return STALE_BACKOFF_MAX_MINUTES
    raw = default_wake_minutes * (STALE_BACKOFF_BASE_MULTIPLIER**streak)
    return min(raw, STALE_BACKOFF_MAX_MINUTES)
