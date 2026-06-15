"""Per-venue rotation state helpers."""

from __future__ import annotations


def empty_venue_state() -> dict:
    """Return an empty per-venue rotation state."""
    return {"venues": {}}
