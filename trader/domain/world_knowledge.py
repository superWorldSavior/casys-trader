"""Knowledge-artifact row contract for ABOUT hydration. Stdlib/domain only.

An ABOUT source (``KnowledgeArtifactRef``) joins a stored row shaped
``{"envelope": ..., "signal": ...}``: a ``world_context.KnowledgeArtifact``
envelope plus the frozen driver bundle (news or company signal) derived at
write time. Hydration re-derives nothing: rule bumps cannot rewrite frozen
history. The single content hash covers the row with the envelope hash field
stripped, so writers, store, and corpus verify the same bytes.

Conventions enforced here, at the store, and in the corpus alike:

- ``artifact_id`` is ``knowledge_artifact:v1:<sha256>`` (graph-namespaced).
- envelope ``content_sha256`` equals the row content hash.
- subjects are graph-namespaced ``kind``/``entity_id`` pairs; the ABOUT target
  must be one of them.
- ``ready_at`` is required (availability clock); ``valid_until`` bounds the
  signal (``None`` means unbounded).
- receipt scope renders ``kind:entity_id`` subjects joined by ``;``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from trader.domain.world_availability import (
    PersistedWorldRef,
    WorldAvailabilityReceipt,
    world_subject_content_sha256,
)
from trader.domain.world_context import ARTIFACT_KINDS, EntityRef, KnowledgeArtifact
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
)
from trader.domain.world_ontology_lifecycle import MARKET_ONTOLOGY_REVISION_FAMILY

KNOWLEDGE_ARTIFACT_SUBJECT_KIND = "knowledge_artifact"
KNOWLEDGE_ARTIFACT_ROW_VERSION = "knowledge_artifact_row.v1"
ABOUT_SIGNAL_ARTIFACT_KINDS = frozenset({"news_macro", "company_intelligence"})
ABOUT_GRAPH_LINK_STATUSES = frozenset({"linked", "skipped"})
ABOUT_GRAPH_SKIP_REASONS = frozenset({"receipt_mismatch"})


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def knowledge_artifact_row(
    envelope: KnowledgeArtifact | Mapping[str, Any],
    signal: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonical stored row. The envelope hash field stays in place here."""

    if isinstance(envelope, KnowledgeArtifact):
        envelope_dict = envelope.to_dict()
    elif isinstance(envelope, Mapping):
        envelope_dict = dict(envelope)
    else:
        raise TypeError("envelope must be KnowledgeArtifact or a mapping")
    if not isinstance(signal, Mapping):
        raise TypeError("signal must be a mapping")
    return {"envelope": envelope_dict, "signal": dict(signal)}


def knowledge_artifact_content_sha256(row: Mapping[str, Any]) -> str:
    """Row content hash with the envelope hash field stripped (single hash)."""

    if not isinstance(row, Mapping):
        raise TypeError("artifact row must be a mapping")
    envelope = row.get("envelope")
    signal = row.get("signal")
    if not isinstance(envelope, Mapping) or not isinstance(signal, Mapping):
        raise TypeError("artifact row must hold envelope and signal mappings")
    stripped = dict(envelope)
    stripped.pop("content_sha256", None)
    return world_subject_content_sha256({"envelope": stripped, "signal": dict(signal)})


def knowledge_artifact_scope(envelope: KnowledgeArtifact | Mapping[str, Any]) -> str:
    """Receipt scope: sorted ``kind:entity_id`` subjects joined by ``;``."""

    if isinstance(envelope, KnowledgeArtifact):
        subjects = envelope.subjects
    elif isinstance(envelope, Mapping):
        raw = envelope.get("subjects") or ()
        if isinstance(raw, Mapping):
            raw = (raw,)
        subjects = tuple(EntityRef.from_mapping(item) for item in raw)
    else:
        raise TypeError("envelope must be KnowledgeArtifact or a mapping")
    rendered = sorted(f"{subject.kind}:{subject.entity_id}" for subject in subjects)
    if not rendered:
        raise ValueError("artifact envelope requires at least one subject")
    return ";".join(rendered)


def knowledge_artifact_ref_for_row(row: Mapping[str, Any]) -> KnowledgeArtifactRef:
    """Rebuild the ABOUT source ref a stored row must join."""

    if not isinstance(row, Mapping):
        raise TypeError("artifact row must be a mapping")
    envelope = row.get("envelope")
    if not isinstance(envelope, Mapping):
        raise TypeError("artifact row must hold an envelope mapping")
    return KnowledgeArtifactRef(
        artifact_id=envelope.get("artifact_id"),
        content_sha256=knowledge_artifact_content_sha256(row),
    )


def knowledge_artifact_id(kind: Any, *parts: str) -> str:
    """Deterministic ``knowledge_artifact:v1:<sha>`` id from source coordinates."""

    text = _required_text(kind, "kind").lower()
    if text not in ARTIFACT_KINDS:
        raise ValueError(f"artifact kind must be one of: {', '.join(sorted(ARTIFACT_KINDS))}")
    if not parts:
        raise ValueError("artifact id requires at least one source part")
    cleaned = [_required_text(part, "source part") for part in parts]
    digest = canonical_sha256({"kind": text, "parts": cleaned})
    return f"knowledge_artifact:v1:{digest}"


def require_about_signal_kind(kind: Any) -> str:
    """ABOUT hydration supports news and company signals only (macro rides OBSERVES)."""

    text = _required_text(kind, "kind").lower()
    if text not in ARTIFACT_KINDS:
        raise ValueError(f"artifact kind must be one of: {', '.join(sorted(ARTIFACT_KINDS))}")
    if text not in ABOUT_SIGNAL_ARTIFACT_KINDS:
        raise ValueError("macro_world_observation artifacts cannot back an ABOUT overlay")
    return text


def require_ready_at(value: Any) -> datetime:
    """Availability clock for PIT gating. Missing means unwritable/unjoinable."""

    if value is None:
        raise ValueError("artifact envelope requires ready_at for point-in-time gating")
    return parse_utc_timestamp(value, "ready_at")


def about_receipt_ref(receipt: WorldAvailabilityReceipt) -> str:
    """Single owner of the ``<receipt_id>/<receipt_sha256>`` drift-ref format.

    The ABOUT binder parses this token back; writers must use this formatter
    so the format can never drift between the link step and the query path.
    """

    if not isinstance(receipt, WorldAvailabilityReceipt):
        raise TypeError("receipt must be WorldAvailabilityReceipt")
    receipt_id = _required_text(receipt.receipt_id, "receipt_id")
    digest = receipt.receipt_sha256
    if not isinstance(digest, str) or not digest.strip():
        raise ValueError("receipt must be store-stamped before linking")
    return f"{receipt_id}/{digest.strip()}"


@dataclass(frozen=True)
class AboutKnowledgeLink:
    """Pure ABOUT link step. Binds one persisted artifact to one draft relation.

    The bridge builds drafts pre-receipt (the receipt only exists once the
    artifact row is durable). This step appends the store receipt ref to the
    draft relation so the query-time binder can verify the join. It never
    restamps the ontology revision: a draft built for another revision is a
    caller bug (raise), while a receipt/envelope mismatch is data (skip with
    a closed reason). ``relation_id`` changes with the appended source ref,
    so only linked relations may be appended to the graph.
    """

    status: str
    relation: KnowledgeWorldRelation | None = None
    skip_reason: str | None = None
    artifact_ref: KnowledgeArtifactRef | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in ABOUT_GRAPH_LINK_STATUSES:
            allowed = ", ".join(sorted(ABOUT_GRAPH_LINK_STATUSES))
            raise ValueError(f"link status must be one of: {allowed}")
        if status == "linked":
            if not isinstance(self.relation, KnowledgeWorldRelation):
                raise TypeError("linked result requires KnowledgeWorldRelation")
            if self.skip_reason is not None:
                raise ValueError("linked result must not carry a skip_reason")
            if not isinstance(self.artifact_ref, KnowledgeArtifactRef):
                raise TypeError("linked result requires KnowledgeArtifactRef")
        else:
            if self.relation is not None:
                raise ValueError("skipped result must not carry a relation")
            if self.artifact_ref is not None:
                raise ValueError("skipped result must not carry an artifact ref")
            reason = _required_text(self.skip_reason, "skip_reason")
            if reason not in ABOUT_GRAPH_SKIP_REASONS:
                allowed = ", ".join(sorted(ABOUT_GRAPH_SKIP_REASONS))
                raise ValueError(f"skip_reason must be one of: {allowed}")
            object.__setattr__(self, "skip_reason", reason)
        object.__setattr__(self, "status", status)

    @classmethod
    def from_persisted(
        cls,
        relation: KnowledgeWorldRelation,
        envelope: KnowledgeArtifact,
        persisted: PersistedWorldRef[KnowledgeArtifactRef],
    ) -> AboutKnowledgeLink:
        if not isinstance(relation, KnowledgeWorldRelation):
            raise TypeError("relation must be KnowledgeWorldRelation")
        if relation.kind != "ABOUT":
            raise ValueError("link step only binds ABOUT relations")
        if not isinstance(envelope, KnowledgeArtifact):
            raise TypeError("envelope must be KnowledgeArtifact")
        if not isinstance(persisted, PersistedWorldRef):
            raise TypeError("persisted must be PersistedWorldRef")
        # No tip witness: drafts pin the stable family, not the tip.
        if relation.ontology_revision != MARKET_ONTOLOGY_REVISION_FAMILY:
            raise ValueError("draft must pin the stable ontology family")
        receipt = persisted.receipt
        expected = KnowledgeArtifactRef(
            artifact_id=envelope.artifact_id, content_sha256=envelope.content_sha256
        )
        if (
            not isinstance(receipt, WorldAvailabilityReceipt)
            or receipt.receipt_sha256 is None
            or receipt.subject.subject_id != envelope.artifact_id
            or receipt.subject.content_sha256 != envelope.content_sha256
            or persisted.identity != expected
            or relation.source != expected
        ):
            return cls(status="skipped", skip_reason="receipt_mismatch")
        linked = replace(
            relation,
            relation_id=None,
            content_sha256=None,
            source_refs=(*relation.source_refs, about_receipt_ref(receipt)),
        )
        return cls(status="linked", relation=linked, artifact_ref=expected)


__all__ = [
    "ABOUT_GRAPH_LINK_STATUSES",
    "ABOUT_GRAPH_SKIP_REASONS",
    "ABOUT_SIGNAL_ARTIFACT_KINDS",
    "KNOWLEDGE_ARTIFACT_ROW_VERSION",
    "KNOWLEDGE_ARTIFACT_SUBJECT_KIND",
    "AboutKnowledgeLink",
    "about_receipt_ref",
    "knowledge_artifact_content_sha256",
    "knowledge_artifact_id",
    "knowledge_artifact_ref_for_row",
    "knowledge_artifact_row",
    "knowledge_artifact_scope",
    "require_about_signal_kind",
    "require_ready_at",
]
