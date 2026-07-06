"""user_overrides — pin/ban utilisateur sur la sélection d'univers.

Le cockpit (page Universe) écrit un bloc ``overrides:`` dans
``config/universe.yaml`` ; la rotation l'applique à chaque composition de
l'univers actif. Deux verbes, sans chevauchement :

- ``pin``  : toujours dans l'univers écrit, quel que soit le score radar.
- ``ban``  : jamais sélectionné — mais n'éjecte JAMAIS un symbole sticky
  (position ouverte / plan armé) : la position reste gérée, le symbole
  quitte seulement la sélection future.

Format cible :

.. code-block:: yaml

    symbols:
    - 1326.TW
    overrides:
      pin:
      - PANW
      ban:
      - 3443.TW

Écriture atomique (tempfile + os.replace, même répertoire) ; lecture
fail-safe (fichier absent/corrompu → overrides vides, jamais d'exception).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@contextlib.contextmanager
def universe_write_lock(universe_path: str | Path):
    """Verrou inter-process sur universe.yaml (flock sur <path>.lock).

    Sérialise les read-modify-write concurrents cockpit (save_user_overrides)
    et rotation (write_universe_atomic) : sans lui, le dernier os.replace
    gagnerait et perdrait silencieusement l'écriture de l'autre.
    """
    lock_path = Path(str(universe_path) + ".lock")
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class UserOverrides:
    """Pin/ban utilisateur. Invariant : un symbole ne peut pas être les deux."""

    pin: tuple[str, ...] = field(default=())
    ban: tuple[str, ...] = field(default=())

    def status_of(self, symbol: str) -> str | None:
        """"pinned" | "banned" | None."""
        if symbol in self.pin:
            return "pinned"
        if symbol in self.ban:
            return "banned"
        return None


def _clean_symbols(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def load_user_overrides(universe_path: str | Path) -> UserOverrides:
    """Lit le bloc ``overrides:`` de universe.yaml. Jamais d'exception."""
    try:
        data = yaml.safe_load(Path(universe_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return UserOverrides()
    raw = data.get("overrides") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return UserOverrides()
    pin = _clean_symbols(raw.get("pin"))
    ban = tuple(s for s in _clean_symbols(raw.get("ban")) if s not in pin)
    return UserOverrides(pin=pin, ban=ban)


def overrides_block(overrides: UserOverrides) -> dict | None:
    """Représentation YAML du bloc, ou None si vide (bloc omis du fichier)."""
    block: dict = {}
    if overrides.pin:
        block["pin"] = list(overrides.pin)
    if overrides.ban:
        block["ban"] = list(overrides.ban)
    return block or None


def save_user_overrides(universe_path: str | Path, overrides: UserOverrides) -> None:
    """Écrit le bloc ``overrides:`` en préservant TOUTES les autres clés. Atomique.

    Sérialisé avec la rotation via ``universe_write_lock`` : le cockpit ne
    possède que la clé ``overrides``, tout le reste est réécrit tel quel.

    Raises:
        OSError: si l'écriture atomique échoue (répertoire absent, droits).
    """
    path = Path(universe_path)
    with universe_write_lock(path):
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}

        data: dict = {"symbols": existing.get("symbols") or []}
        for key, value in existing.items():
            if key not in ("symbols", "overrides"):
                data[key] = value
        block = overrides_block(overrides)
        if block is not None:
            data["overrides"] = block

        fd, tmp_path = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def pin_symbol(universe_path: str | Path, symbol: str) -> UserOverrides:
    """Pin ``symbol`` (le retire du ban s'il y était). Retourne l'état final."""
    current = load_user_overrides(universe_path)
    symbol = symbol.strip()
    updated = UserOverrides(
        pin=_clean_symbols([*current.pin, symbol]),
        ban=tuple(s for s in current.ban if s != symbol),
    )
    save_user_overrides(universe_path, updated)
    return updated


def ban_symbol(universe_path: str | Path, symbol: str) -> UserOverrides:
    """Ban ``symbol`` (le retire du pin s'il y était). Retourne l'état final."""
    current = load_user_overrides(universe_path)
    symbol = symbol.strip()
    updated = UserOverrides(
        pin=tuple(s for s in current.pin if s != symbol),
        ban=_clean_symbols([*current.ban, symbol]),
    )
    save_user_overrides(universe_path, updated)
    return updated


def clear_override(universe_path: str | Path, symbol: str) -> UserOverrides:
    """Retire ``symbol`` du pin ET du ban (undo). Retourne l'état final."""
    current = load_user_overrides(universe_path)
    symbol = symbol.strip()
    updated = UserOverrides(
        pin=tuple(s for s in current.pin if s != symbol),
        ban=tuple(s for s in current.ban if s != symbol),
    )
    save_user_overrides(universe_path, updated)
    return updated


def apply_user_overrides(
    symbols: list[str],
    *,
    pin: tuple[str, ...] | list[str] = (),
    ban: tuple[str, ...] | list[str] = (),
    sticky: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Applique pin/ban à un univers composé. PUR.

    - ban retire de la sélection, SAUF les sticky (position ouverte / plan
      armé) : une position bannie reste gérée jusqu'à sa clôture.
    - pin ajoute en fin de liste s'il manque (hors quota, comme les sticky).
    """
    banned = set(ban) - set(sticky)
    kept = [s for s in symbols if s not in banned]
    for symbol in pin:
        if symbol not in kept:
            kept.append(symbol)
    return kept


def effective_universe_symbols(
    universe_path: str | Path,
    *,
    positions: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """``symbols:`` de universe.yaml avec pin/ban appliqués À LA LECTURE.

    Filet côté daemon : le ban prend effet au cycle suivant même si la
    rotation n'a pas encore réécrit le fichier (et même si l'univers écrit
    ne peut pas être vidé). ``positions`` = symboles avec position ouverte,
    jamais éjectés par un ban. Jamais d'exception.
    """
    try:
        data = yaml.safe_load(Path(universe_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    symbols = [str(s) for s in (data.get("symbols") or []) if s] if isinstance(data, dict) else []
    overrides = load_user_overrides(universe_path)
    return apply_user_overrides(
        symbols, pin=overrides.pin, ban=overrides.ban, sticky=positions
    )
