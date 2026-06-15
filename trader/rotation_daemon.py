"""Gate de rotation intégrée au daemon : déclenche run_cli aux clôtures de session.

Pas de cron externe : le daemon (`make live`) appelle `maybe_rotate()` à chaque tour de
boucle, juste avant de relire `universe.yaml`. Si une clôture de session de marché est due
depuis la dernière rotation (état persisté `rotation_state.json`), il recalcule le hot-set ;
sinon il ne fait rien. Robuste aux extinctions : au redémarrage, une clôture passée non
couverte déclenche le rattrapage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def maybe_rotate(
    config_dir: str | Path,
    state_dir: str | Path,
    now_iso: str,
    *,
    run_cli: Callable[..., Any],
    log: Any | None = None,
) -> bool:
    """Déclenche une rotation si une clôture de session est due. Fail-safe.

    Retourne True si une rotation a été tentée (succès ou échec loggé), False si aucune
    clôture n'était due depuis ``last_rotation_at``.
    """
    from .rotation_schedule import load_sessions, rotation_due
    from .rotation_state import load_rotation_state

    sessions = load_sessions(config_dir)
    state = load_rotation_state(state_dir)
    if not rotation_due(now_iso, state.get("last_rotation_at", ""), sessions):
        return False
    try:
        run_cli(config_dir, state_dir, as_of=now_iso)
    except Exception:  # noqa: BLE001 — le daemon ne doit pas tomber sur une rotation ratée
        if log is not None:
            log.exception("rotation EOD échouée")
    return True
