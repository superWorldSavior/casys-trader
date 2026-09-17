from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_availability import (
    WORLD_AVAILABILITY_RECEIPT_SCHEMA,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
)
from trader.domain.world_context import EntityRef, KnowledgeArtifact
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    WorldEntityRef,
    WorldObservationRef,
)
from trader.domain.world_knowledge import (
    ABOUT_GRAPH_LINK_STATUSES,
    KNOWLEDGE_ARTIFACT_SUBJECT_KIND,
    AboutKnowledgeLink,
    about_receipt_ref,
    knowledge_artifact_content_sha256,
    knowledge_artifact_id,
    knowledge_artifact_ref_for_row,
    knowledge_artifact_row,
    knowledge_artifact_scope,
    require_about_signal_kind,
    require_ready_at,
)

MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_knowledge.py"
UTC = timezone.utc
READY = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _envelope(**overrides: object) -> KnowledgeArtifact:
    values: dict[str, object] = {
        "kind": "news_macro",
        "artifact_id": "knowledge_artifact:v1:" + "ab" * 32,
        "subjects": (EntityRef(kind="instrument", entity_id="mic:XPAR:symbol:TTE"),),
        "schema_version": "news_brief.v1",
        "content_sha256": "00" * 32,
        "ready_at": READY,
        "source_refs": ("yahoo:uuid-1",),
    }
    values.update(overrides)
    return KnowledgeArtifact(**values)  # type: ignore[arg-type]


def _signal() -> dict[str, object]:
    return {
        "schema_version": "driver_news_bundle.v1",
        "event_class": "earnings",
        "direction": "bullish",
        "strength": "strong",
        "severity": "watch",
        "horizon_bucket": "quarters",
        "attribution_quality": "symbol_sourced",
    }


def test_row_hash_ignores_envelope_hash_field() -> None:
    row = knowledge_artifact_row(_envelope(), _signal())
    assert set(row) == {"envelope", "signal"}
    first = knowledge_artifact_content_sha256(row)
    assert len(first) == 64
    altered = knowledge_artifact_row(_envelope(content_sha256="ff" * 32), _signal())
    assert knowledge_artifact_content_sha256(altered) == first
    tampered = {"envelope": {**row["envelope"], "severity": "risk"}, "signal": row["signal"]}
    assert knowledge_artifact_content_sha256(tampered) != first
    with pytest.raises(TypeError, match="artifact row must be a mapping"):
        knowledge_artifact_content_sha256(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="envelope and signal mappings"):
        knowledge_artifact_content_sha256({"envelope": {}})


def test_row_builder_rejects_non_mappings() -> None:
    with pytest.raises(TypeError, match="KnowledgeArtifact or a mapping"):
        knowledge_artifact_row(42, _signal())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="signal must be a mapping"):
        knowledge_artifact_row(_envelope(), 42)  # type: ignore[arg-type]


def test_ref_rebuild_is_stable_and_prefixed() -> None:
    row = knowledge_artifact_row(_envelope(), _signal())
    ref = knowledge_artifact_ref_for_row(row)
    assert ref.artifact_id == "knowledge_artifact:v1:" + "ab" * 32
    assert ref.content_sha256 == knowledge_artifact_content_sha256(row)
    with pytest.raises(TypeError, match="artifact row must be a mapping"):
        knowledge_artifact_ref_for_row(42)  # type: ignore[arg-type]


def test_artifact_id_is_deterministic_and_namespaced() -> None:
    first = knowledge_artifact_id("news_macro", "brief-1|symbol|X|0")
    assert first.startswith("knowledge_artifact:v1:")
    assert len(first) == len("knowledge_artifact:v1:") + 64
    assert first == knowledge_artifact_id("news_macro", "brief-1|symbol|X|0")
    assert first != knowledge_artifact_id("news_macro", "brief-1|symbol|X|1")
    assert first != knowledge_artifact_id("company_intelligence", "brief-1|symbol|X|0")
    with pytest.raises(ValueError, match="artifact kind must be one of"):
        knowledge_artifact_id("rumor", "x")
    with pytest.raises(ValueError, match="at least one source part"):
        knowledge_artifact_id("news_macro")
    with pytest.raises(ValueError, match="non-empty string"):
        knowledge_artifact_id("news_macro", "  ")


def test_scope_renders_sorted_subjects() -> None:
    envelope = _envelope(
        subjects=(
            EntityRef(kind="instrument", entity_id="mic:XPAR:symbol:TTE"),
            EntityRef(kind="company", entity_id="issuer:yahoo:v1:XPAR:TTE"),
        )
    )
    assert (
        knowledge_artifact_scope(envelope)
        == "company:issuer:yahoo:v1:XPAR:TTE;instrument:mic:XPAR:symbol:TTE"
    )
    assert knowledge_artifact_scope(envelope.to_dict()) == knowledge_artifact_scope(envelope)
    assert KNOWLEDGE_ARTIFACT_SUBJECT_KIND == "knowledge_artifact"


def test_about_signal_kinds_exclude_macro_observations() -> None:
    assert require_about_signal_kind("news_macro") == "news_macro"
    assert require_about_signal_kind("company_intelligence") == "company_intelligence"
    with pytest.raises(ValueError, match="cannot back an ABOUT overlay"):
        require_about_signal_kind("macro_world_observation")
    with pytest.raises(ValueError, match="artifact kind must be one of"):
        require_about_signal_kind("rumor")


def test_ready_at_is_required_for_pit() -> None:
    assert require_ready_at(READY) == READY
    assert require_ready_at("2026-09-10T12:00:00+00:00") == READY
    with pytest.raises(ValueError, match="requires ready_at"):
        require_ready_at(None)


def _receipt(stamped: bool = True) -> WorldAvailabilityReceipt:
    subject = WorldAvailabilitySubjectRef(
        kind="knowledge_artifact",
        subject_id="knowledge_artifact:v1:" + "ab" * 32,
        content_sha256="00" * 32,
    )
    receipt = WorldAvailabilityReceipt(
        receipt_id="world-availability-receipt:v1:" + "cd" * 32,
        subject=subject,
        scope="instrument:mic:XPAR:symbol:TTE",
        storage_locator=WorldStorageLocator(
            kind="jsonl", store_id="world-knowledge-jsonl.v1", path="artifacts/2026-09-10.jsonl"
        ),
        content_sha256="00" * 32,
        schema_version=WORLD_AVAILABILITY_RECEIPT_SCHEMA,
    )
    if stamped:
        object.__setattr__(receipt, "ready_at", READY)
        object.__setattr__(receipt, "receipt_sha256", "ef" * 32)
    return receipt


def _about_relation() -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="ABOUT",
        source=KnowledgeArtifactRef(
            artifact_id="knowledge_artifact:v1:" + "ab" * 32, content_sha256="00" * 32
        ),
        target=WorldEntityRef(kind="instrument", entity_id="mic:XPAR:symbol:TTE"),
        effective_from=READY,
        effective_until=None,
        ontology_revision="market_ontology.v1",
        source_refs=("situation_note:brief-1",),
    )


def test_about_receipt_ref_needs_a_stamped_receipt() -> None:
    receipt = _receipt()
    assert about_receipt_ref(receipt) == f"{receipt.receipt_id}/{'ef' * 32}"
    with pytest.raises(TypeError, match="WorldAvailabilityReceipt"):
        about_receipt_ref("world-availability-receipt:v1:cd")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="store-stamped"):
        about_receipt_ref(_receipt(stamped=False))


def test_about_link_rejects_mixed_shapes() -> None:
    relation = _about_relation()
    ref = KnowledgeArtifactRef(
        artifact_id="knowledge_artifact:v1:" + "ab" * 32, content_sha256="00" * 32
    )
    assert ABOUT_GRAPH_LINK_STATUSES == frozenset({"linked", "skipped"})
    with pytest.raises(ValueError, match="link status must be one of"):
        AboutKnowledgeLink(status="linked-ish")
    with pytest.raises(ValueError, match="must not carry a skip_reason"):
        AboutKnowledgeLink(status="linked", relation=relation, artifact_ref=ref, skip_reason="x")
    with pytest.raises(TypeError, match="requires KnowledgeWorldRelation"):
        AboutKnowledgeLink(status="linked", artifact_ref=ref)
    with pytest.raises(TypeError, match="requires KnowledgeArtifactRef"):
        AboutKnowledgeLink(status="linked", relation=relation)
    with pytest.raises(ValueError, match="must not carry a relation"):
        AboutKnowledgeLink(status="skipped", relation=relation, skip_reason="receipt_mismatch")
    with pytest.raises(ValueError, match="skip_reason must be one of"):
        AboutKnowledgeLink(status="skipped", skip_reason="nope")


def test_about_link_from_persisted_rejects_type_errors() -> None:
    relation = _about_relation()
    envelope = _envelope()
    with pytest.raises(TypeError, match="relation must be KnowledgeWorldRelation"):
        AboutKnowledgeLink.from_persisted("relation", envelope, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="envelope must be KnowledgeArtifact"):
        AboutKnowledgeLink.from_persisted(relation, "envelope", None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="persisted must be PersistedWorldRef"):
        AboutKnowledgeLink.from_persisted(relation, envelope, None)  # type: ignore[arg-type]
    observes = KnowledgeWorldRelation(
        kind="OBSERVES",
        source=WorldObservationRef(observation_id="world_observation:v1:" + "ab" * 32),
        target=WorldEntityRef(kind="country", entity_id="iso-3166:TW"),
        effective_from=READY,
        effective_until=None,
        ontology_revision="market_ontology.v1",
        source_refs=("macro:obs-1",),
    )
    with pytest.raises(ValueError, match="only binds ABOUT relations"):
        AboutKnowledgeLink.from_persisted(observes, envelope, None)  # type: ignore[arg-type]


def test_world_knowledge_stays_stdlib_domain() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
