from __future__ import annotations

import json

from trader.reporting.read_models.runtime_state import load_runtime_state


def test_runtime_state_exposes_radar_score_shadow_artifacts(tmp_path) -> None:
    (tmp_path / "radar_score_audit.json").write_text(
        json.dumps(
            {
                "status": "shadow_only",
                "selection_effect": "none",
                "global_component_balance": {"median_trend_share": 0.82},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "radar_score_bench.json").write_text(
        json.dumps(
            {
                "status": "insufficient_history",
                "coverage": {"unique_snapshot_count": 21},
            }
        ),
        encoding="utf-8",
    )

    state = load_runtime_state(state_dir=tmp_path, config_dir=tmp_path)

    assert state["radar_score_audit"]["selection_effect"] == "none"
    assert state["radar_score_bench"]["coverage"]["unique_snapshot_count"] == 21
