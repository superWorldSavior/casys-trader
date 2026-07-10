from __future__ import annotations

import json

from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore


def _scope(
    venue: str,
    scope_id: str,
    *,
    as_of: str = "2026-07-10T05:30:00+00:00",
    label: str = "semi-conducteurs",
) -> dict:
    return {
        "candidate_scope_id": scope_id,
        "venue": venue,
        "as_of": as_of,
        "candidates": [{"symbol": "2330.TW", "label": label}],
    }


def test_append_is_canonical_and_current_projection_tracks_latest_record(tmp_path) -> None:
    store = CandidateScopeStore(tmp_path / "candidate_scopes")
    first = _scope("TW", "scope-tw-1")
    second = _scope("TW", "scope-tw-2", label="électronique avancée")

    ref = store.append(first)
    store.append(second)

    rows = [
        json.loads(line)
        for line in (tmp_path / "candidate_scopes" / "2026-07-10.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    current = json.loads(
        (tmp_path / "candidate_scopes" / "current-TW.json").read_text(encoding="utf-8")
    )

    assert ref == {
        "date": "2026-07-10",
        "venue": "TW",
        "candidate_scope_id": "scope-tw-1",
        "as_of": "2026-07-10T05:30:00+00:00",
    }
    assert [row["candidate_scope_id"] for row in rows] == ["scope-tw-1", "scope-tw-2"]
    assert current == second
    assert store.read_current("TW") == second
    assert "électronique avancée" in (
        tmp_path / "candidate_scopes" / "current-TW.json"
    ).read_text(encoding="utf-8")


def test_current_projections_are_isolated_for_three_venues(tmp_path) -> None:
    store = CandidateScopeStore(tmp_path / "candidate_scopes")
    expected = {
        "TW": _scope("TW", "scope-tw"),
        "EU": _scope("EU", "scope-eu"),
        "US": _scope("US", "scope-us"),
    }

    for record in expected.values():
        store.append(record)

    assert {path.name for path in (tmp_path / "candidate_scopes").glob("current-*.json")} == {
        "current-TW.json",
        "current-EU.json",
        "current-US.json",
    }
    assert {venue: store.read_current(venue) for venue in expected} == expected


def test_read_current_falls_back_to_canonical_when_projection_is_corrupt(tmp_path) -> None:
    store = CandidateScopeStore(tmp_path / "candidate_scopes")
    record = _scope("EU", "scope-eu")
    store.append(record)
    store.current_path_for_venue("EU").write_text("{bad json", encoding="utf-8")

    assert store.read_current("EU") == record


def test_read_current_skips_corrupt_canonical_lines_and_wrong_venue_projection(tmp_path) -> None:
    store = CandidateScopeStore(tmp_path / "candidate_scopes")
    record = _scope("US", "scope-us")
    store.append(record)
    with store.path_for_date("2026-07-10").open("a", encoding="utf-8") as fh:
        fh.write("{bad json\n")
    store.current_path_for_venue("US").write_text(
        json.dumps(_scope("EU", "scope-eu")),
        encoding="utf-8",
    )

    assert store.read_current("US") == record
    assert store.read_current("MISSING") is None


def test_append_does_not_leave_projection_tmp_files(tmp_path) -> None:
    store = CandidateScopeStore(tmp_path / "candidate_scopes")

    store.append(_scope("TW", "scope-tw"))

    assert list((tmp_path / "candidate_scopes").glob("*.tmp")) == []
