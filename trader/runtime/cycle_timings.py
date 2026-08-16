"""Horloge défensive des étages de ``run_cycle``.

Les marques sont additives : un échec de chronométrage n'altère jamais la
décision ni le flux du cycle. Les clés émises dans ``cycle_completed`` sont
un dict plat d'entiers (millisecondes).
"""

from __future__ import annotations

import time
from typing import Mapping

STAGE_KEYS: tuple[str, ...] = (
    "snapshot_ms",
    "gate_scope_ms",
    "decide_ms",
    "risk_execute_ms",
    "record_ms",
)


class StageClock:
    """Chronomètre les étages successifs d'un cycle.

    ``mark_start`` / ``mark_end`` n'élèvent jamais. ``as_ms`` renvoie uniquement
    des ``int`` JSON-safe, plus ``total_ms`` depuis la construction.
    """

    def __init__(self, *, monotonic=time.perf_counter) -> None:
        self._monotonic = monotonic
        self._t0 = monotonic()
        self._starts: dict[str, float] = {}
        self._ms: dict[str, int] = {}

    def mark_start(self, name: str) -> None:
        try:
            self._starts[str(name)] = self._monotonic()
        except Exception:
            return

    def mark_end(self, name: str) -> None:
        key = str(name)
        try:
            started = self._starts.pop(key, None)
            if started is None:
                return
            elapsed_ms = (self._monotonic() - started) * 1000.0
            self._ms[key] = int(round(elapsed_ms))
        except Exception:
            return

    def as_ms(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, value in self._ms.items():
            try:
                out[str(key)] = int(value)
            except Exception:
                continue
        try:
            out["total_ms"] = int(round((self._monotonic() - self._t0) * 1000.0))
        except Exception:
            pass
        return out


def try_stage_clock() -> StageClock | None:
    """Construit une horloge ; ``None`` si la construction elle-même échoue."""

    try:
        return StageClock()
    except Exception:
        return None


def mark_start(clock: StageClock | None, name: str) -> None:
    if clock is None:
        return
    try:
        clock.mark_start(name)
    except Exception:
        return


def mark_end(clock: StageClock | None, name: str) -> None:
    if clock is None:
        return
    try:
        clock.mark_end(name)
    except Exception:
        return


def attach_stage_timings(
    payload: Mapping[str, object],
    clock: StageClock | None,
) -> dict[str, object]:
    """Copie ``payload`` et y ajoute ``stage_timings_ms`` si disponible."""

    out = dict(payload)
    if clock is None:
        return out
    try:
        timings = clock.as_ms()
    except Exception:
        return out
    if timings:
        out["stage_timings_ms"] = timings
    return out
