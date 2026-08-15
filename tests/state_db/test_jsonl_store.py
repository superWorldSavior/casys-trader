from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.infrastructure.state_db._jsonl_store import (
    JsonlDayLedger,
    calendar_date_from_as_of,
    loose_date_from_as_of,
    read_jsonl_objects,
    read_projection_or_scan,
    required_text,
    safe_filename_component,
    validated_date,
    write_text_atomic,
)


def test_validated_date_rejects_non_calendar_values() -> None:
    assert validated_date("2026-07-10") == "2026-07-10"
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        validated_date("2026-7-10")
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        validated_date("2026-13-01")


def test_as_of_helpers_keep_strict_vs_loose_divergence() -> None:
    assert calendar_date_from_as_of(
        "2026-07-10T08:00:00+00:00",
        empty_error="short",
    ) == "2026-07-10"
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        calendar_date_from_as_of("2026-99-99T08:00:00+00:00", empty_error="short")
    assert loose_date_from_as_of(
        "2026-99-99T08:00:00+00:00",
        empty_error="need dashes",
    ) == "2026-99-99"
    with pytest.raises(ValueError, match="need dashes"):
        loose_date_from_as_of("20260710T08:00:00", empty_error="need dashes")


def test_safe_filename_component_parameterizes_empty_fallback() -> None:
    assert safe_filename_component("EU/US") == "EUUS"
    assert safe_filename_component("@@@", empty="UNKNOWN") == "UNKNOWN"
    with pytest.raises(ValueError, match="safe filename"):
        safe_filename_component("@@@")


def test_read_jsonl_objects_skips_corrupt_and_non_object_lines(tmp_path: Path) -> None:
    path = tmp_path / "2026-07-10.jsonl"
    path.write_text("{bad\n[]\n{\"ok\": 1}\n", encoding="utf-8")
    assert read_jsonl_objects(path) == [{"ok": 1}]


def test_write_text_atomic_replaces_without_leaving_tmp(tmp_path: Path) -> None:
    path = tmp_path / "latest-EU.jsonl"
    write_text_atomic(path, '{"venue":"EU"}\n')
    assert path.read_text(encoding="utf-8") == '{"venue":"EU"}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_ledger_appends_day_file_and_projects_json(tmp_path: Path) -> None:
    ledger = JsonlDayLedger(tmp_path / "runs")
    payload = {"venue": "EU", "status": "success"}
    latest = tmp_path / "runs" / "latest-EU.json"
    with ledger.write_session() as session:
        session.append(payload, date="2026-07-10")
        session.project_json(latest, payload)

    rows = read_jsonl_objects(tmp_path / "runs" / "2026-07-10.jsonl")
    assert rows == [payload]
    assert json.loads(latest.read_text(encoding="utf-8")) == payload
    assert "\n  " in latest.read_text(encoding="utf-8")


def test_ledger_project_jsonl_keeps_single_line(tmp_path: Path) -> None:
    ledger = JsonlDayLedger(tmp_path / "briefs", validate_date=False)
    payload = {"venue": "EU", "point": "rates"}
    latest = tmp_path / "briefs" / "latest-EU.jsonl"
    with ledger.write_session() as session:
        session.append(payload, date="2026-07-09")
        session.project_jsonl(latest, payload)

    text = latest.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.count("\n") == 1
    assert json.loads(text) == payload


def test_append_if_changed_skips_without_creating_day_file(tmp_path: Path) -> None:
    ledger = JsonlDayLedger(tmp_path / "boards", validate_date=False)
    current_path = tmp_path / "boards" / "current.json"
    payload = {"board_id": "b1", "as_of": "2026-07-10T12:00:00+00:00"}

    first, first_changed = ledger.append_if_changed(
        payload,
        date="2026-07-10",
        latest_path=current_path,
        identity_field="board_id",
        read_current=lambda: None,
    )
    same, same_changed = ledger.append_if_changed(
        {**payload, "as_of": "2026-07-10T13:00:00+00:00"},
        date="2026-07-10",
        latest_path=current_path,
        identity_field="board_id",
        read_current=lambda: first,
    )

    assert first_changed is True
    assert same_changed is False
    assert same == first
    day = tmp_path / "boards" / "2026-07-10.jsonl"
    assert [json.loads(line) for line in day.read_text(encoding="utf-8").splitlines()] == [payload]


def test_read_projection_or_scan_falls_back_to_day_jsonl(tmp_path: Path) -> None:
    day = tmp_path / "2026-07-10.jsonl"
    day.write_text(
        json.dumps({"venue": "EU", "id": "1"}) + "\n" + json.dumps({"venue": "US", "id": "2"}) + "\n",
        encoding="utf-8",
    )
    latest = tmp_path / "latest-EU.json"
    latest.write_text("{bad", encoding="utf-8")
    assert read_projection_or_scan(
        latest,
        tmp_path,
        lambda row: row.get("venue") == "EU",
    ) == {"venue": "EU", "id": "1"}


def test_required_text_rejects_blank() -> None:
    assert required_text(" EU ", field="venue") == "EU"
    with pytest.raises(ValueError, match="venue must be a non-empty string"):
        required_text("  ", field="venue")
