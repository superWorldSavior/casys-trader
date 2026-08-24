from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    MacroCollectionCompleted,
    MacroCollectionRegistered,
    MacroCollectionStarted,
    MacroCoverage,
    MacroDimensionState,
    MacroFactSource,
    MacroNumericValue,
    MacroObservationPublished,
    MacroScope,
    MacroSourceCompleted,
    MacroSourceFailed,
    MacroSourceFact,
    MacroWorldObservation,
)
from trader.infrastructure.state_db.world_macro_store import WorldMacroStore
from trader.reporting.read_models.world_macro_status import read_world_macro_status
from trader.reporting.read_models.world_status import read_world_model_status


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)
STALE_NOW = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
OLD_MTIME = datetime(2000, 1, 1, tzinfo=UTC)

_CLAIM = {
    "authority": "shadow_only",
    "decision_effect": "none",
    "recommendation": "NO_GO",
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": "not_attributable",
}


def _scope(*, kind: str = "venue", entity_id: str = "mic:XTAI") -> MacroScope:
    return MacroScope(kind=kind, entity_id=entity_id)


def _source() -> MacroFactSource:
    return MacroFactSource(
        provider_id="dbnomics",
        adapter_version="world_dbnomics_series.v1",
        source_record_id="stable-provider-id",
        source_ref="https://source.example/record",
    )


def _fact(**overrides: object) -> MacroSourceFact:
    values: dict[str, object] = {
        "fact_kind": "series_point",
        "metric_key": "policy_rate",
        "scope": _scope(kind="country", entity_id="iso-3166:US"),
        "value": MacroNumericValue(number=4.25, unit="percent"),
        "period": "2026-08",
        "occurred_at": "2026-08-23T00:00:00Z",
        "published_at": "2026-08-23T12:30:00Z",
        "ingested_at": "2026-08-23T12:31:10Z",
        "source": _source(),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroSourceFact(**values)  # type: ignore[arg-type]


def _dimension(
    *,
    dimension: str = "rates_regime",
    value: str = "stable",
    coverage_status: str = "complete",
    fact_refs: tuple[str, ...] | None = None,
) -> MacroDimensionState:
    return MacroDimensionState(
        dimension=dimension,
        value=value,
        coverage_status=coverage_status,
        method=MACRO_TRANSFORM_VERSION,
        fact_refs=() if fact_refs is None else fact_refs,
    )


def _observation(*, fact: MacroSourceFact | None = None, **overrides: object) -> MacroWorldObservation:
    resolved = fact if fact is not None else _fact()
    fact_ref = resolved.fact_version_id.value
    values: dict[str, object] = {
        "scope": _scope(),
        "cutoff_at": CUTOFF,
        "producer_version": MACRO_PRODUCER_VERSION,
        "transform_version": MACRO_TRANSFORM_VERSION,
        "source_registry_version": MACRO_SOURCE_REGISTRY_VERSION,
        "fact_refs": (fact_ref,),
        "features": {"macro_regime": "mixed", "rates_regime": "stable", "usd_regime": "unknown"},
        "dimensions": (
            _dimension(dimension="macro_regime", value="mixed", fact_refs=(fact_ref,)),
            _dimension(dimension="rates_regime", value="stable", fact_refs=(fact_ref,)),
            _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
        ),
        "coverage": MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=2,
            missing_source_ids=("broad_usd_index",),
        ),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroWorldObservation(**values)  # type: ignore[arg-type]


def _store(root: Path) -> WorldMacroStore:
    return WorldMacroStore(root, clock=lambda: READY)


def _assert_claims(payload: dict[str, Any]) -> None:
    for key, expected in _CLAIM.items():
        assert payload[key] == expected
    assert payload.get("winner") is not True


def _assert_separated_from_ml(payload: dict[str, Any]) -> None:
    for forbidden in ("evaluation", "impact", "contrasts", "matched_sets", "predictions_by_model"):
        assert forbidden not in payload
    study = payload["cohort"]
    assert study["separated"] is True
    assert "groups" not in study
    assert "contrasts" not in study
    assert study["recommendation"] == "NO_GO"


def _paths_snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    snapshot: dict[str, tuple[int, bytes]] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = (path.stat().st_mtime_ns, path.read_bytes())
    return snapshot


def _publish_partial_run(store: WorldMacroStore, *, fact: MacroSourceFact, observation: MacroWorldObservation) -> str:
    envelope = store.append_observation(observation)
    registered = MacroCollectionRegistered(
        scope=_scope(),
        cutoff_at=CUTOFF,
        expected_source_ids=("fed_policy_rate", "brent"),
    )
    store.append_event(registered)
    store.append_event(MacroCollectionStarted(run_id=registered.run_id))
    store.append_event(
        MacroSourceCompleted(
            run_id=registered.run_id,
            source_id="fed_policy_rate",
            fact_version_ids=(fact.fact_version_id.value,),
        )
    )
    store.append_event(MacroSourceFailed(run_id=registered.run_id, source_id="brent", reason="timeout"))
    store.append_event(MacroObservationPublished(run_id=registered.run_id, envelope=envelope))
    store.append_event(
        MacroCollectionCompleted(
            run_id=registered.run_id,
            status="completed_partial",
            observation_id=observation.observation_id,
        )
    )
    return registered.run_id


def test_missing_state_and_missing_db_are_strictly_read_only(tmp_path: Path) -> None:
    payload = read_world_macro_status(tmp_path, now=NOW)

    assert payload["schema_version"] == "world_macro_status.v1"
    assert payload["status"] == "not_started"
    assert payload["exists"] is False
    assert payload["collection"]["status"] == "not_started"
    assert payload["coverage"]["status"] == "missing"
    assert payload["freshness"]["status"] == "unknown"
    assert payload["gaps"]["missing_source_ids"] == []
    assert payload["attach"]["status"] == "not_started"
    _assert_claims(payload)
    _assert_separated_from_ml(payload)
    assert not (tmp_path / "world_macro").exists()
    assert not (tmp_path / "world_model.db").exists()
    assert list(tmp_path.iterdir()) == []


def test_corrupt_status_json_is_ignored_and_not_rewritten(tmp_path: Path) -> None:
    fact = _fact()
    observation = _observation(fact=fact)
    root = tmp_path / "world_macro"
    store = _store(root)
    store.append_fact(fact)
    store.append_observation(observation)
    status_path = root / "status.json"
    status_path.write_text("{not-json", encoding="utf-8")
    os.utime(status_path, (OLD_MTIME.timestamp(), OLD_MTIME.timestamp()))
    before = _paths_snapshot(tmp_path)

    payload = read_world_macro_status(tmp_path, now=NOW)

    assert payload["status"] == "loaded"
    assert payload["collection"]["facts"] == 1
    assert payload["coverage"]["observations"] == 1
    assert payload["coverage"]["status"] == "partial"
    assert status_path.read_text(encoding="utf-8") == "{not-json"
    assert _paths_snapshot(tmp_path) == before
    _assert_claims(payload)


def test_partial_and_corrupt_histories_stay_read_only(tmp_path: Path) -> None:
    root = tmp_path / "world_macro"
    facts = root / "facts"
    facts.mkdir(parents=True)
    (facts / "2026-08-23.jsonl").write_text(
        json.dumps(_fact().to_dict(), sort_keys=True) + "\n{not-json\n",
        encoding="utf-8",
    )
    (tmp_path / "gdelt").mkdir()
    (tmp_path / "gdelt" / "events.jsonl").write_text(
        json.dumps({"provider": "gdelt", "title": "should-not-be-read"}) + "\n",
        encoding="utf-8",
    )
    before = _paths_snapshot(tmp_path)

    payload = read_world_macro_status(tmp_path, now=NOW)

    assert payload["status"] == "partial"
    assert payload["collection"]["facts"] == 1
    assert payload["collection"]["proven_facts"] == 0
    assert payload["collection"]["unproven_facts"] == 1
    assert payload["gaps"]["unproven_facts"] == 1
    assert payload["gaps"]["gdelt"] == "excluded"
    assert payload["gaps"]["news_macro_brief"] == "excluded"
    assert "gdelt" not in json.dumps(payload["collection"]).lower()
    assert "should-not-be-read" not in json.dumps(payload)
    assert _paths_snapshot(tmp_path) == before
    _assert_claims(payload)


def test_collection_coverage_freshness_and_gaps_are_reconstructed_from_histories(tmp_path: Path) -> None:
    fact = _fact()
    observation = _observation(fact=fact)
    root = tmp_path / "world_macro"
    store = _store(root)
    store.append_fact(fact)
    run_id = _publish_partial_run(store, fact=fact, observation=observation)
    history = root / "observations" / "2026-08-23.jsonl"
    os.utime(history, (OLD_MTIME.timestamp(), OLD_MTIME.timestamp()))
    before = _paths_snapshot(tmp_path)

    fresh = read_world_macro_status(tmp_path, now=NOW)
    stale = read_world_macro_status(tmp_path, now=STALE_NOW)

    assert fresh["status"] == "loaded"
    assert fresh["lane_identity"] == "world.context.macro"
    collection = fresh["collection"]
    assert collection["status"] == "completed_partial"
    assert collection["runs"] == 1
    assert collection["by_status"]["completed_partial"] == 1
    assert collection["facts"] == 1
    assert collection["proven_facts"] == 1
    assert collection["events"] == 6
    assert collection["run_ids"] == [run_id]
    coverage = fresh["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["observations"] == 1
    assert coverage["proven_observations"] == 1
    assert coverage["required_sources"] == 3
    assert coverage["fresh_sources"] == 2
    assert coverage["by_scope"][0]["scope"] == {"kind": "venue", "entity_id": "mic:XTAI"}
    assert coverage["by_scope"][0]["missing_source_ids"] == ["broad_usd_index"]
    freshness = fresh["freshness"]
    assert freshness["status"] == "fresh"
    assert freshness["fresh_observations"] == 1
    assert freshness["stale_observations"] == 0
    assert freshness["as_of"] == NOW.isoformat()
    assert freshness["mtime_is_not_proof"] is True
    assert stale["freshness"]["status"] == "stale"
    assert stale["freshness"]["stale_observations"] == 1
    gaps = fresh["gaps"]
    assert gaps["missing_source_ids"] == ["broad_usd_index"]
    assert gaps["failed_source_ids"] == ["brent"]
    assert "usd_regime" in gaps["unknown_dimensions"]
    assert gaps["gdelt"] == "excluded"
    _assert_separated_from_ml(fresh)
    _assert_claims(fresh)
    assert _paths_snapshot(tmp_path) == before


def test_store_projection_counts_are_not_pit_authority(tmp_path: Path) -> None:
    fact = _fact()
    root = tmp_path / "world_macro"
    store = _store(root)
    store.append_fact(fact)
    (root / "status.json").write_text(
        json.dumps({"schema_version": "world_macro_status.v1", "facts": 999, "observations": 999, "events": 999}),
        encoding="utf-8",
    )

    payload = read_world_macro_status(tmp_path, now=NOW)

    assert payload["collection"]["facts"] == 1
    assert payload["coverage"]["observations"] == 0
    assert payload["collection"]["events"] == 0


def test_attach_is_separated_from_ml_study_and_missing_db_stays_read_only(tmp_path: Path) -> None:
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    fact = _fact()
    observation = _observation(fact=fact)
    store = _store(tmp_path / "world_macro")
    store.append_fact(fact)
    store.append_observation(observation)

    world = WorldModelStore(tmp_path / "world_model.db")
    try:
        assert world.append_episode(
            {
                "episode_id": "episode-macro-attach",
                "venue": "TW",
                "symbol": "2301.TW",
                "observed_at": "2026-08-23T13:00:00+00:00",
                "available_at": "2026-08-23T13:00:00+00:00",
                "as_of_bar_ts": "2026-08-23T13:00:00+00:00",
                "bar_interval": "1h",
                "feature_contract_version": "world_features.v1",
                "sampling_policy_version": "cycle_snapshot.v1",
                "training_eligible": True,
                "observation": {
                    "symbol": "2301.TW",
                    "categorical_features": {"macro_status": "partial"},
                },
                "source_evidence": {"source": "fixture", "kind": "macro_world_observation"},
            }
        )
        assert world.append_episode(
            {
                "episode_id": "episode-v1-unattached",
                "venue": "US",
                "symbol": "SPY",
                "observed_at": "2026-08-23T13:00:00+00:00",
                "available_at": "2026-08-23T13:00:00+00:00",
                "as_of_bar_ts": "2026-08-23T13:00:00+00:00",
                "bar_interval": "1h",
                "feature_contract_version": "world_features.v1",
                "sampling_policy_version": "cycle_snapshot.v1",
                "training_eligible": True,
                "observation": {"symbol": "SPY", "features": {"return": 0.01}},
                "source_evidence": {"source": "fixture"},
            }
        )
    finally:
        world.close()
    db_bytes = (tmp_path / "world_model.db").read_bytes()
    before_macro = _paths_snapshot(tmp_path / "world_macro")

    payload = read_world_macro_status(tmp_path, now=NOW)
    world_status = read_world_model_status(tmp_path, now=NOW)

    assert payload["attach"]["status"] == "partial"
    assert payload["attach"]["episodes"] == 2
    assert payload["attach"]["macro_present"] == 1
    assert payload["attach"]["macro_missing"] == 1
    assert payload["attach"]["lane_identity"] == "world.context.macro"
    _assert_separated_from_ml(payload)
    assert world_status["macro"]["collection"] == payload["collection"]
    assert world_status["macro"]["coverage"] == payload["coverage"]
    assert world_status["macro"]["freshness"] == payload["freshness"]
    assert world_status["macro"]["gaps"] == payload["gaps"]
    assert "evaluation" in world_status
    assert "impact" in world_status
    assert world_status["recommendation"] == "NO_GO"
    assert world_status["evaluation"] is not world_status["macro"]
    assert (tmp_path / "world_model.db").read_bytes() == db_bytes
    assert _paths_snapshot(tmp_path / "world_macro") == before_macro


def test_world_status_nests_macro_without_creating_absent_state(tmp_path: Path) -> None:
    world = read_world_model_status(tmp_path)
    macro = world["macro"]

    assert world["status"] == "not_started"
    assert world["authority"] == "shadow_only"
    assert world["decision_effect"] == "none"
    assert world["recommendation"] == "NO_GO"
    assert macro["status"] == "not_started"
    assert "evaluation" not in macro
    assert "impact" not in macro
    assert not (tmp_path / "world_macro").exists()
    assert not (tmp_path / "world_model.db").exists()
    assert list(tmp_path.glob("world_model.db*")) == []
