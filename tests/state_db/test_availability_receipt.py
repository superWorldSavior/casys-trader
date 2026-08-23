from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.domain.world_availability import (
    PersistedWorldRef,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    world_subject_content_sha256,
)
from trader.infrastructure.state_db import availability_receipt as receipt_mod
from trader.infrastructure.state_db.availability_receipt import (
    AVAILABILITY_RECEIPT_SCHEMA,
    WorldAvailabilityJsonlReceiptStore,
    build_availability_receipt,
    load_receipts,
    parse_world_availability_receipt,
    payload_sha256,
    validate_availability_receipt,
)


PAYLOAD = {"brief_id": "brief-1", "venue": "XTAI"}
SUBJECT_PAYLOAD = {"observation_id": "macro_world_observation:v1:abc", "scope": "mic:XTAI"}
READY = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
STORE_READY = datetime(2026, 8, 23, 13, 0, tzinfo=timezone.utc)
HISTORY_REL = "facts/2026-08-23.jsonl"
RECEIPT_REL = "availability_receipts/2026-08-23.jsonl"
STORE_ID = "world-availability-jsonl.v1"


def _subject() -> WorldAvailabilitySubjectRef:
    return WorldAvailabilitySubjectRef(
        kind="macro_world_observation",
        subject_id="macro_world_observation:v1:abc",
        content_sha256=world_subject_content_sha256(SUBJECT_PAYLOAD),
    )


def _store(tmp_path, clock=None) -> WorldAvailabilityJsonlReceiptStore:
    return WorldAvailabilityJsonlReceiptStore(
        tmp_path,
        store_id=STORE_ID,
        clock=clock or (lambda: STORE_READY),
    )


def _receipt(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = build_availability_receipt(
        artifact_id="brief-1",
        artifact_ref={"brief_id": "brief-1"},
        payload=PAYLOAD,
        history_ref={"path": "2026-08-22.jsonl", "scope": "2026-08-22", "encoding": "jsonl"},
        ready_at=READY,
    )
    row.update(overrides)
    return row


def test_build_rejects_empty_and_mismatched_refs() -> None:
    with pytest.raises(ValueError, match="artifact_id"):
        build_availability_receipt(
            artifact_id=" ",
            artifact_ref={"brief_id": "brief-1"},
            payload=PAYLOAD,
            history_ref={"path": "2026-08-22.jsonl", "scope": "2026-08-22", "encoding": "jsonl"},
            ready_at=READY,
        )
    with pytest.raises(ValueError, match="artifact_ref|brief_id"):
        build_availability_receipt(
            artifact_id="brief-1",
            artifact_ref={},
            payload=PAYLOAD,
            history_ref={"path": "2026-08-22.jsonl", "scope": "2026-08-22", "encoding": "jsonl"},
            ready_at=READY,
        )
    with pytest.raises(ValueError, match="history_ref"):
        build_availability_receipt(
            artifact_id="brief-1",
            artifact_ref={"brief_id": "brief-1"},
            payload=PAYLOAD,
            history_ref={"path": "2026-08-22.jsonl", "scope": "2026-08-22", "encoding": "csv"},
            ready_at=READY,
        )


def test_validate_fails_closed_on_empty_wrong_and_malformed_refs() -> None:
    kwargs = {
        "expected_artifact_id": "brief-1",
        "expected_scope": "2026-08-22",
        "expected_history_path": "2026-08-22.jsonl",
    }
    assert validate_availability_receipt(_receipt(), PAYLOAD, **kwargs) == READY

    empty_refs = _receipt(artifact_ref={}, history_ref={})
    assert validate_availability_receipt(empty_refs, PAYLOAD, **kwargs) is None

    missing_path = _receipt()
    missing_path["history_ref"] = {"scope": "2026-08-22", "encoding": "jsonl"}
    assert validate_availability_receipt(missing_path, PAYLOAD, **kwargs) is None

    wrong_path = _receipt()
    wrong_path["history_ref"] = {"path": "other.jsonl", "scope": "2026-08-22", "encoding": "jsonl"}
    assert validate_availability_receipt(wrong_path, PAYLOAD, **kwargs) is None

    missing_scope = _receipt()
    missing_scope["history_ref"] = {"path": "2026-08-22.jsonl", "encoding": "jsonl"}
    assert validate_availability_receipt(missing_scope, PAYLOAD, **kwargs) is None

    wrong_scope = _receipt()
    wrong_scope["history_ref"] = {"path": "2026-08-22.jsonl", "scope": "other", "encoding": "jsonl"}
    assert validate_availability_receipt(wrong_scope, PAYLOAD, **kwargs) is None

    missing_encoding = _receipt()
    missing_encoding["history_ref"] = {"path": "2026-08-22.jsonl", "scope": "2026-08-22"}
    assert validate_availability_receipt(missing_encoding, PAYLOAD, **kwargs) is None

    missing_id = _receipt()
    missing_id["artifact_ref"] = {}
    assert validate_availability_receipt(missing_id, PAYLOAD, **kwargs) is None

    wrong_id = _receipt()
    wrong_id["artifact_ref"] = {"brief_id": "other"}
    assert validate_availability_receipt(wrong_id, PAYLOAD, **kwargs) is None

    digest_mismatch = _receipt(payload_sha256="0" * 64)
    assert validate_availability_receipt(digest_mismatch, PAYLOAD, **kwargs) is None
    assert digest_mismatch["payload_sha256"] != payload_sha256(PAYLOAD)

    naive = _receipt(ready_at="2026-08-22T09:00:00")
    assert validate_availability_receipt(naive, PAYLOAD, **kwargs) is None

    malformed = _receipt(ready_at="not-a-time")
    assert validate_availability_receipt(malformed, PAYLOAD, **kwargs) is None


def test_legacy_brief_id_receipts_remain_v1_and_are_not_world_evidence() -> None:
    row = _receipt()
    kwargs = {
        "expected_artifact_id": "brief-1",
        "expected_scope": "2026-08-22",
        "expected_history_path": "2026-08-22.jsonl",
    }
    assert row["schema_version"] == AVAILABILITY_RECEIPT_SCHEMA
    assert row["schema_version"] == "availability_receipt.v1"
    assert row["artifact_ref"]["brief_id"] == "brief-1"
    assert validate_availability_receipt(row, PAYLOAD, **kwargs) == READY
    assert parse_world_availability_receipt(row) is None
    assert parse_world_availability_receipt(row, payload=PAYLOAD) is None
    assert parse_world_availability_receipt(_receipt(payload={"nested": True}), payload=PAYLOAD) is None


def test_store_stamp_assigns_ready_at_from_clock(tmp_path) -> None:
    ref = _store(tmp_path).append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    receipt = ref.receipt
    assert isinstance(ref, PersistedWorldRef)
    assert receipt.ready_at == STORE_READY
    assert receipt.schema_version == "availability_receipt.v2"
    assert receipt.storage_locator == WorldStorageLocator(kind="jsonl", store_id=STORE_ID, path=HISTORY_REL)
    assert receipt._store_attested is True
    assert receipt.receipt_id
    assert receipt.receipt_sha256
    parsed = parse_world_availability_receipt(receipt.to_dict())
    assert parsed == receipt
    assert parse_world_availability_receipt({**receipt.to_dict(), "receipt_sha256": "0" * 64}) is None
    missing_hash = receipt.to_dict()
    missing_hash.pop("receipt_sha256")
    assert parse_world_availability_receipt(missing_hash) is None
    missing_id = receipt.to_dict()
    missing_id.pop("receipt_id")
    assert parse_world_availability_receipt(missing_id) is None
    forged_time = receipt.to_dict()
    forged_time["ready_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    assert parse_world_availability_receipt(forged_time) is None


def test_world_parse_never_promotes_v1_or_forged_legacy_rows() -> None:
    kwargs = {
        "expected_artifact_id": "brief-1",
        "expected_scope": "2026-08-22",
        "expected_history_path": "2026-08-22.jsonl",
    }
    assert parse_world_availability_receipt(_receipt(), payload=PAYLOAD) is None
    forged_id = _receipt(artifact_id="forged", artifact_ref={"brief_id": "forged"})
    assert parse_world_availability_receipt(forged_id, payload=PAYLOAD) is None
    assert validate_availability_receipt(forged_id, PAYLOAD, **kwargs) is None
    forged_scope = _receipt()
    forged_scope["history_ref"] = {"path": "2026-08-22.jsonl", "scope": "forged", "encoding": "jsonl"}
    assert parse_world_availability_receipt(forged_scope, payload=PAYLOAD) is None
    assert validate_availability_receipt(forged_scope, PAYLOAD, **kwargs) is None
    forged_path = _receipt()
    forged_path["history_ref"] = {"path": "forged.jsonl", "scope": "2026-08-22", "encoding": "jsonl"}
    assert parse_world_availability_receipt(forged_path, payload=PAYLOAD) is None
    assert validate_availability_receipt(forged_path, PAYLOAD, **kwargs) is None
    forged_ready = _receipt(ready_at="1999-01-01T00:00:00+00:00")
    assert parse_world_availability_receipt(forged_ready, payload=PAYLOAD) is None
    forged_digest = _receipt(payload_sha256="0" * 64)
    assert parse_world_availability_receipt(forged_digest, payload=PAYLOAD) is None
    assert validate_availability_receipt(forged_digest, PAYLOAD, **kwargs) is None


def test_parse_fails_closed_on_non_mapping_nested_subject(tmp_path) -> None:
    receipt = _store(tmp_path).append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    ).receipt
    row = receipt.to_dict()
    row.pop("subject_kind")
    row.pop("subject_id")
    row["subject"] = "not-a-mapping"
    with pytest.raises((TypeError, ValueError)):
        WorldAvailabilityReceipt.from_mapping(row)
    assert parse_world_availability_receipt(row) is None
    assert parse_world_availability_receipt({**row, "subject": ["nested"]}) is None


def test_public_surface_does_not_export_a_ready_at_minter() -> None:
    assert "stamp_world_availability_receipt" not in receipt_mod.__all__
    assert "DurableWorldAvailabilityReceiptAdapter" not in receipt_mod.__all__
    assert not hasattr(receipt_mod, "stamp_world_availability_receipt")
    assert not hasattr(receipt_mod, "DurableWorldAvailabilityReceiptAdapter")
    append = inspect.signature(WorldAvailabilityJsonlReceiptStore.append)
    init = inspect.signature(WorldAvailabilityJsonlReceiptStore.__init__)
    assert "ready_at" not in append.parameters
    assert "clock" not in append.parameters
    assert "storage_locator" not in append.parameters
    assert "store_id" in init.parameters
    assert "_seal_world_availability_receipt" not in receipt_mod.__all__
    assert "_attest_verified_store_receipt" not in receipt_mod.__all__


def test_jsonl_store_fsyncs_history_then_clock_then_receipt(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    history_abs = (tmp_path / HISTORY_REL).resolve()
    receipt_abs = (tmp_path / RECEIPT_REL).resolve()
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
        return STORE_READY

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", tracked_append)
    ref = WorldAvailabilityJsonlReceiptStore(tmp_path, store_id=STORE_ID, clock=clock).append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    assert events == ["history_fsync", "clock", "receipt_fsync"]
    assert history_abs.is_file()
    assert receipt_abs.is_file()
    assert world_subject_content_sha256(load_receipts(history_abs)[-1]) == _subject().content_sha256
    assert parse_world_availability_receipt(load_receipts(receipt_abs)[-1]) == ref.receipt
    assert ref.receipt.ready_at == STORE_READY
    assert ref.receipt.storage_locator.path == HISTORY_REL
    assert ref.identity == _subject()


def test_history_fsync_failure_never_calls_clock_or_writes_receipt(tmp_path, monkeypatch) -> None:
    clock_calls: list[str] = []
    history_abs = (tmp_path / HISTORY_REL).resolve()
    receipt_abs = (tmp_path / RECEIPT_REL).resolve()

    def boom(path, payload):
        raise OSError("history fsync failed")

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", boom)
    with pytest.raises(OSError, match="history"):
        WorldAvailabilityJsonlReceiptStore(
            tmp_path,
            store_id=STORE_ID,
            clock=lambda: clock_calls.append("clock") or STORE_READY,
        ).append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path=HISTORY_REL,
            receipt_path=RECEIPT_REL,
        )
    assert clock_calls == []
    assert not receipt_abs.exists()
    assert not history_abs.exists() or history_abs.read_text(encoding="utf-8") == ""


def test_receipt_fsync_failure_returns_no_persisted_ref(tmp_path, monkeypatch) -> None:
    history_abs = (tmp_path / HISTORY_REL).resolve()
    receipt_abs = (tmp_path / RECEIPT_REL).resolve()
    real_append = receipt_mod.append_jsonl_and_fsync

    def boom(path, payload):
        if Path(path).resolve() == receipt_abs:
            raise OSError("receipt fsync failed")
        return real_append(path, payload)

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", boom)
    result = None
    with pytest.raises(OSError, match="receipt"):
        result = WorldAvailabilityJsonlReceiptStore(tmp_path, store_id=STORE_ID, clock=lambda: STORE_READY).append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path=HISTORY_REL,
            receipt_path=RECEIPT_REL,
        )
    assert result is None
    assert history_abs.is_file()
    assert world_subject_content_sha256(load_receipts(history_abs)[-1]) == _subject().content_sha256
    assert not receipt_abs.exists()


def test_subject_digest_mismatch_fails_before_write(tmp_path) -> None:
    clock_calls: list[str] = []
    history_abs = tmp_path / HISTORY_REL
    receipt_abs = tmp_path / RECEIPT_REL
    mismatched = WorldAvailabilitySubjectRef(
        kind="macro_world_observation",
        subject_id="macro_world_observation:v1:abc",
        content_sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="content_sha256"):
        WorldAvailabilityJsonlReceiptStore(
            tmp_path,
            store_id=STORE_ID,
            clock=lambda: clock_calls.append("clock") or STORE_READY,
        ).append(
            mismatched,
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path=HISTORY_REL,
            receipt_path=RECEIPT_REL,
        )
    assert clock_calls == []
    assert not history_abs.exists()
    assert not receipt_abs.exists()


def test_store_rejects_absolute_and_parent_traversal_paths(tmp_path) -> None:
    store = _store(tmp_path)
    subject = _subject()
    kwargs = {"scope": "mic:XTAI", "payload": SUBJECT_PAYLOAD}
    with pytest.raises(ValueError, match="history_path"):
        store.append(subject, history_path="/tmp/escape.jsonl", receipt_path=RECEIPT_REL, **kwargs)
    with pytest.raises(ValueError, match="history_path"):
        store.append(subject, history_path="../escape.jsonl", receipt_path=RECEIPT_REL, **kwargs)
    with pytest.raises(ValueError, match="history_path"):
        store.append(subject, history_path="facts/../../escape.jsonl", receipt_path=RECEIPT_REL, **kwargs)
    with pytest.raises(ValueError, match="receipt_path"):
        store.append(subject, history_path=HISTORY_REL, receipt_path="/tmp/escape.jsonl", **kwargs)
    with pytest.raises(ValueError, match="receipt_path"):
        store.append(subject, history_path=HISTORY_REL, receipt_path="../escape.jsonl", **kwargs)
    with pytest.raises(ValueError, match="distinct"):
        store.append(subject, history_path=HISTORY_REL, receipt_path=HISTORY_REL, **kwargs)
    assert not any(tmp_path.rglob("escape.jsonl"))


def test_append_is_idempotent_on_natural_key(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    real_append = receipt_mod.append_jsonl_and_fsync

    def tracked_append(path, payload):
        events.append(Path(path).name)
        return real_append(path, payload)

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", tracked_append)
    store = _store(tmp_path)
    first = store.append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    second = store.append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    assert events == ["2026-08-23.jsonl", "2026-08-23.jsonl"]
    assert first.receipt.ready_at == second.receipt.ready_at == STORE_READY
    assert first.receipt.receipt_id == second.receipt.receipt_id
    assert first.receipt.receipt_sha256 == second.receipt.receipt_sha256
    assert len(load_receipts(tmp_path / HISTORY_REL)) == 1
    assert len(load_receipts(tmp_path / RECEIPT_REL)) == 1


def test_retry_completes_missing_receipt_without_rewriting_history(tmp_path, monkeypatch) -> None:
    history_abs = (tmp_path / HISTORY_REL).resolve()
    receipt_abs = (tmp_path / RECEIPT_REL).resolve()
    real_append = receipt_mod.append_jsonl_and_fsync
    clock_calls: list[str] = []

    def boom(path, payload):
        if Path(path).resolve() == receipt_abs:
            raise OSError("receipt fsync failed")
        return real_append(path, payload)

    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", boom)
    with pytest.raises(OSError, match="receipt"):
        WorldAvailabilityJsonlReceiptStore(
            tmp_path,
            store_id=STORE_ID,
            clock=lambda: clock_calls.append("clock") or STORE_READY,
        ).append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path=HISTORY_REL,
            receipt_path=RECEIPT_REL,
        )
    assert clock_calls == ["clock"]
    assert history_abs.is_file()
    assert not receipt_abs.exists()

    events: list[str] = []

    def tracked_append(path, payload):
        resolved = Path(path).resolve()
        events.append("history_fsync" if resolved == history_abs else "receipt_fsync")
        return real_append(path, payload)

    retry_ready = datetime(2026, 8, 23, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(receipt_mod, "append_jsonl_and_fsync", tracked_append)
    recovered = WorldAvailabilityJsonlReceiptStore(
        tmp_path,
        store_id=STORE_ID,
        clock=lambda: clock_calls.append("retry") or retry_ready,
    ).append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    assert events == ["receipt_fsync"]
    assert clock_calls == ["clock", "retry"]
    assert recovered.receipt.ready_at == retry_ready
    assert len(load_receipts(history_abs)) == 1
    assert len(load_receipts(receipt_abs)) == 1


def test_natural_key_conflict_on_incompatible_scope_or_locator(tmp_path) -> None:
    store = _store(tmp_path)
    store.append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    with pytest.raises(ValueError, match="conflict"):
        store.append(
            _subject(),
            scope="iso-3166:TW",
            payload=SUBJECT_PAYLOAD,
            history_path=HISTORY_REL,
            receipt_path=RECEIPT_REL,
        )
    with pytest.raises(ValueError, match="conflict"):
        store.append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path="facts/other.jsonl",
            receipt_path=RECEIPT_REL,
        )
    assert len(load_receipts(tmp_path / HISTORY_REL)) == 1
    assert len(load_receipts(tmp_path / RECEIPT_REL)) == 1


def _reseal_receipt(receipt: WorldAvailabilityReceipt, *, ready_at: datetime) -> dict[str, object]:
    from trader.domain.world_availability import _iso, _receipt_hash_payload, _receipt_id_for, _receipt_identity_payload
    from trader.domain.world_episode import canonical_sha256

    identity = _receipt_identity_payload(
        schema_version=receipt.schema_version,
        subject=receipt.subject,
        scope=receipt.scope,
        storage_locator=receipt.storage_locator,
    )
    receipt_id = _receipt_id_for(identity)
    return {
        **identity,
        "receipt_id": receipt_id,
        "ready_at": _iso(ready_at),
        "receipt_sha256": canonical_sha256(
            _receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=ready_at)
        ),
    }


def test_mapping_content_payload_round_trips_through_v2_store(tmp_path) -> None:
    from trader.domain.world_scope import (
        WorldCanonicalScopeRef,
        WorldMarketAnchorRef,
        WorldScopeMapping,
        WorldScopeMappingEntry,
    )

    mapping = WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="TW", instrument="2330"),
                venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XTAI"),
                country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:TW"),
                region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:030"),
                world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
                provider_proofs=("provider:listing",),
                taxonomy_version="geo.v1",
            ),
        ),
    )
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="content_sha256"):
        store.append(
            mapping.subject_ref(),
            scope="world:market",
            payload=mapping.to_dict(),
            history_path=HISTORY_REL,
            receipt_path=RECEIPT_REL,
        )
    ref = store.append(
        mapping.subject_ref(),
        scope="world:market",
        payload=mapping.content_payload(),
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    assert ref.identity == mapping.subject_ref()
    assert world_subject_content_sha256(mapping.content_payload()) == mapping.content_sha256
    assert parse_world_availability_receipt(load_receipts(tmp_path / RECEIPT_REL)[-1]) == ref.receipt


def test_receipt_flush_then_fsync_failure_retries_with_current_fsync(tmp_path, monkeypatch) -> None:
    history_abs = (tmp_path / HISTORY_REL).resolve()
    receipt_abs = (tmp_path / RECEIPT_REL).resolve()
    real_fsync = receipt_mod.fsync_path
    events: list[str] = []
    fail_receipt = {"once": True}

    def boom(path):
        resolved = Path(path).resolve()
        if resolved == receipt_abs:
            events.append("receipt_fsync")
            if fail_receipt["once"]:
                fail_receipt["once"] = False
                raise OSError("receipt fsync failed")
            real_fsync(path)
            events.append("receipt_fsync_ok")
            return
        events.append("history_fsync")
        return real_fsync(path)

    monkeypatch.setattr(receipt_mod, "fsync_path", boom)
    clock_calls: list[str] = []
    store = WorldAvailabilityJsonlReceiptStore(
        tmp_path,
        store_id=STORE_ID,
        clock=lambda: clock_calls.append("clock") or STORE_READY,
    )
    with pytest.raises(OSError, match="receipt"):
        store.append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path=HISTORY_REL,
            receipt_path=RECEIPT_REL,
        )
    assert clock_calls == ["clock"]
    assert receipt_abs.is_file()
    assert receipt_abs.read_text(encoding="utf-8").strip()
    assert "receipt_fsync_ok" not in events

    recovered = store.append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    assert clock_calls == ["clock"]
    assert events[-2:] == ["receipt_fsync", "receipt_fsync_ok"]
    assert recovered.receipt.ready_at == STORE_READY
    assert recovered.receipt._store_attested is True
    assert len(load_receipts(receipt_abs)) == 1
    assert history_abs.is_file()


def test_store_rejects_symlink_aliased_history_and_receipt_paths(tmp_path) -> None:
    facts = tmp_path / "facts"
    facts.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(facts, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported")
    with pytest.raises(ValueError, match="distinct"):
        _store(tmp_path).append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path="facts/2026-08-23.jsonl",
            receipt_path="alias/2026-08-23.jsonl",
        )
    assert not any(tmp_path.rglob("2026-08-23.jsonl"))


def test_natural_key_conflicts_across_receipt_files(tmp_path) -> None:
    store = _store(tmp_path)
    store.append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path="availability_receipts/one.jsonl",
    )
    with pytest.raises(ValueError, match="conflict"):
        store.append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path="facts/other.jsonl",
            receipt_path="availability_receipts/two.jsonl",
        )
    assert len(load_receipts(tmp_path / "availability_receipts/one.jsonl")) == 1
    assert not (tmp_path / "availability_receipts/two.jsonl").exists()


def test_duplicate_valid_receipt_rows_fail_closed(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.append(
        _subject(),
        scope="mic:XTAI",
        payload=SUBJECT_PAYLOAD,
        history_path=HISTORY_REL,
        receipt_path=RECEIPT_REL,
    )
    later = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)
    receipt_mod.append_jsonl_and_fsync(
        tmp_path / "availability_receipts/other.jsonl",
        _reseal_receipt(first.receipt, ready_at=later),
    )
    with pytest.raises(ValueError, match="multiple"):
        store.append(
            _subject(),
            scope="mic:XTAI",
            payload=SUBJECT_PAYLOAD,
            history_path=HISTORY_REL,
            receipt_path="availability_receipts/other.jsonl",
        )
