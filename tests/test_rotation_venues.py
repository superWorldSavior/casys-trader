"""Tests for per-venue rotation state helpers."""

from __future__ import annotations

from trader.rotation_venues import empty_venue_state, load_venue_state


def test_empty_venue_state_returns_empty_venues():
    assert empty_venue_state() == {"venues": {}}


def test_load_venue_state_missing_file_returns_empty(tmp_path):
    assert load_venue_state(tmp_path) == {"venues": {}}


def test_load_venue_state_ill_readable_file_returns_empty(tmp_path):
    (tmp_path / "venue_state.json").write_text("{ not json", encoding="utf-8")

    assert load_venue_state(tmp_path) == {"venues": {}}
