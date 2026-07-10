from __future__ import annotations

import hashlib
import json

import pytest

from trader.infrastructure.state_db.universe_run_store import UniverseRunStore


def _run(
    venue: str,
    scope_id: str,
    status: str,
    *,
    as_of: str = "2026-07-10T06:00:00+00:00",
    agent_run_id: str | None = None,
) -> dict:
    record = {
        "candidate_scope_id": scope_id,
        "venue": venue,
        "as_of": as_of,
        "status": status,
        "message": "sélection préparée",
    }
    if agent_run_id is not None:
        record["agent_run_id"] = agent_run_id
    return record


def test_append_preserves_every_status_and_latest_projection(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    records = [
        _run("TW", "scope-tw", "error", agent_run_id="run-1"),
        _run("TW", "scope-tw", "invalid", agent_run_id="run-2"),
        _run("TW", "scope-tw", "success", agent_run_id="run-3"),
    ]

    refs = [store.append(record) for record in records]

    rows = [
        json.loads(line)
        for line in (tmp_path / "universe_runs" / "2026-07-10.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["status"] for row in rows] == ["error", "invalid", "success"]
    assert store.read_latest("TW") == records[-1]
    assert refs[-1] == {
        "date": "2026-07-10",
        "venue": "TW",
        "candidate_scope_id": "scope-tw",
        "as_of": "2026-07-10T06:00:00+00:00",
        "status": "success",
        "agent_run_id": "run-3",
    }
    assert "sélection préparée" in (
        tmp_path / "universe_runs" / "latest-TW.json"
    ).read_text(encoding="utf-8")


def test_latest_projections_are_isolated_for_three_venues(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    expected = {
        venue: _run(venue, f"scope-{venue.lower()}", "success")
        for venue in ("TW", "EU", "US")
    }

    for record in expected.values():
        store.append(record)

    assert {path.name for path in (tmp_path / "universe_runs").glob("latest-*.json")} == {
        "latest-TW.json",
        "latest-EU.json",
        "latest-US.json",
    }
    assert {venue: store.read_latest(venue) for venue in expected} == expected


def test_read_latest_falls_back_to_canonical_when_projection_is_corrupt(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    record = _run("EU", "scope-eu", "error")
    store.append(record)
    store.latest_path_for_venue("EU").write_text("{bad json", encoding="utf-8")

    assert store.read_latest("EU") == record


def test_prepared_projection_uses_safe_hash_and_reads_exact_scope(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    scope_id = "TW:2026-07-10T05:30:00+00:00/unsafe"
    record = {
        **_run("TW", scope_id, "success"),
        "selected_hotlist": ["2330.TW", "2454.TW"],
    }

    written = store.write_prepared(scope_id, record)
    expected_name = hashlib.sha256(scope_id.encode("utf-8")).hexdigest() + ".json"

    assert store.prepared_path_for_scope(scope_id).name == expected_name
    assert "/" not in expected_name
    assert store.read_prepared(scope_id) == record
    assert written == record
    assert store.read_prepared("stale-scope") is None


def test_read_prepared_rejects_stale_embedded_scope_and_corruption(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    scope_id = "scope-current"
    path = store.prepared_path_for_scope(scope_id)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_run("US", "scope-stale", "success")),
        encoding="utf-8",
    )

    assert store.read_prepared(scope_id) is None

    path.write_text("{bad json", encoding="utf-8")
    assert store.read_prepared(scope_id) is None


def test_write_prepared_rejects_mismatched_scope_without_writing(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")

    with pytest.raises(ValueError, match="does not match"):
        store.write_prepared("scope-current", _run("TW", "scope-stale", "success"))

    assert store.read_prepared("scope-current") is None


def test_prepared_write_is_atomic_and_does_not_touch_canonical_runs(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    record = _run("TW", "scope-tw", "success")

    store.write_prepared("scope-tw", record)

    assert store.read_prepared("scope-tw") == record
    assert list((tmp_path / "universe_prepared").glob("*.tmp")) == []
    assert not (tmp_path / "universe_runs").exists()
