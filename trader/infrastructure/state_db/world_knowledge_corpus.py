"""Read-only attested knowledge-artifact corpus and ABOUT binder.

Shared by the formation and evaluation query adapters so both sides hydrate
identical ``DriverState`` bindings: frozen news/company signals joined by
artifact id, verified by content hash, gated by point-in-time clocks. Never
creates directories or files, never re-derives signals from briefs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.application.world_model.pattern_discovery_ports import PatternDriverStateBinding
from trader.domain.world_availability import (
    WorldAvailabilityReceipt,
    _attest_verified_store_receipt,
    world_subject_content_sha256,
)
from trader.domain.world_company import DriverCompanyBundle
from trader.domain.world_context import EntityRef, KnowledgeArtifact
from trader.domain.world_driver import DriverState
from trader.domain.world_episode import parse_utc_timestamp
from trader.domain.world_graph import KnowledgeArtifactRef, KnowledgeWorldRelation, WorldEntityRef
from trader.domain.world_knowledge import (
    KNOWLEDGE_ARTIFACT_SUBJECT_KIND,
    knowledge_artifact_content_sha256,
    knowledge_artifact_scope,
)
from trader.domain.world_news import DriverNewsBundle
from trader.infrastructure.state_db._jsonl_store import read_jsonl_objects
from trader.infrastructure.state_db.availability_receipt import load_receipts, parse_world_availability_receipt
from trader.infrastructure.state_db.world_knowledge_store import WORLD_KNOWLEDGE_STORE_ID

KnowledgeArtifactJoin = tuple[
    KnowledgeArtifact, DriverNewsBundle | DriverCompanyBundle, WorldAvailabilityReceipt
]

_RECEIPT_REF_PREFIX = "world-availability-receipt:v1:"


def _unique_evidence(*values: str | None) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.append(text)
    return tuple(seen)


def _receipt_ref_from_source_refs(refs: Sequence[str]) -> tuple[str, str] | None:
    # Intentional mirror of the formation query parser (not imported: the
    # formation query imports this corpus, so sharing would be a cycle).
    # Deliberately stricter on one point: a malformed receipt token yields a
    # mismatch tuple (fail closed) instead of being skipped.
    for token in refs:
        if not isinstance(token, str) or not token.startswith(_RECEIPT_REF_PREFIX):
            continue
        rest = token[len(_RECEIPT_REF_PREFIX):]
        if "/" not in rest:
            continue
        receipt_id, _, digest = rest.partition("/")
        if receipt_id.strip() and digest.strip():
            return (_RECEIPT_REF_PREFIX + receipt_id.strip(), digest.strip())
    return None


def _resolve_knowledge_path(root: Path, locator_path: str) -> Path | None:
    text = str(locator_path or "").strip()
    if not text:
        return None
    candidate_rel = Path(text)
    if candidate_rel.is_absolute() or candidate_rel.anchor or ".." in candidate_rel.parts:
        return None
    resolved_root = root.resolve()
    candidate = (resolved_root / candidate_rel).resolve()
    if not candidate.is_relative_to(resolved_root):
        return None
    return candidate


def _index_knowledge_history_payloads(history: Path) -> dict[str, dict[str, Any]]:
    if not history.is_file():
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for row in read_jsonl_objects(history):
        if isinstance(row, dict):
            indexed.setdefault(world_subject_content_sha256(row), dict(row))
    return indexed


def _iter_knowledge_artifact_receipts(root: Path) -> tuple[WorldAvailabilityReceipt, ...]:
    receipts: list[WorldAvailabilityReceipt] = []
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file() or path.parent.name != "availability_receipts":
            continue
        for row in load_receipts(path):
            parsed = parse_world_availability_receipt(row)
            if parsed is None:
                continue
            if parsed.subject.kind != KNOWLEDGE_ARTIFACT_SUBJECT_KIND:
                continue
            if parsed.storage_locator.store_id != WORLD_KNOWLEDGE_STORE_ID:
                continue
            receipts.append(parsed)
    return tuple(receipts)


def load_knowledge_corpus(root: Path | None) -> Mapping[str, KnowledgeArtifactJoin]:
    """Read-only attested artifact corpus. Never creates directories or files."""

    if root is None or not root.exists() or not root.is_dir():
        return {}
    grouped: dict[str, list[WorldAvailabilityReceipt]] = {}
    locators: dict[str, None] = {}
    for receipt in _iter_knowledge_artifact_receipts(root):
        grouped.setdefault(receipt.subject.subject_id, []).append(receipt)
        locator = receipt.storage_locator
        if locator.kind == "jsonl" and locator.store_id == WORLD_KNOWLEDGE_STORE_ID and locator.path:
            locators[str(locator.path)] = None
    payloads_by_locator = {
        locator_path: _index_knowledge_history_payloads(history)
        for locator_path in locators
        if (history := _resolve_knowledge_path(root, locator_path)) is not None
    }
    corpus: dict[str, KnowledgeArtifactJoin] = {}
    for receipts in grouped.values():
        if len(receipts) != 1:
            continue
        receipt = receipts[0]
        locator = receipt.storage_locator
        if locator.kind != "jsonl" or locator.store_id != WORLD_KNOWLEDGE_STORE_ID or not locator.path:
            continue
        payload = payloads_by_locator.get(str(locator.path), {}).get(receipt.subject.content_sha256)
        if payload is None:
            continue
        envelope_raw = payload.get("envelope")
        if not isinstance(envelope_raw, Mapping):
            continue
        if str(envelope_raw.get("artifact_id") or "") != receipt.subject.subject_id:
            continue
        try:
            envelope = KnowledgeArtifact.from_mapping(
                {**envelope_raw, "content_sha256": receipt.subject.content_sha256}
            )
            signal_raw = payload.get("signal")
            if not isinstance(signal_raw, Mapping):
                continue
            if envelope.kind == "news_macro":
                signal: DriverNewsBundle | DriverCompanyBundle = DriverNewsBundle.from_mapping(signal_raw)
            elif envelope.kind == "company_intelligence":
                signal = DriverCompanyBundle.from_mapping(signal_raw)
            else:
                continue
            attested = _attest_verified_store_receipt(
                receipt,
                expected_subject=receipt.subject,
                expected_scope=receipt.scope,
                expected_locator=locator,
            )
        except (TypeError, ValueError):
            continue
        if knowledge_artifact_content_sha256(payload) != attested.subject.content_sha256:
            continue
        try:
            expected_scope = knowledge_artifact_scope(envelope)
        except (TypeError, ValueError):
            continue
        if attested.scope != expected_scope:
            continue
        corpus[envelope.artifact_id] = (envelope, signal, attested)
    return corpus


def _missing_about_binding(relation: KnowledgeWorldRelation, *evidence: str | None) -> PatternDriverStateBinding:
    return PatternDriverStateBinding(
        relation_id=relation.relation_id,
        driver_state=DriverState.missing(
            missingness="artifact_unjoined",
            source_family="knowledge_artifact",
            artifact_kind=None,
            until_bound="unknown",
        ),
        evidence_refs=_unique_evidence(*evidence),
    )


def bind_about_relation(
    relation: KnowledgeWorldRelation,
    *,
    cutoff: datetime,
    corpus: Mapping[str, KnowledgeArtifactJoin],
) -> PatternDriverStateBinding:
    """Join one admitted ABOUT relation to its frozen signal, PIT-gated."""

    source = relation.source
    evidence: tuple[str | None, ...] = (
        source.artifact_id if isinstance(source, KnowledgeArtifactRef) else None,
        *(relation.source_refs or ()),
    )
    if not isinstance(source, KnowledgeArtifactRef):
        return _missing_about_binding(relation, *evidence)
    joined = corpus.get(source.artifact_id)
    if joined is None:
        return _missing_about_binding(relation, *evidence)
    envelope, signal, receipt = joined
    evidence = (
        source.artifact_id,
        f"{source.artifact_id}/{source.content_sha256}",
        *evidence,
        receipt.receipt_id,
    )
    if source.content_sha256 != receipt.subject.content_sha256:
        return _missing_about_binding(relation, *evidence)
    if envelope.kind == "news_macro" and not isinstance(signal, DriverNewsBundle):
        return _missing_about_binding(relation, *evidence)
    if envelope.kind == "company_intelligence" and not isinstance(signal, DriverCompanyBundle):
        return _missing_about_binding(relation, *evidence)
    if not isinstance(relation.target, WorldEntityRef):
        return _missing_about_binding(relation, *evidence)
    subjects = {
        (subject.kind, subject.entity_id)
        for subject in envelope.subjects
        if isinstance(subject, EntityRef)
    }
    if (relation.target.kind, relation.target.entity_id) not in subjects:
        return _missing_about_binding(relation, *evidence)
    try:
        ready_at = parse_utc_timestamp(envelope.ready_at, "ready_at")
    except (TypeError, ValueError):
        return _missing_about_binding(relation, *evidence)
    if relation.effective_from != ready_at or relation.effective_until != envelope.valid_until:
        return _missing_about_binding(relation, *evidence)
    if not relation.effective_at(cutoff):
        return _missing_about_binding(relation, *evidence)
    if ready_at > cutoff:
        return _missing_about_binding(relation, *evidence)
    if envelope.valid_until is not None and cutoff >= envelope.valid_until:
        return _missing_about_binding(relation, *evidence)
    if receipt.ready_at is None or receipt.ready_at > cutoff:
        return _missing_about_binding(relation, *evidence)
    source_receipt = _receipt_ref_from_source_refs(relation.source_refs)
    if source_receipt is not None and source_receipt != (receipt.receipt_id, receipt.receipt_sha256):
        return _missing_about_binding(relation, *evidence)
    if KnowledgeArtifactRef(
        artifact_id=envelope.artifact_id, content_sha256=receipt.subject.content_sha256
    ) != source:
        return _missing_about_binding(relation, *evidence)
    try:
        if isinstance(signal, DriverNewsBundle):
            driver = DriverState.from_news_signal(
                signal, until_bound="bounded" if envelope.valid_until is not None else "unbounded"
            )
        else:
            driver = DriverState.from_company_signal(
                signal, until_bound="bounded" if envelope.valid_until is not None else "unbounded"
            )
    except (TypeError, ValueError):
        return _missing_about_binding(relation, *evidence)
    return PatternDriverStateBinding(
        relation_id=relation.relation_id,
        driver_state=driver,
        evidence_refs=_unique_evidence(*evidence),
    )


__all__ = [
    "KnowledgeArtifactJoin",
    "bind_about_relation",
    "load_knowledge_corpus",
]
