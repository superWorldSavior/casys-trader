from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.infrastructure.state_db.availability_receipt import (
    build_availability_receipt,
    payload_sha256,
    validate_availability_receipt,
)


PAYLOAD = {"brief_id": "brief-1", "venue": "XTAI"}
READY = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)


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
