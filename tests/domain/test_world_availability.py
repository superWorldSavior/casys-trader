from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
    WORLD_AVAILABILITY_RECEIPT_SCHEMA,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
    _iso,
    _receipt_hash_payload,
    _receipt_id_for,
    _receipt_identity_payload,
)
from trader.domain.world_episode import canonical_sha256


UTC = timezone.utc
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 8, 23, 12, 5, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
CONTENT = "a" * 64
STORE_ID = "world-availability-jsonl.v1"


def _subject(**overrides: object) -> WorldAvailabilitySubjectRef:
    values: dict[str, object] = {
        "kind": "artifact",
        "subject_id": "macro_source_fact_version:v1:abc",
        "content_sha256": CONTENT,
    }
    values.update(overrides)
    return WorldAvailabilitySubjectRef(**values)  # type: ignore[arg-type]


def _locator() -> WorldStorageLocator:
    return WorldStorageLocator(kind="jsonl", store_id=STORE_ID, path="facts/2026-08-23.jsonl")


def _attested(
    *,
    ready: datetime = READY,
    subject: WorldAvailabilitySubjectRef | None = None,
    scope: str = "iso-3166:US",
    storage_locator: WorldStorageLocator | None = None,
) -> WorldAvailabilityReceipt:
    receipt = _stamp(ready=ready, subject=subject, scope=scope, storage_locator=storage_locator)
    return _attest_verified_store_receipt(
        receipt,
        expected_subject=receipt.subject,
        expected_scope=receipt.scope,
        expected_locator=receipt.storage_locator,
    )


def _stamp(
    *,
    ready: datetime = READY,
    subject: WorldAvailabilitySubjectRef | None = None,
    scope: str = "iso-3166:US",
    storage_locator: WorldStorageLocator | None = None,
) -> WorldAvailabilityReceipt:
    resolved_subject = subject or _subject()
    locator = storage_locator or _locator()
    identity = _receipt_identity_payload(
        schema_version=WORLD_AVAILABILITY_RECEIPT_SCHEMA,
        subject=resolved_subject,
        scope=scope,
        storage_locator=locator,
    )
    receipt_id = _receipt_id_for(identity)
    digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=ready))
    return WorldAvailabilityReceipt.from_mapping(
        {
            **identity,
            "receipt_id": receipt_id,
            "ready_at": _iso(ready),
            "receipt_sha256": digest,
        }
    )


def test_public_constructors_do_not_accept_caller_ready_at() -> None:
    for target in (
        WorldAvailabilityReceipt,
        WorldAvailabilitySubjectRef,
        WorldStorageLocator,
        AvailabilityEvidence,
        PersistedWorldRef,
        PointInTimeEligibilityPolicy,
        WorldAvailabilityReceipt.from_mapping,
        _attest_verified_store_receipt,
    ):
        assert "ready_at" not in inspect.signature(target).parameters

    evidence_params = inspect.signature(AvailabilityEvidence).parameters
    assert "effective_ready_at" not in evidence_params

    evaluate_params = inspect.signature(PointInTimeEligibilityPolicy.evaluate).parameters
    assert "ready_at" not in evaluate_params
    assert "as_of" not in evaluate_params
    assert "mtime" not in evaluate_params


def test_from_mapping_rehydrates_sealed_rows_and_rejects_forged_ready_at() -> None:
    sealed = _stamp().to_dict()
    replayed = WorldAvailabilityReceipt.from_mapping(sealed)
    assert replayed.ready_at == READY
    assert replayed.receipt_id == sealed["receipt_id"]
    assert replayed.receipt_sha256 == sealed["receipt_sha256"]

    missing_ready = dict(sealed)
    missing_ready.pop("ready_at")
    with pytest.raises(ValueError, match="ready_at"):
        WorldAvailabilityReceipt.from_mapping(missing_ready)

    missing_id = dict(sealed)
    missing_id.pop("receipt_id")
    with pytest.raises(ValueError, match="receipt_id"):
        WorldAvailabilityReceipt.from_mapping(missing_id)

    missing_hash = dict(sealed)
    missing_hash.pop("receipt_sha256")
    with pytest.raises(ValueError, match="receipt_sha256"):
        WorldAvailabilityReceipt.from_mapping(missing_hash)

    with pytest.raises(ValueError, match="timezone"):
        WorldAvailabilityReceipt.from_mapping({**sealed, "ready_at": "2026-08-23T12:00:00"})

    forged_time = dict(sealed)
    forged_time["ready_at"] = datetime(2026, 8, 23, 11, 0, tzinfo=UTC).isoformat()
    with pytest.raises(ValueError, match="receipt_sha256"):
        WorldAvailabilityReceipt.from_mapping(forged_time)


def test_unsigned_or_rehydrated_receipt_cannot_enter_persisted_ref_or_evidence() -> None:
    unsigned = WorldAvailabilityReceipt(
        receipt_id="unsigned",
        subject=_subject(),
        scope="iso-3166:US",
        storage_locator=_locator(),
        content_sha256=CONTENT,
        schema_version=WORLD_AVAILABILITY_RECEIPT_SCHEMA,
    )
    assert unsigned.ready_at is None
    with pytest.raises(ValueError, match="store-attested"):
        PersistedWorldRef(identity="macro_source_fact_version:v1:abc", receipt=unsigned)
    with pytest.raises(ValueError, match="store-attested"):
        AvailabilityEvidence(receipt=unsigned, first_seen_at=FIRST_SEEN)

    record = _stamp()
    assert record._store_attested is False
    with pytest.raises(ValueError, match="store-attested"):
        PersistedWorldRef(identity="macro_source_fact_version:v1:abc", receipt=record)
    with pytest.raises(ValueError, match="store-attested"):
        AvailabilityEvidence(receipt=record, first_seen_at=FIRST_SEEN)
    assert PointInTimeEligibilityPolicy().evaluate(evidence=None, cutoff_at=CUTOFF).status == "availability_unproven"


def test_receipt_round_trip_and_identity_ignore_ready_at_for_retry() -> None:
    first = _stamp(ready=READY)
    later = _stamp(ready=datetime(2026, 8, 23, 12, 30, tzinfo=UTC))
    assert first.receipt_id == later.receipt_id
    assert first.receipt_sha256 != later.receipt_sha256
    assert first.to_dict()["ready_at"] == READY.isoformat()
    replayed = WorldAvailabilityReceipt.from_mapping(first.to_dict())
    assert replayed == first
    assert replayed.receipt_sha256 == first.receipt_sha256


def test_receipt_hash_mismatch_and_locator_kinds_fail_closed() -> None:
    sealed = _stamp().to_dict()
    with pytest.raises(ValueError, match="receipt_sha256"):
        WorldAvailabilityReceipt.from_mapping({**sealed, "receipt_sha256": "0" * 64})

    sqlite = _stamp(
        storage_locator=WorldStorageLocator(
            kind="sqlite",
            store_id="world-model.db.v1",
            table="world_availability_receipts",
            row_id="row-1",
        )
    )
    assert sqlite.storage_locator.kind == "sqlite"
    assert sqlite.storage_locator.store_id == "world-model.db.v1"
    assert sqlite.storage_locator.table == "world_availability_receipts"
    assert sqlite.storage_locator.row_id == "row-1"

    with pytest.raises((TypeError, ValueError), match="store_id"):
        WorldStorageLocator(kind="jsonl", path="facts/2026-08-23.jsonl")
    with pytest.raises(ValueError, match="store_id"):
        WorldStorageLocator(kind="jsonl", store_id=" ", path="facts/2026-08-23.jsonl")
    with pytest.raises(ValueError, match="jsonl"):
        WorldStorageLocator(
            kind="jsonl",
            store_id=STORE_ID,
            path="facts/2026-08-23.jsonl",
            table="world_availability_receipts",
        )
    with pytest.raises(ValueError, match="sqlite"):
        WorldStorageLocator(kind="sqlite", store_id="world-model.db.v1", table="world_availability_receipts")
    with pytest.raises(ValueError, match="kind"):
        WorldStorageLocator(kind="mtime", store_id=STORE_ID)


def test_parsers_and_attestation_accept_only_current_world_receipt_schema() -> None:
    sealed = _stamp()
    assert sealed.schema_version == WORLD_AVAILABILITY_RECEIPT_SCHEMA == "world_availability_receipt.v1"
    attested = _attest_verified_store_receipt(
        sealed,
        expected_subject=sealed.subject,
        expected_scope=sealed.scope,
        expected_locator=sealed.storage_locator,
    )
    assert attested.schema_version == WORLD_AVAILABILITY_RECEIPT_SCHEMA
    for old in ("availability_receipt.v1", "availability_receipt.v2"):
        with pytest.raises(ValueError, match="schema_version"):
            WorldAvailabilityReceipt(
                receipt_id="unsigned",
                subject=_subject(),
                scope="iso-3166:US",
                storage_locator=_locator(),
                content_sha256=CONTENT,
                schema_version=old,
            )
        identity = _receipt_identity_payload(
            schema_version=old,
            subject=_subject(),
            scope="iso-3166:US",
            storage_locator=_locator(),
        )
        receipt_id = _receipt_id_for(identity)
        digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=READY))
        with pytest.raises(ValueError, match="schema_version"):
            WorldAvailabilityReceipt.from_mapping(
                {
                    **identity,
                    "receipt_id": receipt_id,
                    "ready_at": _iso(READY),
                    "receipt_sha256": digest,
                }
            )
        forged = _stamp()
        object.__setattr__(forged, "schema_version", old)
        with pytest.raises(ValueError, match=WORLD_AVAILABILITY_RECEIPT_SCHEMA):
            _attest_verified_store_receipt(
                forged,
                expected_subject=forged.subject,
                expected_scope=forged.scope,
                expected_locator=forged.storage_locator,
            )


def test_receipt_is_frozen() -> None:
    receipt = _stamp()
    with pytest.raises(FrozenInstanceError):
        receipt.scope = "other"  # type: ignore[misc]


def test_evidence_uses_max_ready_at_and_first_seen() -> None:
    receipt = _attested()
    late_seen = AvailabilityEvidence(receipt=receipt, first_seen_at=FIRST_SEEN)
    assert late_seen.effective_ready_at == FIRST_SEEN
    early_seen = AvailabilityEvidence(receipt=receipt, first_seen_at=datetime(2026, 8, 23, 11, 0, tzinfo=UTC))
    assert early_seen.effective_ready_at == READY
    with pytest.raises(ValueError, match="timezone"):
        AvailabilityEvidence(receipt=receipt, first_seen_at=datetime(2026, 8, 23, 12, 5))


def test_point_in_time_policy_eligibility_boundaries() -> None:
    policy = PointInTimeEligibilityPolicy()
    evidence = AvailabilityEvidence(receipt=_attested(), first_seen_at=FIRST_SEEN)

    eligible = policy.evaluate(evidence=evidence, cutoff_at=CUTOFF, valid_until=VALID_UNTIL)
    assert eligible.status == "eligible"
    assert eligible.effective_ready_at == FIRST_SEEN
    assert policy.is_eligible(evidence=evidence, cutoff_at=CUTOFF, valid_until=VALID_UNTIL) is True

    at_cutoff = AvailabilityEvidence(
        receipt=_attested(ready=CUTOFF),
        first_seen_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )
    assert at_cutoff.effective_ready_at == CUTOFF
    assert policy.evaluate(evidence=at_cutoff, cutoff_at=CUTOFF, valid_until=VALID_UNTIL).status == "eligible"
    assert policy.is_eligible(evidence=at_cutoff, cutoff_at=CUTOFF, valid_until=VALID_UNTIL) is True

    seen_at_cutoff = AvailabilityEvidence(receipt=_attested(), first_seen_at=CUTOFF)
    assert seen_at_cutoff.effective_ready_at == CUTOFF
    assert policy.is_eligible(evidence=seen_at_cutoff, cutoff_at=CUTOFF, valid_until=VALID_UNTIL) is True

    at_expiry = policy.evaluate(evidence=evidence, cutoff_at=VALID_UNTIL, valid_until=VALID_UNTIL)
    assert at_expiry.status == "stale"
    assert policy.is_eligible(evidence=evidence, cutoff_at=VALID_UNTIL, valid_until=VALID_UNTIL) is False

    after_cutoff_seen = AvailabilityEvidence(
        receipt=_attested(),
        first_seen_at=datetime(2026, 8, 23, 13, 5, tzinfo=UTC),
    )
    late = policy.evaluate(evidence=after_cutoff_seen, cutoff_at=CUTOFF, valid_until=VALID_UNTIL)
    assert late.status == "availability_unproven"

    future_ready = AvailabilityEvidence(
        receipt=_attested(ready=datetime(2026, 8, 23, 14, 0, tzinfo=UTC)),
        first_seen_at=datetime(2026, 8, 23, 14, 0, tzinfo=UTC),
    )
    assert policy.evaluate(evidence=future_ready, cutoff_at=CUTOFF).status == "availability_unproven"
    assert policy.evaluate(evidence=None, cutoff_at=CUTOFF).status == "availability_unproven"

    superseded = policy.evaluate(evidence=evidence, cutoff_at=CUTOFF, superseded=True)
    assert superseded.status == "superseded"


def test_admitted_versions_are_enforced_without_forging_ready_at() -> None:
    receipt = _attested(ready=READY)
    forged = receipt.to_dict()
    forged["ready_at"] = datetime(2026, 8, 23, 11, 0, tzinfo=UTC).isoformat()
    with pytest.raises(ValueError, match="receipt_sha256"):
        WorldAvailabilityReceipt.from_mapping(forged)
    assert receipt.ready_at == READY

    policy = PointInTimeEligibilityPolicy(admitted_versions=frozenset({"admitted.v1"}))
    evidence = AvailabilityEvidence(receipt=receipt, first_seen_at=FIRST_SEEN)
    denied = policy.evaluate(
        evidence=evidence,
        cutoff_at=CUTOFF,
        version="admitted.v0",
    )
    assert denied.status == "availability_unproven"
    admitted = policy.evaluate(
        evidence=evidence,
        cutoff_at=CUTOFF,
        version="admitted.v1",
    )
    assert admitted.status == "eligible"


def test_persisted_ref_keeps_identity_and_stamped_receipt() -> None:
    receipt = _attested()
    ref: PersistedWorldRef[str] = PersistedWorldRef(
        identity="macro_source_fact_version:v1:abc",
        receipt=receipt,
    )
    assert ref.identity == "macro_source_fact_version:v1:abc"
    assert ref.receipt.ready_at == READY
    with pytest.raises(FrozenInstanceError):
        ref.identity = "other"  # type: ignore[misc]


def test_from_mapping_fails_closed_on_non_mapping_nested_subject() -> None:
    sealed = _stamp().to_dict()
    sealed.pop("subject_kind")
    sealed.pop("subject_id")
    for bad_subject in ("not-a-mapping", ["nested"], 1):
        row = dict(sealed)
        row["subject"] = bad_subject
        with pytest.raises((TypeError, ValueError)):
            WorldAvailabilityReceipt.from_mapping(row)


def test_receipt_retry_identity_is_stable_for_the_subject_triple() -> None:
    first = _stamp(ready=READY)
    retry = _stamp(ready=datetime(1999, 1, 1, tzinfo=UTC))
    assert first.subject.kind == retry.subject.kind
    assert first.subject.subject_id == retry.subject.subject_id
    assert first.subject.content_sha256 == retry.subject.content_sha256
    assert first.receipt_id == retry.receipt_id
    assert first.receipt_sha256 != retry.receipt_sha256


def test_point_in_time_policy_rejects_forged_duck_typed_evidence() -> None:
    policy = PointInTimeEligibilityPolicy()
    forged = SimpleNamespace(
        receipt=SimpleNamespace(_store_attested=True, ready_at=READY),
        effective_ready_at=READY,
    )
    with pytest.raises(TypeError, match="AvailabilityEvidence"):
        policy.evaluate(evidence=forged, cutoff_at=CUTOFF)
    assert policy.is_eligible(evidence=None, cutoff_at=CUTOFF) is False
