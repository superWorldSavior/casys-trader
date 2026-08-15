"""Gate de rotation D9 legacy — AUCUN appelant de production.

Le daemon ne passe plus par ici : la rotation vivante est `tick_market_rotation`
(D10, `trader/runtime/market_rotation_runtime.py`), déclenchée par venue. Ce module
n'est conservé que pour les shims de compat (`trader/__init__.py`) et ses tests ;
sa suppression est planifiée avec le gommage strangler des shims.
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
    from .schedule import load_sessions, rotation_due
    from .state import load_rotation_state

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
