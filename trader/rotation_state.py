"""État persistant de la rotation — mémoire de l'hystérésis.

Structure JSON : {"current_hot_set": [...], "dwell_days_by_symbol": {sym: int},
                  "last_valid_universe": [...]}
"""
from __future__ import annotations

import json
import os
import tempfile

_FILENAME = "rotation_state.json"

_DEFAULT: dict = {
    "current_hot_set": [],
    "dwell_days_by_symbol": {},
    "last_valid_universe": [],
}


def _default_state() -> dict:
    return {
        "current_hot_set": [],
        "dwell_days_by_symbol": {},
        "last_valid_universe": [],
    }


def load_rotation_state(state_dir: str) -> dict:
    """Lit state_dir/rotation_state.json.

    Retourne le défaut si le fichier est absent ou illisible.
    """
    path = os.path.join(state_dir, _FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default_state()


def seed_state(universe_symbols: list[str]) -> dict:
    """Bootstrap (tout premier run) — chaque symbole démarre à dwell=1."""
    syms = list(universe_symbols)
    return {
        "current_hot_set": syms,
        "dwell_days_by_symbol": {s: 1 for s in syms},
        "last_valid_universe": syms,
    }


def advance_state(state: dict, new_hot_set: list[str]) -> dict:
    """Transition déterministe vers new_hot_set.

    - Resté chaud → dwell += 1
    - Entrant      → dwell = 1
    - Sortant      → retiré de dwell_days_by_symbol
    - Si new_hot_set vide → last_valid_universe conservé ; sinon mis à jour.
    """
    prev_set = set(state["current_hot_set"])
    prev_dwell: dict[str, int] = dict(state["dwell_days_by_symbol"])

    new_dwell: dict[str, int] = {}
    for sym in new_hot_set:
        if sym in prev_set:
            new_dwell[sym] = prev_dwell.get(sym, 0) + 1
        else:
            new_dwell[sym] = 1

    if new_hot_set:
        last_valid = list(new_hot_set)
    else:
        last_valid = list(state.get("last_valid_universe", []))

    return {
        "current_hot_set": list(new_hot_set),
        "dwell_days_by_symbol": new_dwell,
        "last_valid_universe": last_valid,
    }


def save_rotation_state(state_dir: str, state: dict) -> None:
    """Écrit state_dir/rotation_state.json (JSON indenté, écriture atomique).

    Crée state_dir s'il n'existe pas.
    """
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, _FILENAME)
    payload = json.dumps(state, indent=2, ensure_ascii=False)

    # Écriture atomique via fichier temporaire + os.replace
    fd, tmp_path = tempfile.mkstemp(dir=state_dir, prefix=".tmp_rotation_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        # Nettoyage du tmp en cas d'erreur
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
