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


def test_waiting_brief_keeps_last_success_projection_and_exposes_latest_waiting(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    success = _run("US", "scope-yesterday", "success", agent_run_id="run-success")
    waiting = {
        **_run(
            "US",
            "scope-today",
            "waiting_brief",
            as_of="2026-07-10T12:07:00+00:00",
        ),
        "error_code": "brief_scope_mismatch",
    }

    store.append(success)
    store.append(waiting)

    assert store.read_latest("US") == {**success, "latest_waiting": waiting}
    rows = [
        json.loads(line)
        for line in (tmp_path / "universe_runs" / "2026-07-10.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows == [success, waiting]

    recovered = {**success, "as_of": "2026-07-10T12:20:00+00:00", "agent_run_id": "run-recovered"}
    store.append(recovered)
    latest = store.read_latest("US")
    assert latest == recovered
    assert "latest_waiting" not in latest
    assert "latest_failure" not in latest


def test_waiting_brief_without_prior_success_remains_the_projection(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    waiting = {
        **_run("US", "scope-today", "waiting_brief"),
        "error_code": "brief_missing",
    }

    store.append(waiting)

    assert store.read_latest("US") == waiting


def test_error_keeps_last_success_projection_and_exposes_latest_failure(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    success = _run("EU", "scope-eu", "success", agent_run_id="run-success")
    failure = {
        **_run(
            "EU",
            "scope-eu",
            "error",
            as_of="2026-07-10T06:30:00+00:00",
            agent_run_id="run-error",
        ),
        "error_code": "TimeoutError",
        "retry_attempt": 1,
        "next_retry_at": "2026-07-10T07:00:00+00:00",
    }

    store.append(success)
    store.append(failure)

    assert store.read_latest("EU") == {**success, "latest_failure": failure}
    rows = [
        json.loads(line)
        for line in (tmp_path / "universe_runs" / "2026-07-10.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows == [success, failure]

    store.append({**success, "as_of": "2026-07-10T07:01:00+00:00", "agent_run_id": "run-recovered"})
    assert "latest_failure" not in store.read_latest("EU")


def test_waiting_overlay_replaces_prior_failure_marker(tmp_path) -> None:
    store = UniverseRunStore(tmp_path / "universe_runs")
    success = _run("US", "scope-yesterday", "success")
    failure = {
        **_run("US", "scope-yesterday", "error", as_of="2026-07-10T06:30:00+00:00"),
        "error_code": "TimeoutError",
    }
    waiting = {
        **_run("US", "scope-today", "waiting_brief", as_of="2026-07-10T12:07:00+00:00"),
        "error_code": "brief_scope_mismatch",
    }

    store.append(success)
    store.append(failure)
    store.append(waiting)

    latest = store.read_latest("US")
    assert latest["status"] == "success"
    assert latest["latest_waiting"] == waiting
    assert "latest_failure" not in latest


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
