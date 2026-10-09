"""Shadow JSON atomique — double-write derrière les stores SQLite.

Principe : écriture dans un fichier `.tmp` à nom unique (pid + thread id),
puis `os.replace` (rename atomique POSIX) → la cible ne reçoit jamais un
contenu partiel. Deux writers concurrents depuis des threads différents
utilisent des tmp distincts → pas de collision.

Usage :
    from trader.infrastructure.state_db.shadow import write_json_atomic
    write_json_atomic(state_dir / "broker.json", {"cash": 100_000.0, ...})
"""

import json
import logging
import os
import threading
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


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_atomic(path: Path, data: dict, *, durable: bool = False) -> None:
    """Écrit *data* dans *path* de façon atomique via un fichier temporaire unique.

    Étapes :
    1. S'assurer que le répertoire parent existe.
    2. Sérialiser en JSON (indent=2, ensure_ascii=False — identique à l'existant).
    3. Écrire dans un tmp à nom unique : ``{name}.{pid}.{tid}.tmp``
       (pid = os.getpid(), tid = threading.get_ident()).
    4. ``os.replace(tmp, path)`` (rename atomique POSIX).

    En cas d'exception :
    - loggue ``[state_db] shadow échec <name>: <exc>`` en WARNING.
    - nettoie le tmp (best-effort ``os.unlink``).
    - re-raise → le caller décide.

    Args:
        path: chemin cible (ex. ``state/broker.json``).
        data: dict sérialisable en JSON.
        durable: synchroniser le fichier et ses entrées de répertoire avant
            retour ; réservé aux preuves qui autorisent un apprentissage.
    """
    path = Path(path)
    pid = os.getpid()
    tid = threading.get_ident()
    tmp = path.with_name(f"{path.name}.{pid}.{tid}.tmp")

    missing_parents = []
    if durable:
        parent = path.parent
        while not parent.exists():
            missing_parents.append(parent)
            parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(data, indent=2, ensure_ascii=False)

    try:
        _write_text(str(tmp), payload)
        if durable:
            with tmp.open("rb") as handle:
                os.fsync(handle.fileno())
        os.replace(tmp, path)
        if durable:
            _sync_directory(path.parent)
            for created in missing_parents:
                _sync_directory(created.parent)
    except Exception as exc:
        log.warning("[state_db] shadow échec %s: %s", path.name, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    log.debug("[state_db] shadow écrit %s", path.name)
