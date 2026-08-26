from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
from tests.state_db.test_world_pattern_formation_query import _seed_labeled
from trader.domain.world_episode import WorldEpisode, WorldOutcome, canonical_sha256
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_pattern_outcome_query import SqlitePatternOutcomeLeafQuery


UTC = timezone.utc
AS_OF = datetime(2026, 9, 12, tzinfo=UTC)
POST = datetime(2026, 9, 3, tzinfo=UTC)
_QUERY = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_outcome_query.py"


def test_outcome_query_is_readonly_and_is_the_only_new_outcome_reader() -> None:
    source = _QUERY.read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "query_only=ON" in source
    assert "world_outcome_events" in source
    assert "StateDb(" not in source
    assert "WorldPatternStore(" not in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source


def test_active_leaves_are_returned_and_missing_horizons_stay_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=True, close=False)
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    world: WorldModelStore = seeded["world"]  # type: ignore[assignment]
    four_h = WorldOutcome(
        episode_id=episode.episode_id,
        horizon={"horizon_id": "elapsed_4h.v1", "duration_seconds": 4 * 60 * 60},
        status="observed",
        target_at=POST + timedelta(hours=4),
        available_at=POST + timedelta(hours=4, minutes=5),
        computed_at=POST + timedelta(hours=4, minutes=5),
        anchor_close=100.0,
        endpoint_close=100.2,
        endpoint_bar_ts=POST + timedelta(hours=4),
        source="analysis_bars",
        source_raw_sha256=canonical_sha256({"horizon": "elapsed_4h.v1"}),
    )
    world.append_outcome_event(four_h)
    world.close()
    seeded["graph"].close()  # type: ignore[union-attr]

    leaves = SqlitePatternOutcomeLeafQuery(seeded["db_path"]).load_active_observed_leaves(
        episode_ids=(episode.episode_id,),
        horizon_ids=("elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1"),
        as_of=AS_OF,
    )
    horizons = {item.horizon.horizon_id for item in leaves}
    assert horizons == {"elapsed_4h.v1", "elapsed_1d.v1"}
    assert "elapsed_3d.v1" not in horizons
    assert all(item.status == "observed" for item in leaves)
