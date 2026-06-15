"""Tests for per-venue rotation state helpers."""

from __future__ import annotations

from trader.rotation_venues import empty_venue_state


def test_empty_venue_state_returns_empty_venues():
    assert empty_venue_state() == {"venues": {}}
