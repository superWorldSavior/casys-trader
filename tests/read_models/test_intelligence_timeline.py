from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trader.reporting.read_models.intelligence_timeline import (
    load_company_intelligence,
    load_region_intelligence,
    load_world_timeline,
)


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, *rows: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _posture(
    *,
    posture_id: str,
    as_of: datetime,
    gross_mode: str = "normal",
    net_bias: str = "neutral",
) -> dict:
    return {
        "posture_id": posture_id,
        "as_of": as_of.isoformat(),
        "status": "complete",
        "gross_mode": gross_mode,
        "net_bias": net_bias,
        "venue_posture": {"TW": "selective"},
        "family_priority": {"favored": ["defense"], "deprioritized": []},
        "rationale": f"{gross_mode} posture",
    }


def _company_brief(
    *,
    brief_id: str,
    as_of: datetime,
    thesis_status: str,
) -> dict:
    return {
        "brief_id": brief_id,
        "as_of": as_of.isoformat(),
        "symbol": "ACME",
        "depth": "deep",
        "issuer_identity": {"venue": "US", "issuer_name": "Acme Corp"},
        "company_thesis": {
            "status": thesis_status,
            "summary": f"{thesis_status} thesis",
        },
        "selection_view": {"posture": "watch", "confidence": 0.7},
        "coverage": {"status": "complete"},
        "catalysts": [{"point": "New contract", "source_refs": ["src-1"]}],
        "risks": [{"point": "Execution"}],
        "open_questions": [],
    }


def test_world_window_includes_day_30_and_excludes_day_31(tmp_path: Path) -> None:
    day_30 = NOW - timedelta(days=30)
    day_31 = NOW - timedelta(days=31)
    _write_jsonl(
        tmp_path / "global_universe_postures" / f"{day_30.date().isoformat()}.jsonl",
        _posture(posture_id="included", as_of=day_30),
    )
    _write_jsonl(
        tmp_path / "global_universe_postures" / f"{day_31.date().isoformat()}.jsonl",
        _posture(posture_id="excluded", as_of=day_31),
    )
    _write_jsonl(
        tmp_path / "global_universe_postures" / f"{NOW.date().isoformat()}.jsonl",
        _posture(posture_id="future", as_of=NOW + timedelta(hours=1)),
    )

    payload = load_world_timeline(tmp_path, now=NOW)
    event_ids = {event["event_id"] for event in payload["events"]}

    assert "included" in event_ids
    assert "excluded" not in event_ids
    assert "future" not in event_ids


def test_world_deduplicates_stable_ids_and_describes_posture_change(
    tmp_path: Path,
) -> None:
    previous = _posture(
        posture_id="posture-1",
        as_of=NOW - timedelta(days=1),
        gross_mode="normal",
    )
    current = _posture(
        posture_id="posture-2",
        as_of=NOW,
        gross_mode="cautious",
    )
    _write_jsonl(
        tmp_path / "global_universe_postures" / f"{NOW.date().isoformat()}.jsonl",
        previous,
        current,
        current,
    )

    payload = load_world_timeline(tmp_path, now=NOW)
    posture_events = [
        event for event in payload["events"] if event["kind"] == "global_posture"
    ]
    changed = next(event for event in posture_events if event["event_id"] == "posture-2")

    assert len(posture_events) == 2
    assert {
        "field": "gross_mode",
        "from": "normal",
        "to": "cautious",
    } in changed["changes"]


def test_failure_does_not_replace_latest_success(tmp_path: Path) -> None:
    success = _posture(
        posture_id="success",
        as_of=NOW - timedelta(hours=1),
        gross_mode="cautious",
    )
    failure = {
        "as_of": NOW.isoformat(),
        "error_code": "provider_timeout",
        "error_message": "refresh failed",
    }
    _write_json(tmp_path / "global_universe_postures" / "current.json", success)
    _write_json(
        tmp_path / "global_universe_postures" / "latest_failure.json",
        failure,
    )
    _write_jsonl(
        tmp_path
        / "global_universe_postures"
        / "failures"
        / f"{NOW.date().isoformat()}.jsonl",
        failure,
    )

    payload = load_world_timeline(tmp_path, now=NOW)

    assert payload["current"]["posture"]["posture_id"] == "success"
    assert payload["current"]["latest_failure"]["error_code"] == "provider_timeout"
    assert any(event["status"] == "error" for event in payload["events"])


def test_regional_failure_does_not_replace_latest_success(tmp_path: Path) -> None:
    success = {
        "agent_run_id": "run-success",
        "as_of": (NOW - timedelta(hours=2)).isoformat(),
        "venue": "TW",
        "status": "success",
        "selected_hotlist": ["2330.TW"],
    }
    failure = {
        "agent_run_id": "run-failure",
        "as_of": (NOW - timedelta(hours=1)).isoformat(),
        "venue": "TW",
        "status": "error",
        "error": "provider timeout",
    }
    _write_json(
        tmp_path / "universe_runs" / "latest-TW.json",
        success,
    )
    _write_jsonl(
        tmp_path / "universe_runs" / f"{NOW.date().isoformat()}.jsonl",
        success,
        failure,
    )

    payload = load_region_intelligence(tmp_path, venue="TW", now=NOW)

    assert payload["current"]["TW"]["regional_run"]["agent_run_id"] == "run-success"
    assert payload["current"]["TW"]["latest_failure"]["agent_run_id"] == "run-failure"
    assert any(event["kind"] == "regional_failure" for event in payload["events"])


def test_regional_success_after_failure_clears_latest_failure(tmp_path: Path) -> None:
    failure = {
        "agent_run_id": "run-failure",
        "as_of": (NOW - timedelta(hours=2)).isoformat(),
        "venue": "TW",
        "status": "error",
        "error": "provider timeout",
    }
    recovered = {
        "agent_run_id": "run-recovered",
        "as_of": (NOW - timedelta(hours=1)).isoformat(),
        "venue": "TW",
        "status": "success",
        "selected_hotlist": ["2330.TW"],
    }
    _write_json(tmp_path / "universe_runs" / "latest-TW.json", recovered)
    _write_jsonl(
        tmp_path / "universe_runs" / f"{NOW.date().isoformat()}.jsonl",
        failure,
        recovered,
    )

    payload = load_region_intelligence(tmp_path, venue="TW", now=NOW)

    assert payload["current"]["TW"]["regional_run"]["agent_run_id"] == "run-recovered"
    assert payload["current"]["TW"]["latest_failure"] is None


def test_regional_current_projections_survive_outside_history_window(
    tmp_path: Path,
) -> None:
    old = NOW - timedelta(days=31)
    run = {
        "agent_run_id": "run-current",
        "as_of": old.isoformat(),
        "venue": "TW",
        "status": "success",
        "selected_hotlist": ["2330.TW"],
    }
    brief = {
        "brief_id": "brief-current",
        "as_of": old.isoformat(),
        "venue": "TW",
        "summary": "Canonical macro projection",
    }
    mandate = {
        "mandate_id": "mandate-current",
        "as_of": old.isoformat(),
        "venue": "TW",
        "status": "active",
        "symbols": {"2330.TW": {}},
    }
    board = {
        "board_id": "board-current",
        "as_of": old.isoformat(),
        "venues": {
            "TW": {
                "families": {
                    "tw_osat": {
                        "radar_rank_within_venue": 1,
                        "average_attractiveness": 0.8,
                        "candidate_count": 2,
                    }
                }
            }
        },
    }
    _write_json(tmp_path / "universe_runs" / "latest-TW.json", run)
    _write_jsonl(tmp_path / "news_briefs" / "latest-TW.jsonl", brief)
    _write_json(
        tmp_path / "universe_mandates" / "active" / "venues" / "TW.json",
        mandate,
    )
    _write_json(tmp_path / "global_family_boards" / "current.json", board)

    payload = load_region_intelligence(tmp_path, venue="TW", now=NOW)

    assert payload["events"] == []
    assert payload["current"]["TW"]["regional_run"]["agent_run_id"] == "run-current"
    assert payload["current"]["TW"]["macro_brief"]["brief_id"] == "brief-current"
    assert payload["current"]["TW"]["mandate"]["mandate_id"] == "mandate-current"
    assert payload["families"][0]["rank"] == 1


def test_family_delta_uses_previous_observation_not_previous_global_board(
    tmp_path: Path,
) -> None:
    boards = [
        {
            "board_id": "board-1",
            "as_of": (NOW - timedelta(hours=3)).isoformat(),
            "venues": {
                "TW": {
                    "families": {
                        "tw_osat": {
                            "radar_rank_within_venue": 3,
                            "average_attractiveness": 0.5,
                        }
                    }
                }
            },
        },
        {
            "board_id": "board-2",
            "as_of": (NOW - timedelta(hours=2)).isoformat(),
            "venues": {"EU": {"families": {"defense": {"radar_rank_within_venue": 1}}}},
        },
        {
            "board_id": "board-3",
            "as_of": (NOW - timedelta(hours=1)).isoformat(),
            "venues": {
                "TW": {
                    "families": {
                        "tw_osat": {
                            "radar_rank_within_venue": 2,
                            "average_attractiveness": 0.6,
                        }
                    }
                }
            },
        },
    ]
    _write_jsonl(
        tmp_path / "global_family_boards" / f"{NOW.date().isoformat()}.jsonl",
        *boards,
    )

    payload = load_region_intelligence(tmp_path, venue="TW", now=NOW)
    family = payload["families"][0]

    assert family["rank"] == 2
    assert family["rank_delta"] == 1
    assert round(family["attractiveness_delta"], 3) == 0.1
    assert len(family["history"]) == 3
    assert family["history"][1]["rank"] is None
    technology = next(
        row for row in payload["comparison"] if row["group"] == "technology"
    )
    healthcare = next(
        row for row in payload["comparison"] if row["group"] == "healthcare"
    )
    assert technology["venues"]["TW"]["leader"] == "tw_osat"
    assert healthcare["venues"]["TW"]["status"] == "not_observed"
    assert healthcare["venues"]["TW"]["reason"]


def test_latest_board_omission_clears_old_family_rank(tmp_path: Path) -> None:
    previous = {
        "board_id": "board-previous",
        "as_of": (NOW - timedelta(hours=2)).isoformat(),
        "venues": {
            "TW": {
                "families": {
                    "tw_osat": {
                        "radar_rank_within_venue": 1,
                        "average_attractiveness": 0.8,
                        "candidate_count": 2,
                    }
                }
            }
        },
    }
    current = {
        "board_id": "board-current",
        "as_of": (NOW - timedelta(hours=1)).isoformat(),
        "venues": {"TW": {"families": {}}},
    }
    _write_jsonl(
        tmp_path / "global_family_boards" / f"{NOW.date().isoformat()}.jsonl",
        previous,
        current,
    )
    _write_json(tmp_path / "global_family_boards" / "current.json", current)

    payload = load_region_intelligence(tmp_path, venue="TW", now=NOW)
    family = next(row for row in payload["families"] if row["family"] == "tw_osat")

    assert family["rank"] is None
    assert [point["rank"] for point in family["history"]] == [1, None]


def test_decision_links_mandate_by_candidate_scope(tmp_path: Path) -> None:
    current = _company_brief(
        brief_id="company-1",
        as_of=NOW,
        thesis_status="constructive",
    )
    _write_json(
        tmp_path / "company_intelligence" / "current" / "acme.json",
        {"symbol": "ACME", "briefs": {"deep": current}},
    )
    _write_jsonl(
        tmp_path / "company_intelligence" / "history" / "acme.jsonl",
        current,
    )
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        {
            "decision_id": "decision-linked",
            "cycle_ts": NOW.isoformat(),
            "symbol": "ACME",
            "action": "BUY",
            "executed": True,
            "mandate_ref": {
                "mandate_id": "mandate-1",
                "candidate_scope_id": "scope-1",
                "agent_run_id": "run-1",
            },
        },
        {
            "decision_id": "decision-unlinked",
            "cycle_ts": (NOW - timedelta(hours=1)).isoformat(),
            "symbol": "ACME",
            "action": "HOLD",
            "executed": False,
        },
        {
            "decision_id": "decision-company-linked",
            "cycle_ts": (NOW - timedelta(minutes=30)).isoformat(),
            "symbol": "ACME",
            "action": "HOLD",
            "executed": False,
            "decision": {
                "company_brief_refs": [
                    {"symbol": "ACME", "brief_id": "company-1"}
                ]
            },
        },
    )

    payload = load_company_intelligence(tmp_path, now=NOW)
    linked = next(
        event for event in payload["events"] if event["event_id"] == "decision-linked"
    )
    unlinked = next(
        event for event in payload["events"] if event["event_id"] == "decision-unlinked"
    )
    company_linked = next(
        event
        for event in payload["events"]
        if event["event_id"] == "decision-company-linked"
    )

    assert linked["linkage"] == "linked"
    assert linked["scope_id"] == "scope-1"
    assert linked["mandate_id"] == "mandate-1"
    assert linked["agent_run_id"] == "run-1"
    assert linked["refs"]["agent_run_id"] == "run-1"
    assert unlinked["linkage"] == "unlinked"
    assert company_linked["linkage"] == "linked"
    assert company_linked["refs"]["company_brief_refs"][0]["brief_id"] == "company-1"
    assert payload["counts"]["unlinked_decisions"] == 1


def test_stale_current_company_is_retained_but_not_added_to_timeline(
    tmp_path: Path,
) -> None:
    stale = _company_brief(
        brief_id="company-stale",
        as_of=NOW - timedelta(days=31),
        thesis_status="watch",
    )
    _write_json(
        tmp_path / "company_intelligence" / "current" / "acme.json",
        {"symbol": "ACME", "briefs": {"deep": stale}},
    )
    _write_jsonl(
        tmp_path / "company_intelligence" / "history" / "acme.jsonl",
        stale,
    )

    payload = load_company_intelligence(tmp_path, now=NOW)

    assert [company["symbol"] for company in payload["companies"]] == ["ACME"]
    assert not any(event["kind"] == "company_brief" for event in payload["events"])


def test_company_history_metrics_are_bounded_to_window(tmp_path: Path) -> None:
    current = _company_brief(
        brief_id="company-current",
        as_of=NOW,
        thesis_status="constructive",
    )
    old = _company_brief(
        brief_id="company-old",
        as_of=NOW - timedelta(days=31),
        thesis_status="watch",
    )
    _write_json(
        tmp_path / "company_intelligence" / "current" / "acme.json",
        {"symbol": "ACME", "briefs": {"deep": current}},
    )
    _write_jsonl(
        tmp_path / "company_intelligence" / "history" / "acme.jsonl",
        old,
        current,
    )

    company = load_company_intelligence(tmp_path, now=NOW)["companies"][0]

    assert company["history_count"] == 1
    assert company["previous_thesis_status"] is None
    assert company["thesis_changed"] is False


def test_empty_and_corrupt_state_is_tolerated(tmp_path: Path) -> None:
    corrupt = tmp_path / "global_family_boards" / f"{NOW.date().isoformat()}.jsonl"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{broken\n", encoding="utf-8")
    company = tmp_path / "company_intelligence" / "current" / "broken.json"
    company.parent.mkdir(parents=True)
    company.write_text("{broken", encoding="utf-8")

    assert load_world_timeline(tmp_path, now=NOW)["events"] == []
    assert load_region_intelligence(tmp_path, now=NOW)["families"] == []
    assert load_company_intelligence(tmp_path, now=NOW)["companies"] == []
