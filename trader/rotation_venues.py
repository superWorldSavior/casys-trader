"""Per-venue rotation state helpers."""

from __future__ import annotations

import json
from pathlib import Path


def empty_venue_state() -> dict:
    """Return an empty per-venue rotation state."""
    return {"venues": {}}


def load_venue_state(state_dir) -> dict:
    """Load per-venue state, returning an empty state if unavailable."""
    path = Path(state_dir) / "venue_state.json"
    try:
        with path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        return empty_venue_state()

    if not isinstance(state, dict):
        return empty_venue_state()
    if not isinstance(state.get("venues"), dict):
        return empty_venue_state()
    return state
