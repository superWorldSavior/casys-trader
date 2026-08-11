import json

from trader.domain.universe.global_posture import GlobalUniversePosture
from trader.infrastructure.state_db.global_universe_posture_store import (
    GlobalUniversePostureStore,
)


def _posture(as_of: str, gross_mode: str = "cautious") -> GlobalUniversePosture:
    return GlobalUniversePosture(
        as_of=as_of,
        venue_posture={"TW": "watch", "EU": "selective", "US": "favor"},
        family_priority={
            "favored": ("us_semis",),
            "deprioritized": ("eu_industrials",),
        },
        gross_mode=gross_mode,
        net_bias="long",
        rationale="US families have the clearest breadth.",
        source_refs=("global-board-1",),
    )


def test_store_appends_only_material_global_universe_posture_changes(tmp_path) -> None:
    store = GlobalUniversePostureStore(tmp_path / "global_universe_postures")

    first, first_ref, first_changed = store.append_if_changed(
        _posture("2026-07-11T08:00:00+00:00")
    )
    same, same_ref, same_changed = store.append_if_changed(
        _posture("2026-07-11T08:00:00+00:00")
    )
    second, second_ref, second_changed = store.append_if_changed(
        _posture("2026-07-11T08:00:00+00:00", gross_mode="risk_off")
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "global_universe_postures" / "2026-07-11.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert first_changed is True
    assert same_changed is False
    assert second_changed is True
    assert same == first
    assert same_ref == first_ref
    assert first["venue_posture"] == {"TW": "watch", "EU": "selective", "US": "favor"}
    assert first_ref["posture_id"] == first["posture_id"]
    assert second_ref["posture_id"] == second["posture_id"]
    assert [row["posture_id"] for row in rows] == [
        first["posture_id"],
        second["posture_id"],
    ]
    assert store.read_current() == second


def test_store_keeps_latest_failure_separate_from_current_and_clears_it_on_success(tmp_path) -> None:
    store = GlobalUniversePostureStore(tmp_path / "global_universe_postures")
    current, _, _ = store.append_if_changed(_posture("2026-07-11T08:00:00+00:00"))
    failure = store.append_failure(
        {
            "as_of": "2026-07-11T12:00:00+00:00",
            "status": "error",
            "error_code": "TimeoutError",
            "error_message": "model deadline exceeded",
            "retry_lineage": "global-posture:current:previous",
            "retry_attempt": 2,
            "retry_delay_seconds": 3600,
            "next_retry_at": "2026-07-11T13:00:00+00:00",
        }
    )

    assert store.read_current() == current
    assert store.read_latest_failure() == failure
    assert json.loads(store.latest_failure_path.read_text(encoding="utf-8")) == failure
    assert [
        json.loads(line)
        for line in store.failure_path_for_date("2026-07-11").read_text(encoding="utf-8").splitlines()
    ] == [failure]

    refreshed, _, changed = store.append_if_changed(
        _posture("2026-07-11T12:30:00+00:00", gross_mode="risk_off")
    )

    assert changed is True
    assert store.read_current() == refreshed
    assert store.read_latest_failure() is None
    assert store.latest_failure_path.exists() is False
