from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.application.world_model.dynamics_service import DynamicsReplayRequest, run_dynamics_replay
from trader.reporting.read_models.world_dynamics import project_world_dynamics_result


@pytest.mark.parametrize("max_origins", [8, 64])
def test_report_describes_the_actual_automatic_or_offline_evaluation_bound(max_origins: int) -> None:
    request = DynamicsReplayRequest(datetime(2026, 10, 9, 12, tzinfo=timezone.utc),
                                    max_evaluation_origins=max_origins)
    result = run_dynamics_replay((), request)
    payload = project_world_dynamics_result(result, request, {
        "venue": "US", "symbol": "SPY", "bar_interval": "15m",
        "evidence_kind": "observed_market_bar",
        "availability_policy": "max(first_seen_at,recorded_at)",
        "missing_bars_policy": "exclude_gaps_no_fetch_or_backfill",
    })
    assert payload["evaluation"]["max_evaluation_origins"] == max_origins
    assert payload["evaluation"]["origin_selection"] == "evenly_spaced_before_future_label_inspection"
    assert payload["evaluation"]["origins"] == 0
    assert payload["data"]["availability_policy"] == "max(first_seen_at,recorded_at)"
    assert payload["data"]["read_evidence"] == 0
    assert payload["rollout"]["origin_evidence_id"] is None
    assert payload["schema_version"] == "world_dynamics_report.v2"
