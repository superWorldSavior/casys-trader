from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
    world_subject_content_sha256,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
    MacroCollectionCompleted,
    MacroCollectionEventId,
    MacroCollectionRegistered,
    MacroCollectionRun,
    MacroCollectionRunId,
    MacroCollectionStarted,
    MacroCoverage,
    MacroDimensionState,
    MacroFactSource,
    MacroNumericValue,
    MacroObservationEnvelope,
    MacroObservationId,
    MacroObservationPublished,
    MacroScope,
    MacroSourceCompleted,
    MacroSourceFailed,
    MacroSourceFact,
    MacroSourceFactVersionId,
    MacroWorldObservation,
    reconcile_macro_source_fact,
    reconcile_macro_world_observation,
)
from trader.infrastructure.state_db import availability_receipt as receipt_mod
from trader.infrastructure.state_db.availability_receipt import (
    WorldAvailabilityJsonlReceiptStore,
    load_receipts,
    parse_world_availability_receipt,
)
from trader.infrastructure.state_db.world_macro_store import WORLD_MACRO_STORE_ID, WorldMacroStore


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
BOOT = datetime(2026, 8, 23, 13, 5, tzinfo=UTC)
OLD_MTIME = datetime(2000, 1, 1, tzinfo=UTC)


def _scope(*, kind: str = "venue", entity_id: str = "mic:XTAI") -> MacroScope:
    return MacroScope(kind=kind, entity_id=entity_id)


def _source(**overrides: object) -> MacroFactSource:
    values: dict[str, object] = {
        "provider_id": "official_provider",
        "adapter_version": "official_provider.v1",
        "source_record_id": "stable-provider-id",
        "source_ref": "https://source.example/record",
    }
    values.update(overrides)
    return MacroFactSource(**values)  # type: ignore[arg-type]


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


def _store(tmp_path: Path, *, clock=None) -> WorldMacroStore:
    return WorldMacroStore(tmp_path, clock=clock or (lambda: READY))


def _history_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_public_append_surface_does_not_accept_ready_at() -> None:
    init = inspect.signature(WorldMacroStore.__init__)
    assert "ready_at" not in init.parameters
    assert "storage_locator" not in init.parameters
    for name in ("append_fact", "append_observation", "append_event"):
        params = inspect.signature(getattr(WorldMacroStore, name)).parameters
        assert "ready_at" not in params
        assert "clock" not in params
        assert "storage_locator" not in params
    list_params = inspect.signature(WorldMacroStore.list_candidates_available_through).parameters
    assert list(list_params) == ["self", "scope", "cutoff_at"]
    load_params = inspect.signature(WorldMacroStore.load).parameters
    assert list(load_params) == ["self", "run_id"]


def test_append_fact_writes_history_then_store_attested_receipt(tmp_path: Path) -> None:
    fact = _fact()
    ref = _store(tmp_path).append_fact(fact)
    history = tmp_path / "facts" / "2026-08-23.jsonl"
    receipts = tmp_path / "facts" / "availability_receipts" / "2026-08-23.jsonl"
    assert isinstance(ref, PersistedWorldRef)
    assert isinstance(ref.identity, MacroSourceFactVersionId)
    assert ref.identity == fact.fact_version_id
    assert ref.receipt.ready_at == READY
    assert ref.receipt.schema_version == "availability_receipt.v2"
    assert ref.receipt._store_attested is True
    assert ref.receipt.subject.kind == "macro_source_fact"
    assert ref.receipt.subject.subject_id == fact.fact_version_id.value
    assert ref.receipt.scope == "country:iso-3166:US"
    assert ref.receipt.storage_locator.store_id == WORLD_MACRO_STORE_ID
    assert ref.receipt.storage_locator.path == "facts/2026-08-23.jsonl"
    rows = _history_rows(history)
    assert len(rows) == 1
    loaded = MacroSourceFact.from_mapping(rows[0])
    assert loaded.fact_version_id == fact.fact_version_id
    assert loaded.content_sha256 == fact.content_sha256
    assert loaded.ingested_at == fact.ingested_at
    assert len(load_receipts(receipts)) == 1
    parsed = parse_world_availability_receipt(load_receipts(receipts)[-1])
    assert parsed is not None
    assert parsed.receipt_id == ref.receipt.receipt_id


def test_append_observation_returns_envelope_bound_to_store_receipt(tmp_path: Path) -> None:
    observation = _observation()
    envelope = _store(tmp_path).append_observation(observation)
    assert isinstance(envelope, MacroObservationEnvelope)
    assert envelope.observation == observation
    assert envelope.persisted.identity == MacroObservationId(observation.observation_id)
    assert envelope.persisted.receipt.ready_at == READY
    assert envelope.persisted.receipt._store_attested is True
    assert envelope.persisted.receipt.subject.kind == MACRO_WORLD_OBSERVATION_SUBJECT_KIND
    assert envelope.persisted.receipt.subject.content_sha256 == observation.content_sha256
    assert envelope.persisted.receipt.scope == "venue:mic:XTAI"
    assert envelope.evidence.receipt == envelope.persisted.receipt
    assert envelope.evidence.first_seen_at == READY
    assert envelope.evidence.effective_ready_at == READY
    history = tmp_path / "observations" / "2026-08-23.jsonl"
    payload = _history_rows(history)[-1]
    assert "content_sha256" not in payload
    assert world_subject_content_sha256(payload) == observation.content_sha256
    assert MacroWorldObservation.from_mapping(payload).observation_id == observation.observation_id


def test_append_is_idempotent_and_does_not_rewrite_history(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    real_append = receipt_mod.append_jsonl_and_fsync

    def tracked_append(path, payload):
        events.append(Path(path).as_posix())
        return real_append(path, payload)

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", tracked_append)
    store = _store(tmp_path)
    fact = _fact()
    first = store.append_fact(fact)
    second = store.append_fact(fact)
    observation = _observation(fact=fact)
    first_obs = store.append_observation(observation)
    second_obs = store.append_observation(observation)
    assert first.receipt.receipt_id == second.receipt.receipt_id
    assert first.receipt.ready_at == second.receipt.ready_at == READY
    assert first_obs.observation.observation_id == second_obs.observation.observation_id
    assert first_obs.persisted.receipt.receipt_id == second_obs.persisted.receipt.receipt_id
    assert len(_history_rows(tmp_path / "facts" / "2026-08-23.jsonl")) == 1
    assert len(_history_rows(tmp_path / "observations" / "2026-08-23.jsonl")) == 1
    assert len(load_receipts(tmp_path / "facts" / "availability_receipts" / "2026-08-23.jsonl")) == 1
    assert len(load_receipts(tmp_path / "observations" / "availability_receipts" / "2026-08-23.jsonl")) == 1
    history_writes = [path for path in events if path.endswith("2026-08-23.jsonl") and "availability_receipts" not in path]
    assert len(history_writes) == 2


def test_same_identity_different_content_is_an_explicit_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fact = _fact()
    store.append_fact(fact)
    conflicting_fact = _fact(occurred_at="2026-08-22T00:00:00Z")
    assert conflicting_fact.fact_version_id == fact.fact_version_id
    assert conflicting_fact.content_sha256 != fact.content_sha256
    with pytest.raises(ValueError, match="conflict"):
        store.append_fact(conflicting_fact)
    observation = _observation(fact=fact)
    store.append_observation(observation)
    different_features = dict(observation.features)
    different_features["macro_regime"] = "quiet"
    conflicting_observation = _observation(
        fact=fact,
        features=different_features,
        dimensions=(
            _dimension(dimension="macro_regime", value="quiet", fact_refs=observation.fact_refs),
            _dimension(dimension="rates_regime", value="stable", fact_refs=observation.fact_refs),
            _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
        ),
    )
    assert conflicting_observation.observation_id == observation.observation_id
    assert conflicting_observation.content_sha256 != observation.content_sha256
    with pytest.raises(ValueError, match="conflict"):
        store.append_observation(conflicting_observation)
    assert len(_history_rows(tmp_path / "facts" / "2026-08-23.jsonl")) == 1
    assert len(_history_rows(tmp_path / "observations" / "2026-08-23.jsonl")) == 1
    assert reconcile_macro_source_fact(fact, fact) == fact
    assert reconcile_macro_world_observation(observation, observation) == observation


def test_fsyncs_history_then_clock_then_receipt(tmp_path: Path, monkeypatch) -> None:
    events: list[str] = []
    fact = _fact()
    history_abs = (tmp_path / "facts" / "2026-08-23.jsonl").resolve()
    receipt_abs = (tmp_path / "facts" / "availability_receipts" / "2026-08-23.jsonl").resolve()
    real_append = receipt_mod.append_jsonl_and_fsync

    def tracked_append(path, payload):
        resolved = Path(path).resolve()
        if resolved == history_abs:
            events.append("history_fsync")
        elif resolved == receipt_abs:
            events.append("receipt_fsync")
        else:
            events.append(f"other:{resolved}")
        return real_append(path, payload)

    def clock():
        events.append("clock")
        return READY

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", tracked_append)
    ref = WorldMacroStore(tmp_path, clock=clock).append_fact(fact)
    assert events == ["history_fsync", "clock", "receipt_fsync"]
    assert ref.receipt.ready_at == READY


def test_crash_between_history_and_receipt_is_unproven_until_idempotent_retry(
    tmp_path: Path, monkeypatch
) -> None:
    observation = _observation()
    history_abs = (tmp_path / "observations" / "2026-08-23.jsonl").resolve()
    receipt_abs = (tmp_path / "observations" / "availability_receipts" / "2026-08-23.jsonl").resolve()
    real_append = receipt_mod.append_jsonl_and_fsync

    def boom(path, payload):
        if Path(path).resolve() == receipt_abs:
            raise OSError("receipt fsync failed")
        return real_append(path, payload)

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", boom)
    clock_calls: list[str] = []
    writer = WorldMacroStore(tmp_path, clock=lambda: clock_calls.append("clock") or READY)
    history_before = None
    with pytest.raises(OSError, match="receipt"):
        writer.append_observation(observation)
    assert history_abs.is_file()
    history_before = history_abs.read_bytes()
    assert not receipt_abs.exists()
    reader = WorldMacroStore(tmp_path, clock=lambda: READY)
    assert reader.list_candidates_available_through(_scope(), CUTOFF) == ()

    retry_events: list[str] = []

    def tracked_append(path, payload):
        resolved = Path(path).resolve()
        retry_events.append("history_fsync" if resolved == history_abs else "receipt_fsync")
        return real_append(path, payload)

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", tracked_append)
    recovered = WorldMacroStore(tmp_path, clock=lambda: clock_calls.append("retry") or READY).append_observation(
        observation
    )
    assert retry_events == ["receipt_fsync"]
    assert history_abs.read_bytes() == history_before
    assert recovered.persisted.receipt.ready_at == READY
    assert recovered.persisted.receipt._store_attested is True
    assert len(_history_rows(history_abs)) == 1
    assert len(load_receipts(receipt_abs)) == 1
    listed = WorldMacroStore(tmp_path, clock=lambda: READY).list_candidates_available_through(_scope(), CUTOFF)
    assert len(listed) == 1
    assert listed[0].observation.observation_id == observation.observation_id


def test_tampered_receipt_is_excluded_from_pit_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observation = _observation()
    store.append_observation(observation)
    receipt_path = tmp_path / "observations" / "availability_receipts" / "2026-08-23.jsonl"
    history = tmp_path / "observations" / "2026-08-23.jsonl"
    original_receipt = receipt_path.read_text(encoding="utf-8")
    row = load_receipts(receipt_path)[-1]
    row["receipt_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    assert WorldMacroStore(tmp_path, clock=lambda: READY).list_candidates_available_through(_scope(), CUTOFF) == ()
    receipt_path.write_text(original_receipt, encoding="utf-8")
    payload = _history_rows(history)[-1]
    payload["features"]["macro_regime"] = "quiet"
    history.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    assert WorldMacroStore(tmp_path, clock=lambda: READY).list_candidates_available_through(_scope(), CUTOFF) == ()
    assert history.read_text(encoding="utf-8").strip()
    assert original_receipt == receipt_path.read_text(encoding="utf-8")


def test_list_candidates_is_exact_scope_and_keeps_explicit_composition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    country_fact = _fact()
    world_fact = _fact(
        fact_kind="market_benchmark",
        metric_key="brent",
        scope=_scope(kind="world", entity_id="market"),
        value=MacroNumericValue(number=80.0, unit="usd_per_barrel"),
        period="2026-08-22",
        source=_source(provider_id="market_benchmark", adapter_version="commodities.v1", source_record_id="BRN"),
    )
    store.append_fact(country_fact)
    store.append_fact(world_fact)
    venue_obs = _observation(
        fact=country_fact,
        fact_refs=(country_fact.fact_version_id.value, world_fact.fact_version_id.value),
        dimensions=(
            _dimension(dimension="macro_regime", value="mixed", fact_refs=(country_fact.fact_version_id.value,)),
            _dimension(dimension="rates_regime", value="stable", fact_refs=(country_fact.fact_version_id.value,)),
            _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
        ),
    )
    world_obs = _observation(
        fact=world_fact,
        scope=_scope(kind="world", entity_id="market"),
        fact_refs=(world_fact.fact_version_id.value,),
        dimensions=(
            _dimension(dimension="macro_regime", value="mixed", fact_refs=(world_fact.fact_version_id.value,)),
            _dimension(dimension="rates_regime", value="stable", fact_refs=(world_fact.fact_version_id.value,)),
            _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
        ),
    )
    store.append_observation(venue_obs)
    store.append_observation(world_obs)
    venue_candidates = store.list_candidates_available_through(_scope(), CUTOFF)
    world_candidates = store.list_candidates_available_through(_scope(kind="world", entity_id="market"), CUTOFF)
    country_candidates = store.list_candidates_available_through(
        _scope(kind="country", entity_id="iso-3166:US"), CUTOFF
    )
    assert [item.observation.observation_id for item in venue_candidates] == [venue_obs.observation_id]
    assert venue_candidates[0].observation.scope == _scope()
    assert venue_candidates[0].observation.fact_refs == tuple(
        sorted((country_fact.fact_version_id.value, world_fact.fact_version_id.value))
    )
    assert [item.observation.observation_id for item in world_candidates] == [world_obs.observation_id]
    assert country_candidates == ()


def test_random_append_order_does_not_change_canonical_observation(tmp_path: Path) -> None:
    first = _fact()
    second = _fact(
        source=_source(source_record_id="second-id"),
        metric_key="cpi",
        period="2026-07",
    )
    left = _store(tmp_path / "left")
    right = _store(tmp_path / "right")
    left.append_fact(first)
    left.append_fact(second)
    right.append_fact(second)
    right.append_fact(first)
    refs = tuple(sorted((first.fact_version_id.value, second.fact_version_id.value)))
    observation = _observation(
        fact=first,
        fact_refs=refs,
        dimensions=(
            _dimension(dimension="macro_regime", value="mixed", fact_refs=(first.fact_version_id.value,)),
            _dimension(dimension="rates_regime", value="stable", fact_refs=(first.fact_version_id.value,)),
            _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
        ),
    )
    left_env = left.append_observation(observation)
    right_env = right.append_observation(observation)
    assert left_env.observation.observation_id == right_env.observation.observation_id == observation.observation_id
    assert left_env.observation.content_sha256 == right_env.observation.content_sha256
    listed_left = left.list_candidates_available_through(_scope(), CUTOFF)
    listed_right = right.list_candidates_available_through(_scope(), CUTOFF)
    assert [item.observation.observation_id for item in listed_left] == [
        item.observation.observation_id for item in listed_right
    ]


def test_receipt_discovered_after_cutoff_keeps_conservative_first_seen(tmp_path: Path) -> None:
    observation = _observation()
    writer = WorldMacroStore(tmp_path, clock=lambda: READY)
    written = writer.append_observation(observation)
    assert written.evidence.first_seen_at == READY
    reader = WorldMacroStore(tmp_path, clock=lambda: BOOT)
    candidates = reader.list_candidates_available_through(_scope(), CUTOFF)
    assert len(candidates) == 1
    assert candidates[0].persisted.receipt.ready_at == READY
    assert candidates[0].evidence.first_seen_at == BOOT
    assert candidates[0].evidence.effective_ready_at == BOOT
    decision = PointInTimeEligibilityPolicy().evaluate(
        evidence=candidates[0].evidence,
        cutoff_at=CUTOFF,
        valid_until=observation.valid_until,
        version=observation.transform_version,
    )
    assert decision.status == "availability_unproven"


def test_restart_idempotent_append_keeps_unproven_first_seen(tmp_path: Path) -> None:
    observation = _observation()
    WorldMacroStore(tmp_path, clock=lambda: READY).append_observation(observation)
    restarted = WorldMacroStore(tmp_path, clock=lambda: BOOT)
    listed = restarted.list_candidates_available_through(_scope(), CUTOFF)
    assert len(listed) == 1
    assert listed[0].evidence.first_seen_at == BOOT
    assert listed[0].evidence.effective_ready_at == BOOT
    policy = PointInTimeEligibilityPolicy()
    listed_decision = policy.evaluate(
        evidence=listed[0].evidence,
        cutoff_at=CUTOFF,
        valid_until=observation.valid_until,
        version=observation.transform_version,
    )
    assert listed_decision.status == "availability_unproven"
    retried = restarted.append_observation(observation)
    assert retried.evidence.first_seen_at == listed[0].evidence.first_seen_at == BOOT
    assert retried.evidence.effective_ready_at == BOOT
    retried_decision = policy.evaluate(
        evidence=retried.evidence,
        cutoff_at=CUTOFF,
        valid_until=observation.valid_until,
        version=observation.transform_version,
    )
    assert retried_decision.status == "availability_unproven"


def test_restart_first_seen_ignores_mtime_and_does_not_mutate_history(tmp_path: Path) -> None:
    observation = _observation()
    writer = WorldMacroStore(tmp_path, clock=lambda: READY)
    writer.append_observation(observation)
    history = tmp_path / "observations" / "2026-08-23.jsonl"
    receipt = tmp_path / "observations" / "availability_receipts" / "2026-08-23.jsonl"
    before = history.read_bytes()
    receipt_before = receipt.read_bytes()
    old = OLD_MTIME.timestamp()
    os.utime(history, (old, old))
    os.utime(receipt, (old, old))
    restarted = WorldMacroStore(tmp_path, clock=lambda: BOOT)
    candidates = restarted.list_candidates_available_through(_scope(), CUTOFF)
    assert len(candidates) == 1
    assert candidates[0].evidence.first_seen_at == BOOT
    assert candidates[0].evidence.effective_ready_at == BOOT
    assert history.stat().st_mtime == old
    assert history.read_bytes() == before
    assert receipt.read_bytes() == receipt_before
    assert "st_mtime" not in Path(WorldMacroStore.__init__.__code__.co_filename).read_text(encoding="utf-8")


def test_append_event_round_trips_and_rehydrates_published_envelope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    fact = _fact()
    store.append_fact(fact)
    observation = _observation(fact=fact)
    envelope = store.append_observation(observation)
    registered = MacroCollectionRegistered(
        scope=_scope(),
        cutoff_at=CUTOFF,
        expected_source_ids=("fed_policy_rate", "brent"),
    )
    started = MacroCollectionStarted(run_id=registered.run_id)
    completed_source = MacroSourceCompleted(
        run_id=registered.run_id,
        source_id="fed_policy_rate",
        fact_version_ids=(fact.fact_version_id.value,),
    )
    failed_source = MacroSourceFailed(run_id=registered.run_id, source_id="brent", reason="missing")
    published = MacroObservationPublished(run_id=registered.run_id, envelope=envelope)
    completed = MacroCollectionCompleted(
        run_id=registered.run_id,
        status="completed_partial",
        observation_id=observation.observation_id,
    )
    refs = [
        store.append_event(registered),
        store.append_event(started),
        store.append_event(completed_source),
        store.append_event(failed_source),
        store.append_event(published),
        store.append_event(completed),
    ]
    assert all(isinstance(ref.identity, MacroCollectionEventId) for ref in refs)
    assert all(ref.receipt._store_attested for ref in refs)
    assert refs[0].receipt.scope == registered.run_id
    loaded = store.load(MacroCollectionRunId(registered.run_id))
    run = MacroCollectionRun.from_events(loaded)
    assert run.status == "completed_partial"
    assert run.published_envelope is not None
    assert run.published_envelope.observation.observation_id == observation.observation_id
    assert run.published_envelope.persisted.receipt.receipt_id == envelope.persisted.receipt.receipt_id
    replayed = store.append_event(registered)
    assert replayed.receipt.receipt_id == refs[0].receipt.receipt_id
    assert len(_history_rows(tmp_path / "runs" / "events" / "2026-08-23.jsonl")) == 6


def _publish_partial_run(store: WorldMacroStore, *, fact: MacroSourceFact, observation: MacroWorldObservation):
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
    store.append_event(MacroSourceFailed(run_id=registered.run_id, source_id="brent", reason="missing"))
    store.append_event(MacroObservationPublished(run_id=registered.run_id, envelope=envelope))
    store.append_event(
        MacroCollectionCompleted(
            run_id=registered.run_id,
            status="completed_partial",
            observation_id=observation.observation_id,
        )
    )
    return envelope, registered.run_id


def test_restart_load_of_published_run_uses_reader_first_seen_not_ready_at(tmp_path: Path) -> None:
    fact = _fact()
    observation = _observation(fact=fact)
    writer = WorldMacroStore(tmp_path, clock=lambda: READY)
    writer.append_fact(fact)
    written, run_id = _publish_partial_run(writer, fact=fact, observation=observation)
    assert written.evidence.first_seen_at == READY
    assert written.persisted.receipt.ready_at == READY
    same_process = MacroCollectionRun.from_events(writer.load(MacroCollectionRunId(run_id)))
    assert same_process.published_envelope is not None
    assert same_process.published_envelope.evidence.first_seen_at == READY
    assert same_process.published_envelope.evidence.effective_ready_at == READY
    assert same_process.published_envelope.persisted.receipt.ready_at == READY

    restarted = WorldMacroStore(tmp_path, clock=lambda: BOOT)
    loaded = MacroCollectionRun.from_events(restarted.load(MacroCollectionRunId(run_id)))
    assert loaded.published_envelope is not None
    assert loaded.published_envelope.persisted.receipt.ready_at == READY
    assert loaded.published_envelope.evidence.first_seen_at == BOOT
    assert loaded.published_envelope.evidence.effective_ready_at == BOOT
    listed = restarted.list_candidates_available_through(_scope(), CUTOFF)
    assert listed[0].evidence.first_seen_at == BOOT
    assert listed[0].evidence.effective_ready_at == BOOT


def test_event_conflict_and_unproven_event_are_fail_closed(tmp_path: Path, monkeypatch) -> None:
    conflict_root = tmp_path / "conflict"
    store = _store(conflict_root)
    registered = MacroCollectionRegistered(
        scope=_scope(),
        cutoff_at=CUTOFF,
        expected_source_ids=("fed_policy_rate",),
    )
    store.append_event(registered)
    history = conflict_root / "runs" / "events" / "2026-08-23.jsonl"
    rows = _history_rows(history)
    rows[0]["producer_version"] = "forged.v1"
    history.write_text(json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict|tamper"):
        store.append_event(registered)

    crash_root = tmp_path / "crash"
    live = _store(crash_root)
    live.append_event(registered)
    started = MacroCollectionStarted(run_id=registered.run_id)
    receipt_abs = (crash_root / "runs" / "availability_receipts" / "2026-08-23.jsonl").resolve()
    real_append = receipt_mod.append_jsonl_and_fsync

    def boom(path, payload):
        if Path(path).resolve() == receipt_abs:
            raise OSError("receipt fsync failed")
        return real_append(path, payload)

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", boom)
    crashed = WorldMacroStore(crash_root, clock=lambda: READY)
    with pytest.raises(OSError, match="receipt"):
        crashed.append_event(started)
    loaded = WorldMacroStore(crash_root, clock=lambda: READY).load(MacroCollectionRunId(registered.run_id))
    types = [event.event_type for event in loaded]
    assert "macro_collection_started" not in types
    assert types == ["macro_collection_registered"]


def test_reconstructible_status_is_not_pit_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_observation(_observation())
    status = tmp_path / "status.json"
    assert status.is_file()
    status.write_text("{not-json", encoding="utf-8")
    os.utime(status, (OLD_MTIME.timestamp(), OLD_MTIME.timestamp()))
    candidates = WorldMacroStore(tmp_path, clock=lambda: READY).list_candidates_available_through(_scope(), CUTOFF)
    assert len(candidates) == 1
    assert candidates[0].evidence.first_seen_at == READY
    assert AvailabilityEvidence(
        receipt=candidates[0].persisted.receipt,
        first_seen_at=candidates[0].evidence.first_seen_at,
    ).effective_ready_at == READY


def test_store_does_not_import_runtime_frontend_or_news_brief() -> None:
    source = Path(WorldMacroStore.__init__.__code__.co_filename).read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.reporting" not in source
    assert "trader.application" not in source
    assert "NewsMacroBrief" not in source
    assert "desktop" not in source
    assert "ready_at" not in inspect.signature(WorldAvailabilityJsonlReceiptStore.append).parameters
