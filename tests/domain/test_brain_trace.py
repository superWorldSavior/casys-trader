from __future__ import annotations

from trader.domain.brain_trace import (
    episode_id,
    mandate_id_from_ref,
    observation_ref,
    post_effect_snapshot_ref,
    unique_identity,
)


def test_episode_id_is_stable_from_durable_task_and_process_identity() -> None:
    assert (
        episode_id(task_id=11214, process_instance_id="process-1")
        == "trader-episode:task:11214:process:process-1"
    )
    assert episode_id(task_id="11214", process_instance_id=" process-1 ") == episode_id(
        task_id=11214,
        process_instance_id="process-1",
    )


def test_episode_id_fails_closed_when_identity_is_missing_or_infra() -> None:
    assert episode_id(task_id=None, process_instance_id="process-1") is None
    assert episode_id(task_id=11214, process_instance_id="") is None
    assert episode_id(task_id="0", process_instance_id="process-1") is None
    assert (
        episode_id(
            task_id=11214,
            process_instance_id="process-1",
            decision_source="infra",
        )
        is None
    )


def test_unique_identity_fails_closed_on_conflict() -> None:
    assert unique_identity("process-1", "process-1", None) == "process-1"
    assert unique_identity("process-1", "process-2") is None
    assert unique_identity(None, "") is None
    assert unique_identity(12, "12") == "12"


def test_observation_and_snapshot_refs_are_explicit_when_unavailable() -> None:
    assert observation_ref(ts="2026-08-21T12:00:00+00:00", source="analysis_bar_as_of") == {
        "status": "available",
        "ts": "2026-08-21T12:00:00+00:00",
        "source": "analysis_bar_as_of",
    }
    assert observation_ref(ts=None, source="analysis_bar_as_of") == {
        "status": "unavailable",
        "ts": None,
        "source": None,
    }
    assert post_effect_snapshot_ref(snapshot_id="snap-1") == {
        "status": "available",
        "snapshot_id": "snap-1",
    }
    assert post_effect_snapshot_ref(snapshot_id=None) == {
        "status": "unavailable",
        "snapshot_id": None,
    }


def test_mandate_id_from_ref_does_not_invent_state() -> None:
    assert mandate_id_from_ref({"mandate_id": "universe-mandate:eu-1"}) == "universe-mandate:eu-1"
    assert mandate_id_from_ref({"mandate_id": ""}) is None
    assert mandate_id_from_ref(None) is None
    assert mandate_id_from_ref("universe-mandate:eu-1") is None
