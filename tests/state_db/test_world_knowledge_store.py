from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.domain.world_company import DriverCompanyBundle
from trader.domain.world_context import EntityRef, KnowledgeArtifact
from trader.domain.world_knowledge import (
    KNOWLEDGE_ARTIFACT_SUBJECT_KIND,
    knowledge_artifact_content_sha256,
    knowledge_artifact_row,
)
from trader.domain.world_news import DriverNewsBundle
from trader.infrastructure.state_db.world_knowledge_store import (
    WORLD_KNOWLEDGE_STORE_ID,
    WorldKnowledgeStore,
)

UTC = timezone.utc
READY = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
STAMP = datetime(2026, 9, 10, 12, 5, tzinfo=UTC)


def _news_signal(**overrides: object) -> DriverNewsBundle:
    values: dict[str, object] = {
        "event_class": "earnings",
        "event_class_source": "analyst",
        "direction": "bullish",
        "strength": "strong",
        "severity": "watch",
        "horizon_bucket": "quarters",
        "attribution_quality": "symbol_sourced",
    }
    values.update(overrides)
    return DriverNewsBundle(**values)  # type: ignore[arg-type]


def _company_signal() -> DriverCompanyBundle:
    return DriverCompanyBundle(
        sector="eu_energy",
        thesis_status="watch",
        coverage_status="full",
        freshness_status="stale",
        catalyst_bucket="many",
        risk_bucket="few",
        depth="screen",
    )


def _envelope(
    seed: str = "ab",
    signal: DriverNewsBundle | DriverCompanyBundle | None = None,
    **overrides: object,
) -> KnowledgeArtifact:
    resolved = _news_signal() if signal is None else signal
    values: dict[str, object] = {
        "kind": "news_macro",
        "artifact_id": f"knowledge_artifact:v1:{seed * 32}",
        "subjects": (EntityRef(kind="instrument", entity_id="mic:XPAR:symbol:TTE"),),
        "schema_version": "news_brief.v1",
        "content_sha256": "00" * 32,
        "ready_at": READY,
        "source_refs": ("yahoo:uuid-1",),
    }
    values.update(overrides)
    placeholder = KnowledgeArtifact(**values)  # type: ignore[arg-type]
    row_hash = knowledge_artifact_content_sha256(knowledge_artifact_row(placeholder, resolved.to_dict()))
    values["content_sha256"] = row_hash
    return KnowledgeArtifact(**values)  # type: ignore[arg-type]


def _store(root: Path) -> WorldKnowledgeStore:
    return WorldKnowledgeStore(root, clock=lambda: STAMP)


def test_append_persists_row_and_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signal = _news_signal()
    ref = store.append_artifact(_envelope(signal=signal), signal)
    assert ref.identity.artifact_id == "knowledge_artifact:v1:" + "ab" * 32
    assert ref.receipt.subject.kind == KNOWLEDGE_ARTIFACT_SUBJECT_KIND
    assert ref.receipt.subject.subject_id == ref.identity.artifact_id
    assert ref.receipt.subject.content_sha256 == ref.identity.content_sha256
    assert ref.receipt.storage_locator.store_id == WORLD_KNOWLEDGE_STORE_ID
    assert (tmp_path / "artifacts" / "2026-09-10.jsonl").is_file()
    assert ref.receipt.ready_at == STAMP


def test_reappend_same_content_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signal = _news_signal()
    first = store.append_artifact(_envelope(signal=signal), signal)
    second = store.append_artifact(_envelope(signal=signal), signal)
    assert second.receipt.receipt_id == first.receipt.receipt_id
    rows = (tmp_path / "artifacts" / "2026-09-10.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1


def test_same_id_different_content_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_signal = _news_signal()
    store.append_artifact(_envelope(signal=first_signal), first_signal)
    other_signal = _news_signal(direction="bearish")
    with pytest.raises(ValueError, match="conflict"):
        store.append_artifact(_envelope(signal=other_signal), other_signal)


def test_envelope_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signal = _news_signal()
    envelope = _envelope(signal=signal)
    tampered = KnowledgeArtifact(**{**envelope.to_dict(), "content_sha256": "ff" * 32})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="content_sha256"):
        store.append_artifact(tampered, signal)


def test_ready_at_is_required(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signal = _news_signal()
    envelope = _envelope(signal=signal, ready_at=None)
    with pytest.raises(ValueError, match="ready_at"):
        store.append_artifact(envelope, signal)


def test_macro_observation_kind_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signal = _news_signal()
    envelope = _envelope(signal=signal, kind="macro_world_observation")
    with pytest.raises(ValueError, match="ABOUT overlay"):
        store.append_artifact(envelope, signal)


def test_signal_kind_must_match_envelope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    company = _company_signal()
    news_envelope = _envelope(signal=company)
    with pytest.raises(TypeError, match="DriverNewsBundle"):
        store.append_artifact(news_envelope, company)
    with pytest.raises(TypeError, match="KnowledgeArtifact"):
        store.append_artifact({"kind": "news_macro"}, _news_signal())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DriverNewsBundle or DriverCompanyBundle"):
        store.append_artifact(_envelope(), {"event_class": "earnings"})  # type: ignore[arg-type]


def test_artifact_id_must_be_graph_namespaced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signal = _news_signal()
    envelope = _envelope(signal=signal, artifact_id="brief-123")
    with pytest.raises(ValueError, match="artifact_id"):
        store.append_artifact(envelope, signal)


def test_append_tolerates_corrupt_history_rows(tmp_path: Path) -> None:
    day = tmp_path / "artifacts"
    day.mkdir(parents=True)
    (day / "2026-09-09.jsonl").write_text(
        json.dumps({"envelope": ["not", "a", "mapping"], "signal": {}}) + "\n"
    )
    store = _store(tmp_path)
    signal = _news_signal()
    persisted = store.append_artifact(_envelope(signal=signal), signal)
    assert persisted.identity.artifact_id == "knowledge_artifact:v1:" + "ab" * 32


def test_append_reuses_identical_row_and_rejects_divergent_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    signal = _news_signal()
    envelope = _envelope(signal=signal)
    first = store.append_artifact(envelope, signal)
    second = store.append_artifact(envelope, signal)
    assert second.identity == first.identity
    history = tmp_path / "artifacts" / "2026-09-10.jsonl"
    rows = [line for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    divergent_signal = _news_signal(direction="bearish")
    divergent = _envelope(signal=divergent_signal)
    assert divergent.artifact_id == envelope.artifact_id
    assert divergent.content_sha256 != envelope.content_sha256
    with pytest.raises(ValueError, match="different content"):
        store.append_artifact(divergent, divergent_signal)
