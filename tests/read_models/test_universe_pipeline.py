from __future__ import annotations

import json
from pathlib import Path

from trader.reporting.read_models.runtime_state import (
    _load_universe_pipeline_safe,
    load_runtime_state,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _activation(
    venue: str,
    *,
    as_of: str,
    agent_run_id: str,
    hotlist: list[str],
    fallback_used: bool = False,
    fallback_reason: str | None = None,
) -> dict:
    return {
        "as_of": as_of,
        "final_hot_set": hotlist,
        "overrides": {
            "venue": venue,
            "candidate_scope_id": f"candidate_scope:v1:{venue}:{venue.lower()}-scope",
            "agent_run_id": agent_run_id,
            "brief_ref": {"venue": venue, "brief_id": f"brief-{venue}"},
            "selected_hotlist": hotlist,
            "selected_challengers": hotlist[-1:],
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        },
    }


def test_pipeline_uses_latest_activation_independently_for_each_venue(tmp_path: Path) -> None:
    rows = [
        _activation(
            "TW",
            as_of="2026-07-10T01:00:00+00:00",
            agent_run_id="universe:TW:old-run",
            hotlist=["OLD.TW"],
        ),
        _activation(
            "EU",
            as_of="2026-07-10T02:00:00+00:00",
            agent_run_id="universe:EU:eu-run",
            hotlist=["ASML.AS"],
        ),
        _activation(
            "TW",
            as_of="2026-07-10T03:00:00+00:00",
            agent_run_id="universe:TW:new-run",
            hotlist=["2330.TW", "2454.TW"],
        ),
    ]
    (tmp_path / "rotation_ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n{broken\n",
        encoding="utf-8",
    )

    pipeline = _load_universe_pipeline_safe(tmp_path, {})

    assert pipeline["TW"]["activation"]["agent_run_id"] == "universe:TW:new-run"
    assert pipeline["TW"]["activation"]["hotlist_count"] == 2
    assert pipeline["EU"]["activation"]["agent_run_id"] == "universe:EU:eu-run"
    assert pipeline["US"]["activation"]["status"] == "pending"


def test_pipeline_keeps_agent_success_separate_from_activation_fallback(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "universe_runs" / "latest-US.json",
        {
            "venue": "US",
            "as_of": "2026-07-10T03:00:00+00:00",
            "candidate_scope_id": "candidate_scope:v1:US:scope-id",
            "agent_run_id": "universe:US:successful-agent-run",
            "status": "success",
            "selected_hotlist": ["AAPL", "MSFT"],
            "selected_challengers": ["MSFT"],
            "coverage": {"status": "partial", "global_headlines": "present"},
        },
    )
    row = _activation(
        "US",
        as_of="2026-07-10T03:30:00+00:00",
        agent_run_id="universe:US:successful-agent-run",
        hotlist=["AAPL"],
        fallback_used=True,
        fallback_reason="prepared_scope_mismatch",
    )
    (tmp_path / "rotation_ledger.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    pipeline = _load_universe_pipeline_safe(tmp_path, {})["US"]

    assert pipeline["agent"]["status"] == "success"
    assert pipeline["agent"]["hotlist_count"] == 2
    assert pipeline["agent"]["challenger_count"] == 1
    assert pipeline["activation"]["status"] == "fallback"
    assert pipeline["activation"]["fallback_used"] is True
    assert pipeline["activation"]["fallback_reason"] == "prepared_scope_mismatch"
    assert pipeline["activation"]["hotlist_count"] == 1


def test_pipeline_summarizes_scope_scout_brief_counts_and_short_ids(tmp_path: Path) -> None:
    scope_id = "candidate_scope:v1:EU:1234567890abcdef1234567890abcdef"
    _write_json(
        tmp_path / "candidate_scopes" / "current-EU.json",
        {
            "venue": "EU",
            "candidate_scope_id": scope_id,
            "as_of": "2026-07-10T02:00:00+00:00",
            "candidate_run_ids": ["news:EU:1234567890abcdef"],
            "candidates": [
                {"symbol": "ASML.AS", "candidate_source": "radar"},
                {"symbol": "SAP.DE", "candidate_source": "fresh_news", "fresh_news": {}},
            ],
            "default_hotlist": ["ASML.AS", "SAP.DE"],
        },
    )
    _write_json(
        tmp_path / "news_challenger_runs" / "latest-EU.json",
        {
            "venue": "EU",
            "candidate_run_id": "news:EU:1234567890abcdef",
            "status": "success",
            "coverage_status": "partial",
            "eligible_symbol_count": 40,
            "items_read": 12,
            "eligible_items": 5,
            "challengers": [{"symbol": "SAP.DE"}],
        },
    )
    _write_json(
        tmp_path / "news_briefs" / "latest-EU.jsonl",
        {
            "venue": "EU",
            "brief_id": "brief:EU:1234567890abcdef",
            "as_of": "2026-07-10T02:10:00+00:00",
            "valid_until": "2026-07-11T02:10:00+00:00",
            "input_refs": {
                "candidate_scope_id": scope_id,
                "coverage": {
                    "status": "partial",
                    "candidate_count": 2,
                    "candidates_with_news": 1,
                },
            },
            "zones": {"EU": [{"point": "one"}, {"point": "two"}]},
            "alerts": [{"point": "alert"}],
        },
    )

    pipeline = _load_universe_pipeline_safe(tmp_path, {})["EU"]

    assert pipeline["scope"]["status"] == "ready"
    assert pipeline["scope"]["candidate_count"] == 2
    assert pipeline["scope"]["challenger_count"] == 1
    assert pipeline["scope"]["id_short"] != scope_id
    assert pipeline["scout"]["challenger_count"] == 1
    assert pipeline["scout"]["coverage"]["eligible_items"] == 5
    assert pipeline["brief"]["scope_match"] is True
    assert pipeline["brief"]["point_count"] == 3
    assert pipeline["brief"]["coverage"]["candidates_with_news"] == 1


def test_pipeline_marks_absent_and_corrupt_artifacts_without_raising(tmp_path: Path) -> None:
    pending = _load_universe_pipeline_safe(tmp_path, {})
    for venue in ("TW", "EU", "US"):
        assert pending[venue]["scope"]["status"] == "pending"
        assert pending[venue]["scout"]["status"] == "pending"
        assert pending[venue]["brief"]["status"] == "pending"
        assert pending[venue]["agent"]["status"] == "pending"
        assert pending[venue]["activation"]["status"] == "pending"

    corrupt_paths = [
        tmp_path / "candidate_scopes" / "current-TW.json",
        tmp_path / "news_challenger_runs" / "latest-TW.json",
        tmp_path / "news_briefs" / "latest-TW.jsonl",
        tmp_path / "universe_runs" / "latest-TW.json",
        tmp_path / "rotation_ledger.jsonl",
    ]
    for path in corrupt_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")

    unavailable = _load_universe_pipeline_safe(tmp_path, {})["TW"]
    assert unavailable["scope"]["status"] == "unavailable"
    assert unavailable["scout"]["status"] == "unavailable"
    assert unavailable["brief"]["status"] == "unavailable"
    assert unavailable["agent"]["status"] == "unavailable"
    assert unavailable["activation"]["status"] == "unavailable"


def test_runtime_state_always_exposes_all_venue_pipeline_entries(tmp_path: Path) -> None:
    state = load_runtime_state(
        state_dir=tmp_path,
        current_report_path=tmp_path / "missing-current.json",
        last_report_path=tmp_path / "missing-last.json",
        status_path=tmp_path / "missing-status.json",
        config_dir=str(tmp_path),
    )

    assert tuple(state["universe_pipeline"]) == ("TW", "EU", "US")
    assert state["universe_pipeline"]["TW"]["scope"]["status"] == "pending"
