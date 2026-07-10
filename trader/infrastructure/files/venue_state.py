"""Atomic JSON filesystem adapter for per-venue rotation state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def empty_venue_state() -> dict[str, dict[str, Any]]:
    """Return the valid empty projection."""

    return {"venues": {}}


def load_venue_state(state_dir: str | Path) -> dict[str, Any]:
    """Load ``venue_state.json`` or return an empty valid projection."""

    path = Path(state_dir) / "venue_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - legacy state reads are deliberately fail-safe
        return empty_venue_state()
    if not isinstance(state, dict) or not isinstance(state.get("venues"), dict):
        return empty_venue_state()
    return state


def save_venue_state(state_dir: str | Path, state: dict[str, Any]) -> None:
    """Persist ``venue_state.json`` atomically with stable formatting."""

    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "venue_state.json"
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
            json.dump(state, file_handle, indent=2, sort_keys=True)
            file_handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
