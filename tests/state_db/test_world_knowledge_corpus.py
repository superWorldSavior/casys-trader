from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.domain.world_company import DriverCompanyBundle
from trader.domain.world_context import EntityRef, KnowledgeArtifact
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    WorldEntityRef,
)
from trader.domain.world_knowledge import (
    AboutKnowledgeLink,
    knowledge_artifact_content_sha256,
    knowledge_artifact_row,
)
from trader.domain.world_news import DriverNewsBundle
from trader.infrastructure.state_db.world_knowledge_corpus import (
    bind_about_relation,
    load_knowledge_corpus,
)
from trader.infrastructure.state_db.world_knowledge_store import WorldKnowledgeStore

UTC = timezone.utc
READY = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
STAMP = datetime(2026, 9, 10, 12, 5, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 11, tzinfo=UTC)
UNTIL = datetime(2026, 9, 20, tzinfo=UTC)
INSTRUMENT = "mic:XTAI:symbol:2330"


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
        sector="v1:semiconductors",
        thesis_status="intact",
        coverage_status="full",
        freshness_status="fresh",
        catalyst_bucket="few",
        risk_bucket="few",
        depth="screen",
    )


def _envelope(
    seed: str,
    signal: DriverNewsBundle | DriverCompanyBundle,
    *,
    kind: str = "news_macro",
    subjects: tuple[EntityRef, ...] = (EntityRef(kind="instrument", entity_id=INSTRUMENT),),
    ready_at: datetime | None = READY,
    valid_until: datetime | None = None,
) -> KnowledgeArtifact:
    values: dict[str, object] = {
        "kind": kind,
        "artifact_id": f"knowledge_artifact:v1:{seed * 32}",
        "subjects": subjects,
        "schema_version": "news_brief.v1",
        "content_sha256": "00" * 32,
        "ready_at": ready_at,
        "valid_until": valid_until,
        "source_refs": ("yahoo:uuid-1",),
    }
    placeholder = KnowledgeArtifact(**values)  # type: ignore[arg-type]
    values["content_sha256"] = knowledge_artifact_content_sha256(
        knowledge_artifact_row(placeholder, signal.to_dict())
    )
    return KnowledgeArtifact(**values)  # type: ignore[arg-type]


def _about(
    ref: KnowledgeArtifactRef,
    *,
    target: WorldEntityRef | None = None,
    effective_from: datetime = READY,
    effective_until: datetime | None = None,
    source_refs: tuple[str, ...] = ("artifact:seed",),
) -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="ABOUT",
        source=ref,
        target=target or WorldEntityRef(kind="instrument", entity_id=INSTRUMENT),
        effective_from=effective_from,
        effective_until=effective_until,
        ontology_revision="market_ontology.v1",
        source_refs=source_refs,
    )


def _stored(root: Path, seed: str = "ab") -> tuple[KnowledgeArtifactRef, str]:
    store = WorldKnowledgeStore(root, clock=lambda: STAMP)
    signal = _news_signal()
    persisted = store.append_artifact(_envelope(seed, signal), signal)
    return persisted.identity, persisted.receipt.receipt_id


def test_corpus_loads_attested_joins_and_nothing_else(tmp_path: Path) -> None:
    store = WorldKnowledgeStore(tmp_path, clock=lambda: STAMP)
    news = _news_signal()
    news_ref = store.append_artifact(_envelope("ab", news), news).identity
    company = _company_signal()
    company_ref = store.append_artifact(
        _envelope("cd", company, kind="company_intelligence"), company
    ).identity
    corpus = load_knowledge_corpus(tmp_path)
    assert set(corpus) == {news_ref.artifact_id, company_ref.artifact_id}
    envelope, signal, receipt = corpus[news_ref.artifact_id]
    assert envelope.kind == "news_macro"
    assert isinstance(signal, DriverNewsBundle)
    assert signal.event_class == "earnings"
    assert receipt.subject.content_sha256 == news_ref.content_sha256
    _, company_signal, _ = corpus[company_ref.artifact_id]
    assert isinstance(company_signal, DriverCompanyBundle)


def test_corpus_ignores_missing_roots_and_tampered_rows(tmp_path: Path) -> None:
    assert load_knowledge_corpus(None) == {}
    assert load_knowledge_corpus(tmp_path / "absent") == {}
    ref, _ = _stored(tmp_path)
    assert ref.artifact_id in load_knowledge_corpus(tmp_path)
    history = tmp_path / "artifacts" / "2026-09-10.jsonl"
    row = json.loads(history.read_text(encoding="utf-8").strip().splitlines()[0])
    row["signal"]["direction"] = "bearish"
    history.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert load_knowledge_corpus(tmp_path) == {}


def test_corpus_rejects_duplicate_receipts_for_one_subject(tmp_path: Path) -> None:
    ref, _ = _stored(tmp_path)
    receipts = sorted((tmp_path / "artifacts" / "availability_receipts").glob("*.jsonl"))
    assert len(receipts) == 1
    extra = tmp_path / "artifacts" / "availability_receipts" / "2026-09-11.jsonl"
    extra.write_text(receipts[0].read_text(encoding="utf-8"), encoding="utf-8")
    assert ref.artifact_id not in load_knowledge_corpus(tmp_path)


def test_bind_about_hydrates_news_state(tmp_path: Path) -> None:
    ref, receipt_id = _stored(tmp_path)
    corpus = load_knowledge_corpus(tmp_path)
    binding = bind_about_relation(_about(ref), cutoff=CUTOFF, corpus=corpus)
    assert binding.driver_state.signal_class == "news_state"
    assert binding.driver_state.artifact_kind == "news_macro"
    assert binding.driver_state.news is not None
    assert binding.driver_state.news.event_class == "earnings"
    assert binding.driver_state.until_bound == "unbounded"
    assert ref.artifact_id in binding.evidence_refs
    assert receipt_id in binding.evidence_refs


def test_bind_about_hydrates_bounded_company_state(tmp_path: Path) -> None:
    store = WorldKnowledgeStore(tmp_path, clock=lambda: STAMP)
    signal = _company_signal()
    envelope = _envelope("cd", signal, kind="company_intelligence", valid_until=UNTIL)
    ref = store.append_artifact(envelope, signal).identity
    corpus = load_knowledge_corpus(tmp_path)
    relation = _about(ref, effective_until=UNTIL)
    binding = bind_about_relation(relation, cutoff=CUTOFF, corpus=corpus)
    assert binding.driver_state.signal_class == "company_state"
    assert binding.driver_state.company is not None
    assert binding.driver_state.company.sector == "v1:semiconductors"
    assert binding.driver_state.until_bound == "bounded"


def test_bind_about_stays_missing_when_unjoinable(tmp_path: Path) -> None:
    ref, _ = _stored(tmp_path)
    corpus = load_knowledge_corpus(tmp_path)
    assert bind_about_relation(_about(ref), cutoff=CUTOFF, corpus={}).driver_state.missingness == (
        "artifact_unjoined"
    )
    ghost = KnowledgeArtifactRef(artifact_id="knowledge_artifact:v1:" + "ff" * 32, content_sha256="ff" * 32)
    assert (
        bind_about_relation(_about(ghost), cutoff=CUTOFF, corpus=corpus).driver_state.missingness
        == "artifact_unjoined"
    )
    tampered = KnowledgeArtifactRef(artifact_id=ref.artifact_id, content_sha256="00" * 32)
    assert (
        bind_about_relation(_about(tampered), cutoff=CUTOFF, corpus=corpus).driver_state.missingness
        == "artifact_unjoined"
    )


def test_bind_about_enforces_pit_and_window_and_subjects(tmp_path: Path) -> None:
    ref, _ = _stored(tmp_path)
    corpus = load_knowledge_corpus(tmp_path)
    late_cutoff = datetime(2026, 9, 10, 11, 0, tzinfo=UTC)
    assert (
        bind_about_relation(_about(ref), cutoff=late_cutoff, corpus=corpus).driver_state.missingness
        == "artifact_unjoined"
    )
    other = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:9999")
    assert (
        bind_about_relation(_about(ref, target=other), cutoff=CUTOFF, corpus=corpus).driver_state.missingness
        == "artifact_unjoined"
    )
    shifted = READY.replace(hour=13)
    assert (
        bind_about_relation(_about(ref, effective_from=shifted), cutoff=CUTOFF, corpus=corpus).driver_state.missingness
        == "artifact_unjoined"
    )
    assert (
        bind_about_relation(
            _about(ref, effective_until=UNTIL), cutoff=CUTOFF, corpus=corpus
        ).driver_state.missingness
        == "artifact_unjoined"
    )


def test_bind_about_rejects_wrong_receipt_ref_but_accepts_right_one(tmp_path: Path) -> None:
    ref, receipt_id = _stored(tmp_path)
    corpus = load_knowledge_corpus(tmp_path)
    wrong = (f"{receipt_id}/{'00' * 32}",)
    assert (
        bind_about_relation(_about(ref, source_refs=wrong), cutoff=CUTOFF, corpus=corpus).driver_state.missingness
        == "artifact_unjoined"
    )
    _, _, receipt = corpus[ref.artifact_id]
    right = (f"{receipt.receipt_id}/{receipt.receipt_sha256}",)
    binding = bind_about_relation(_about(ref, source_refs=right), cutoff=CUTOFF, corpus=corpus)
    assert binding.driver_state.signal_class == "news_state"


def test_about_link_attaches_receipt_ref_the_binder_accepts(tmp_path: Path) -> None:
    store = WorldKnowledgeStore(tmp_path, clock=lambda: STAMP)
    signal = _news_signal()
    envelope = _envelope("ab", signal)
    persisted = store.append_artifact(envelope, signal)
    draft = _about(persisted.identity)
    link = AboutKnowledgeLink.from_persisted(draft, envelope, persisted)
    assert link.status == "linked"
    assert link.artifact_ref == persisted.identity
    assert link.relation is not None
    assert link.relation.source_refs[-1] == (
        f"{persisted.receipt.receipt_id}/{persisted.receipt.receipt_sha256}"
    )
    assert link.relation.relation_id != draft.relation_id
    corpus = load_knowledge_corpus(tmp_path)
    binding = bind_about_relation(link.relation, cutoff=CUTOFF, corpus=corpus)
    assert binding.driver_state.signal_class == "news_state"


def test_about_link_skips_foreign_receipt_and_requires_family_stamp(tmp_path: Path) -> None:
    store = WorldKnowledgeStore(tmp_path, clock=lambda: STAMP)
    signal = _news_signal()
    first = store.append_artifact(_envelope("ab", signal), signal)
    second = store.append_artifact(_envelope("cd", signal), signal)
    envelope = _envelope("ab", signal)
    skipped = AboutKnowledgeLink.from_persisted(_about(first.identity), envelope, second)
    assert skipped.status == "skipped"
    assert skipped.skip_reason == "receipt_mismatch"
    assert AboutKnowledgeLink.from_persisted(_about(first.identity), envelope, first).status == "linked"
    with pytest.raises(ValueError, match="stable ontology family"):
        AboutKnowledgeLink.from_persisted(
            KnowledgeWorldRelation.from_mapping({**_about(first.identity).to_dict(), "ontology_revision": "market_ontology:v1:" + "ff" * 32, "relation_id": None, "content_sha256": None}),
            envelope,
            first,
        )


def test_corpus_skips_non_mapping_envelope_rows(tmp_path: Path) -> None:
    ref, _ = _stored(tmp_path)
    assert ref.artifact_id in load_knowledge_corpus(tmp_path)
    planted = tmp_path / "artifacts" / "2026-09-09.jsonl"
    planted.write_text(json.dumps({"envelope": ["not", "a", "mapping"], "signal": {}}) + "\n")
    assert ref.artifact_id in load_knowledge_corpus(tmp_path)
