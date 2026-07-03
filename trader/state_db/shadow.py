"""Shadow JSON atomique — double-write derrière les stores SQLite.

Principe : écriture dans un fichier `.tmp`, puis `os.replace` (rename atomique
POSIX) → la cible ne reçoit jamais un contenu partiel.

Usage :
    from trader.state_db.shadow import write_json_atomic
    write_json_atomic(state_dir / "broker.json", {"cash": 100_000.0, ...})
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Point d'injection pour les tests (monkeypatch de _write_text)
# ---------------------------------------------------------------------------


def _write_text(path: str, data: str, encoding: str = "utf-8") -> None:
    """Wrapper isolable autour de Path.write_text pour les tests."""
    Path(path).write_text(data, encoding=encoding)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def write_json_atomic(path: Path, data: dict) -> None:
    """Écrit *data* dans *path* de façon atomique via un fichier temporaire.

    Étapes :
    1. S'assurer que le répertoire parent existe.
    2. Sérialiser en JSON (indent=2, ensure_ascii=False — identique à l'existant).
    3. Écrire dans ``path.with_suffix(path.suffix + ".tmp")``.
    4. ``os.replace(tmp, path)`` (rename atomique POSIX).

    En cas d'exception à l'écriture du tmp :
    - loggue ``[state_db] shadow échec <name>: <exc>`` en WARNING.
    - re-raise → le caller décide.

    Args:
        path: chemin cible (ex. ``state/broker.json``).
        data: dict sérialisable en JSON.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")

    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(data, indent=2, ensure_ascii=False)

    try:
        _write_text(str(tmp), payload)
    except Exception as exc:
        log.warning("[state_db] shadow échec %s: %s", path.name, exc)
        raise

    os.replace(tmp, path)
    log.debug("[state_db] shadow écrit %s", path.name)
