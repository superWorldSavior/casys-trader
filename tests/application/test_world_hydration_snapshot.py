"""Hydration snapshots: restart without replaying the whole outcome ledger."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
from trader.application.world_model.gru import OnlineGRUWorldChallenger
from trader.application.world_model.hydration_snapshot import (
    SnapshotError,
    read_snapshot,
    snapshot_gate_mismatch,
    write_snapshot,
)
from trader.application.world_model.service import WorldModelService, _row_content_hash, _stable_id
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
)
from trader.infrastructure.state_db.world_model_store import WorldModelStore

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
HORIZON = "elapsed_4h.v1"
EPISODES = 6


def _episode(at: datetime, market_return: float) -> WorldEpisode:
    return WorldEpisode(
        observation=WorldObservation(
            venue="US",
            symbol="SPY",
            bar_interval="1h",
            as_of_bar_ts=at,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=at,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000.0,
                source="fixture",
            ),
            available_at=at,
            captured_at=at + timedelta(minutes=1),
            freshness="fresh",
            categorical_features={"asset_family": "equities", "venue": "US"},
            numeric_features={"return": market_return, "atr_pct": 0.02},
        )
    )


def _seeded_store(tmp_path, *, episodes: int = EPISODES) -> tuple[WorldModelStore, list[WorldEpisode]]:
    store = WorldModelStore(tmp_path / "world_model.db")
    seeded: list[WorldEpisode] = []
    for index in range(episodes):
        at = NOW - timedelta(hours=30) + timedelta(hours=5 * index)
        episode = _episode(at, 0.01 if index % 2 == 0 else -0.01)
        assert store.append_episode(episode)
        seeded.append(episode)
        move = "UP" if index % 2 == 0 else "DOWN"
        assert store.append_legacy_outcome_event(
            {
                "outcome_event_id": f"snapshot-fixture-out-{index}",
                "episode_id": episode.episode_id,
                "horizon_code": HORIZON,
                "status": "observed",
                "move_class": move,
                "training_eligible": True,
                "label_available_at": (at + timedelta(hours=5)).isoformat(),
                "target_at": (at + timedelta(hours=4)).isoformat(),
                "label": {
                    "move_class": move,
                    "simple_return": 0.01 if index % 2 == 0 else -0.01,
                    "target_at": (at + timedelta(hours=4)).isoformat(),
                    "source_raw_sha256": "a" * 64,
                },
                "evidence": {"source": "fixture", "source_raw_sha256": "a" * 64},
            }
        )
    return store, seeded


def _service(
    store: WorldModelStore, snapshot_path, *, extra_predictors=(), logger=None, labeler=None, bars=None
) -> WorldModelService:
    baseline = HierarchicalDirichletWorldBaseline()
    gru = OnlineGRUWorldChallenger()
    return WorldModelService(
        store=store,
        predictor=baseline,
        predictors=[gru, *extra_predictors],
        labeler=labeler,
        bar_provider=bars,
        horizons=(HORIZON,),
        snapshot_path=snapshot_path,
        logger=logger,
    )


class _PendingLabeler:
    """Never seals: pending episodes stay pending (active leaves unchanged)."""

    def label_horizon(self, episode, bars, horizon_id, *, now):
        identifier = str(episode["episode_id"])
        return {
            "outcome_event_id": f"pending:{identifier}:{horizon_id}",
            "episode_id": identifier,
            "horizon_id": horizon_id,
            "status": "pending",
            "training_eligible": False,
            "available_at": now.isoformat(),
        }


class _DummyBars:
    def get_bars(self, symbol, *, lookback, interval):
        return [{"ts": NOW.isoformat(), "close": 100.0}]


def _fingerprints(service: WorldModelService) -> dict[str, str]:
    out: dict[str, str] = {}
    for predictor in service.predictors:
        fingerprint = getattr(predictor, "model_fingerprint", None)
        if not callable(fingerprint):
            continue
        model_id, model_version = service._predictor_identities[id(predictor)]
        out[f"{model_id}:{model_version}"] = fingerprint(HORIZON)
    return out


def test_snapshot_round_trip_matches_full_replay(tmp_path) -> None:
    store, episodes = _seeded_store(tmp_path)
    snapshot_path = tmp_path / "world_model_snapshot.json"

    first = _service(store, snapshot_path)
    first_report = first.mature_pending(NOW)
    assert first_report["errors"] == []
    assert first_report["baseline_replayed"] == EPISODES
    assert first_report["model_replayed"] == 2 * EPISODES
    assert first_report["model_snapshot"] == {"restored": False, "reason": "snapshot_missing"}
    assert first_report["model_snapshot_save"] == {"saved": True, "predictors": 2}
    assert snapshot_path.exists()
    expected = _fingerprints(first)
    target = episodes[-1]
    expected_probas = {
        "baseline": first.predictors[0].predict_proba(target.observation, HORIZON, prediction_at=NOW),
        "gru": first.predictors[1].predict_proba(target, HORIZON, prediction_at=NOW),
    }

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "snapshot_format",
        "saved_at",
        "horizons",
        "eligible_episode_fingerprint",
        "active_outcome_fingerprint",
        "model_hydrated_through",
        "predictors",
    }
    assert all(
        set(entry) == {"model_id", "model_version", "contract", "state", "state_sha256"}
        for entry in payload["predictors"]
    )
    # Learned state only: episode content never crosses into the file.
    assert "categorical_features" not in snapshot_path.read_text(encoding="utf-8")

    second = _service(store, snapshot_path)
    second_report = second.mature_pending(NOW)
    assert second_report["errors"] == []
    assert second_report["model_snapshot"]["restored"] is True
    assert sorted(second_report["model_snapshot"]["predictors"]) == sorted(expected)
    assert second_report["model_replayed"] == 0
    assert second_report["baseline_replayed"] == 0
    assert second_report["model_observations_replayed"] == EPISODES
    assert second_report["model_snapshot_save"] == {}
    assert _fingerprints(second) == expected
    assert second.predictors[0].predict_proba(target.observation, HORIZON, prediction_at=NOW) == expected_probas[
        "baseline"
    ]
    assert second.predictors[1].predict_proba(target, HORIZON, prediction_at=NOW) == expected_probas["gru"]
    store.close()


def test_predict_does_not_change_learned_state(tmp_path) -> None:
    store, episodes = _seeded_store(tmp_path)
    service = _service(store, tmp_path / "snap.json")
    assert service.mature_pending(NOW)["errors"] == []
    before = _fingerprints(service)
    target = episodes[-1]
    service.predictors[0].predict_proba(target.observation, HORIZON, prediction_at=NOW)
    service.predictors[1].predict_proba(target, HORIZON, prediction_at=NOW)
    assert _fingerprints(service) == before
    store.close()


def test_snapshot_contract_survives_json_round_trip() -> None:
    for predictor in (HierarchicalDirichletWorldBaseline(), OnlineGRUWorldChallenger()):
        contract = predictor.snapshot_contract
        assert json.loads(json.dumps(contract)) == contract


def _tampered(payload: dict, tamper: str) -> str:
    if tamper == "eligible":
        payload["eligible_episode_fingerprint"] = "0" * 64
        return "snapshot_eligible_episodes_changed"
    if tamper == "active":
        payload["active_outcome_fingerprint"] = "0" * 64
        return "snapshot_active_outcomes_changed"
    if tamper == "horizons":
        payload["horizons"] = [*payload["horizons"], "elapsed_1d.v1"]
        return "snapshot_horizons_changed"
    if tamper == "contract":
        payload["predictors"][0]["contract"]["alpha"] = 999.0
        model_id = payload["predictors"][0]["model_id"]
        version = payload["predictors"][0]["model_version"]
        return f"snapshot_predictor_contract_changed:{model_id}:{version}"
    if tamper == "predictor_set":
        del payload["predictors"][1]
        return "snapshot_predictor_set_changed"
    if tamper == "cutoff":
        payload["model_hydrated_through"] = (NOW + timedelta(hours=1)).isoformat()
        return "snapshot_cutoff_moved_backwards"
    if tamper == "cutoff_unknown":
        payload["model_hydrated_through"] = None
        return "snapshot_cutoff_unknown"
    if tamper == "checksum":
        state = payload["predictors"][0]["state"][HORIZON]["global"]
        state["UP"] = int(state.get("UP", 0)) + 1
        return "snapshot_state_checksum_mismatch"
    raise AssertionError(f"unknown tamper {tamper}")


@pytest.mark.parametrize(
    "tamper",
    ["eligible", "active", "horizons", "contract", "predictor_set", "cutoff", "cutoff_unknown", "checksum"],
)
def test_snapshot_gate_rejections_replay_and_heal(tmp_path, tamper: str) -> None:
    store, _ = _seeded_store(tmp_path)
    snapshot_path = tmp_path / "world_model_snapshot.json"
    first = _service(store, snapshot_path)
    assert first.mature_pending(NOW)["errors"] == []
    expected = _fingerprints(first)

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    reason = _tampered(payload, tamper)
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    second = _service(store, snapshot_path)
    report = second.mature_pending(NOW)
    assert report["errors"] == []
    assert report["model_snapshot"] == {"restored": False, "reason": reason}
    assert report["model_replayed"] == 2 * EPISODES
    assert report["model_snapshot_save"] == {"saved": True, "predictors": 2}
    assert _fingerprints(second) == expected
    # The replay healed the file: a third boot restores again.
    third = _service(store, snapshot_path)
    third_report = third.mature_pending(NOW)
    assert third_report["errors"] == []
    assert third_report["model_snapshot"]["restored"] is True
    assert _fingerprints(third) == expected
    store.close()


def test_snapshot_corrupt_file_replays(tmp_path) -> None:
    store, _ = _seeded_store(tmp_path)
    snapshot_path = tmp_path / "world_model_snapshot.json"
    first = _service(store, snapshot_path)
    assert first.mature_pending(NOW)["errors"] == []
    expected = _fingerprints(first)
    snapshot_path.write_text("{not json", encoding="utf-8")

    second = _service(store, snapshot_path)
    report = second.mature_pending(NOW)
    assert report["errors"] == []
    assert report["model_snapshot"]["restored"] is False
    assert report["model_snapshot"]["reason"].startswith("snapshot_corrupt:")
    assert report["model_replayed"] == 2 * EPISODES
    assert _fingerprints(second) == expected
    store.close()


class _LegacyPredictor:
    model_id = "legacy_predictor"
    model_version = "v0"

    def __init__(self) -> None:
        self.applied: list[tuple[str, str]] = []

    def apply_outcome(self, outcome, observation, *, available_through):
        self.applied.append((str(outcome["episode_id"]), str(outcome.get("horizon_id") or "")))
        return None


def test_legacy_predictor_disables_snapshots(tmp_path) -> None:
    store, _ = _seeded_store(tmp_path)
    snapshot_path = tmp_path / "world_model_snapshot.json"
    legacy = _LegacyPredictor()
    service = _service(store, snapshot_path, extra_predictors=[legacy])
    report = service.mature_pending(NOW)
    assert report["errors"] == []
    assert legacy.applied
    assert report["model_snapshot_save"] == {
        "saved": False,
        "reason": "snapshot_predictor_unsupported:legacy_predictor:v0",
    }
    assert not snapshot_path.exists()

    valid_path = tmp_path / "valid_snapshot.json"
    assert _service(store, valid_path).mature_pending(NOW)["errors"] == []
    mixed = _service(store, valid_path, extra_predictors=[_LegacyPredictor()])
    mixed_report = mixed.mature_pending(NOW)
    assert mixed_report["errors"] == []
    assert mixed_report["model_snapshot"] == {
        "restored": False,
        "reason": "snapshot_predictor_unsupported:legacy_predictor:v0",
    }
    store.close()


def test_snapshot_save_failure_keeps_hydration(tmp_path) -> None:
    store, _ = _seeded_store(tmp_path)
    service = _service(store, tmp_path / "no_such_dir" / "snap.json")
    report = service.mature_pending(NOW)
    assert report["errors"] == []
    assert report["status"] == "ok"
    assert report["baseline_replayed"] == EPISODES
    assert report["model_snapshot_save"]["saved"] is False
    assert report["model_snapshot_save"]["reason"].startswith("snapshot_unwritable:")
    store.close()


def test_gru_lazy_state_round_trip_without_store() -> None:
    episode = _episode(NOW - timedelta(hours=6), 0.01)
    gru = OnlineGRUWorldChallenger()
    gru.observe_episode(episode)
    expected_proba = gru.predict_proba(episode, HORIZON, prediction_at=NOW)
    expected_fingerprint = gru.model_fingerprint(HORIZON)
    assert gru.snapshot_learned_state()

    restored = OnlineGRUWorldChallenger()
    restored.restore_learned_state(json.loads(json.dumps(gru.snapshot_learned_state())))
    assert restored.model_fingerprint(HORIZON) == expected_fingerprint
    restored.observe_episode(episode)
    assert restored.predict_proba(episode, HORIZON, prediction_at=NOW) == expected_proba


def test_baseline_empty_state_round_trip() -> None:
    baseline = HierarchicalDirichletWorldBaseline()
    assert baseline.snapshot_learned_state() == {}
    restored = HierarchicalDirichletWorldBaseline()
    restored.restore_learned_state(json.loads(json.dumps(baseline.snapshot_learned_state())))
    assert restored.model_fingerprint(HORIZON) == baseline.model_fingerprint(HORIZON)


def _valid_baseline_horizon() -> dict:
    return {
        "exact": [{"state": [["venue", "US"]], "counts": {"UP": 2}}],
        "coarse": [],
        "global": {"UP": 2, "DOWN": 1},
        "training_cutoff": NOW.isoformat(),
        "applied_events": {"event-1": "sig-1"},
        "comparison_event_signatures": ["sig-1"],
    }


def _valid_gru_horizon() -> dict:
    return {
        "parameters": {"weight": {"shape": [2, 2], "data": [[0.1, -0.2], [0.3, 0.4]]}},
        "applied_events": {"event-1": "sig-1"},
        "comparison_event_signatures": ["sig-1"],
        "support": 1,
        "training_steps": 1,
        "training_cutoff": NOW.isoformat(),
    }


def test_valid_fixtures_restore_cleanly() -> None:
    HierarchicalDirichletWorldBaseline().restore_learned_state({HORIZON: _valid_baseline_horizon()})
    OnlineGRUWorldChallenger().restore_learned_state({HORIZON: _valid_gru_horizon()})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda horizon: horizon.update({"global": {"UP": True}}),
        lambda horizon: horizon.update({"global": {"UP": 1.5}}),
        lambda horizon: horizon.update({"training_cutoff": "not-a-date"}),
        lambda horizon: horizon.update({"training_cutoff": 123}),
        lambda horizon: horizon.update({"training_cutoff": "2026-08-28T12:00:00"}),
        lambda horizon: horizon.update({"exact": [{"state": [["venue"]], "counts": {}}]}),
        lambda horizon: horizon.update({"exact": [{"state": [[1, 2]], "counts": {}}]}),
        lambda horizon: horizon.update({"applied_events": {"event-1": 1}}),
        lambda horizon: horizon.update({"comparison_event_signatures": "sig-1"}),
        lambda horizon: horizon.update({"coarse": {}}),
    ],
)
def test_baseline_restore_rejects_invalid_shapes(mutate) -> None:
    horizon = _valid_baseline_horizon()
    mutate(horizon)
    with pytest.raises(SnapshotError):
        HierarchicalDirichletWorldBaseline().restore_learned_state({HORIZON: horizon})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda horizon: horizon.update(
            {"parameters": {"weight": {"shape": [3], "data": [0.1, -0.2]}}}
        ),
        lambda horizon: horizon.update(
            {"parameters": {"weight": {"shape": [2], "data": ["x", "y"]}}}
        ),
        lambda horizon: horizon.update(
            {"parameters": {"weight": {"shape": [2, 2], "data": [[0.1], [0.2, 0.3, 0.4]]}}}
        ),
        lambda horizon: horizon.update({"parameters": {"weight": {"shape": [2], "data": None}}}),
        lambda horizon: horizon.update({"parameters": []}),
        lambda horizon: horizon.update({"parameters": {}}),
        lambda horizon: horizon.update({"support": True}),
        lambda horizon: horizon.update({"training_steps": 1.0}),
        lambda horizon: horizon.update({"training_cutoff": "not-a-date"}),
        lambda horizon: horizon.update({"training_cutoff": "2026-08-28T12:00:00"}),
        lambda horizon: horizon.update({"applied_events": {"event-1": 1}}),
    ],
)
def test_gru_restore_rejects_invalid_shapes(mutate) -> None:
    horizon = _valid_gru_horizon()
    mutate(horizon)
    with pytest.raises(SnapshotError):
        OnlineGRUWorldChallenger().restore_learned_state({HORIZON: horizon})


@pytest.mark.parametrize("state", ["not-a-dict", [], None, {HORIZON: []}, {HORIZON: None}])
def test_restore_rejects_invalid_roots(state) -> None:
    with pytest.raises(SnapshotError):
        HierarchicalDirichletWorldBaseline().restore_learned_state(state)
    with pytest.raises(SnapshotError):
        OnlineGRUWorldChallenger().restore_learned_state(state)


def test_failed_restore_keeps_previous_state() -> None:
    episode = _episode(NOW - timedelta(hours=6), 0.01)
    gru = OnlineGRUWorldChallenger()
    gru.observe_episode(episode)
    gru.predict_proba(episode, HORIZON, prediction_at=NOW)
    expected = gru.model_fingerprint(HORIZON)
    with pytest.raises(SnapshotError):
        gru.restore_learned_state({HORIZON: {"support": "NaN"}})
    assert gru.model_fingerprint(HORIZON) == expected


def _minimal_envelope() -> dict:
    return {
        "snapshot_format": 1,
        "saved_at": NOW.isoformat(),
        "horizons": [HORIZON],
        "eligible_episode_fingerprint": "e" * 64,
        "active_outcome_fingerprint": "a" * 64,
        "model_hydrated_through": NOW.isoformat(),
        "predictors": [],
    }


def test_envelope_rejects_bool_format_and_naive_cutoff(tmp_path) -> None:
    path = tmp_path / "snap.json"
    payload = _minimal_envelope()
    payload["snapshot_format"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotError, match="snapshot_format_unsupported"):
        read_snapshot(path)
    payload["snapshot_format"] = 1
    payload["model_hydrated_through"] = "2026-08-28T12:00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotError, match="snapshot_envelope_invalid"):
        read_snapshot(path)


def test_write_validates_envelope_before_touching_disk(tmp_path) -> None:
    path = tmp_path / "snap.json"
    with pytest.raises(SnapshotError, match="snapshot_envelope_invalid"):
        write_snapshot(path, {"snapshot_format": 1})
    assert not path.exists()


class _SnapshotCapableDouble:
    """Minimal snapshot-protocol predictor with scriptable failures."""

    model_id = "snapshot_capable_double"
    model_version = "v1"

    def __init__(self, *, fail_observe: bool = False, fail_restore: bool = False) -> None:
        self.fail_observe = fail_observe
        self.fail_restore = fail_restore
        self.applied: list[str] = []

    def reset_for_replay(self) -> None:
        self.applied.clear()

    @property
    def snapshot_contract(self) -> dict:
        return {"model_id": self.model_id, "model_version": self.model_version}

    def snapshot_learned_state(self) -> dict:
        return {"applied": list(self.applied)}

    def restore_learned_state(self, state: dict) -> None:
        if self.fail_restore:
            raise SnapshotError("snapshot_state_invalid:boom")
        if not isinstance(state, dict) or not isinstance(state.get("applied"), list):
            raise SnapshotError("snapshot_state_invalid:root")
        self.reset_for_replay()
        self.applied.extend(str(item) for item in state["applied"])

    def observe_episode(self, episode) -> str:
        if self.fail_observe:
            raise RuntimeError("observe_boom")
        return "observed"

    def apply_outcome(self, outcome, observation=None, *, available_through=None):
        self.applied.append(str(outcome["episode_id"]))
        return None


def test_observe_failure_after_restore_falls_through_to_replay(tmp_path, caplog) -> None:
    store, _ = _seeded_store(tmp_path)
    snapshot_path = tmp_path / "world_model_snapshot.json"
    logger = logging.getLogger("snapshot-observe-fail-test")
    healthy = _service(store, snapshot_path, extra_predictors=[_SnapshotCapableDouble()])
    assert healthy.mature_pending(NOW)["errors"] == []

    failing = _service(
        store, snapshot_path, extra_predictors=[_SnapshotCapableDouble(fail_observe=True)], logger=logger
    )
    with caplog.at_level(logging.WARNING, logger="snapshot-observe-fail-test"):
        report = failing.mature_pending(NOW)
    # Weights restored, observe failed, full replay ran instead of a hybrid
    # (twice: mature hydrates before and after maturing, both fall through).
    assert report["model_snapshot"] == {"restored": False, "reason": "snapshot_observe_failed"}
    assert report["baseline_replayed"] == 2 * EPISODES
    assert report["model_replayed"] == 6 * EPISODES
    assert report["errors"]
    assert "snapshot_observe_failed" in caplog.text
    store.close()


def test_restore_failure_falls_through_to_replay(tmp_path) -> None:
    store, _ = _seeded_store(tmp_path)
    snapshot_path = tmp_path / "world_model_snapshot.json"
    first = _service(store, snapshot_path, extra_predictors=[_SnapshotCapableDouble()])
    assert first.mature_pending(NOW)["errors"] == []
    expected = _fingerprints(first)

    flaky = _SnapshotCapableDouble(fail_restore=True)
    second = _service(store, snapshot_path, extra_predictors=[flaky])
    report = second.mature_pending(NOW)
    assert report["errors"] == []
    assert report["model_snapshot"] == {"restored": False, "reason": "snapshot_state_invalid:boom"}
    assert report["baseline_replayed"] == EPISODES
    assert flaky.applied and len(flaky.applied) == EPISODES
    for predictor in second.predictors[:2]:
        identity = second._predictor_identities[id(predictor)]
        assert predictor.model_fingerprint(HORIZON) == expected[f"{identity[0]}:{identity[1]}"]
    store.close()


def test_incremental_append_saves_and_restart_restores(tmp_path) -> None:
    store, _ = _seeded_store(tmp_path)
    snapshot_path = tmp_path / "world_model_snapshot.json"
    labeler, bars = _PendingLabeler(), _DummyBars()
    first = _service(store, snapshot_path, labeler=labeler, bars=bars)
    assert first.mature_pending(NOW)["errors"] == []
    expected = _fingerprints(first)

    late = _episode(NOW - timedelta(hours=1), 0.02)
    assert store.append_episode(late)
    extended = first.mature_pending(NOW)
    assert extended["errors"] == []
    assert extended["model_replayed"] == 0
    assert extended["model_snapshot_save"] == {"saved": True, "predictors": 2}

    second = _service(store, snapshot_path, labeler=labeler, bars=bars)
    report = second.mature_pending(NOW)
    assert report["errors"] == []
    assert report["model_snapshot"]["restored"] is True
    assert report["model_replayed"] == 0
    assert report["model_observations_replayed"] == EPISODES + 1
    assert _fingerprints(second) == expected
    store.close()


def test_gate_rejects_unbounded_restore() -> None:
    payload = _minimal_envelope()
    gate = {
        "eligible_episode_fingerprint": "e" * 64,
        "active_outcome_fingerprint": "a" * 64,
        "predictor_contracts": {},
        "horizons": [HORIZON],
    }
    assert snapshot_gate_mismatch(payload, now=None, **gate) == "snapshot_cutoff_unknown"
    payload["model_hydrated_through"] = None
    assert snapshot_gate_mismatch(payload, now=NOW, **gate) == "snapshot_cutoff_unknown"


def test_row_content_hash_prefers_valid_stored_digest() -> None:
    built: list[str] = []

    def _payload():
        built.append("x")
        return {"a": 1}

    for stored in ("sha256:" + "ab" * 32, "AB" * 32):
        assert _row_content_hash({"payload_sha256": stored}, _payload, prefix="world-test") == stored
    assert built == []


@pytest.mark.parametrize("stored", [None, "", "  ", "not-a-sha", "sha256:xyz", "ab" * 31 + "!", 123, ["x"]])
def test_row_content_hash_falls_back_to_content(stored) -> None:
    assert _row_content_hash({"payload_sha256": stored}, lambda: {"a": 1}, prefix="world-test") == _stable_id(
        "world-test", {"a": 1}
    )


def test_row_content_hash_falls_back_when_digest_missing() -> None:
    assert _row_content_hash({"episode_id": "e1"}, lambda: {"a": 1}, prefix="world-test") == _stable_id(
        "world-test", {"a": 1}
    )
