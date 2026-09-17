from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.application.world_model.about_writer import select_live_notes

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _note(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {"note_key": "n1"}
    values.update(overrides)
    return values


def test_select_live_notes_drops_only_strictly_expired() -> None:
    notes = [
        _note(note_key="live", valid_until="2026-09-11T00:00:00+00:00"),
        _note(note_key="edge", valid_until="2026-09-10T12:00:00+00:00"),
        _note(note_key="expired", valid_until="2026-09-09T00:00:00+00:00"),
        _note(note_key="missing"),
        _note(note_key="unparseable", valid_until="not-a-clock"),
    ]
    selected = select_live_notes(notes, now=NOW)
    assert [note["note_key"] for note in selected] == ["live", "edge", "missing", "unparseable"]


def test_select_live_notes_rejects_non_mapping_inputs() -> None:
    with pytest.raises(TypeError, match="sequence of mappings"):
        select_live_notes("nope")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="sequence of mappings"):
        select_live_notes([_note(), "nope"])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="now must be a datetime"):
        select_live_notes([_note()], now="now")  # type: ignore[arg-type]
