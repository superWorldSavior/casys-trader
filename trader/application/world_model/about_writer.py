"""Forward writer: fresh briefs/notes → artifacts + linked ABOUT relations.

One sweep writes everything new and skips everything stored, so callers stay
stateless (no cursor file to maintain): artifact ids are deterministic from
source keys, store appends no-op on identical content, and graph relation
appends no-op on identical events. Reruns are resume-safe; same id with
different content is a conflict that fails loud.

Ordering per sweep:

1. publish the extended ontology first (self-synchronizing tip: every
   publisher derives the same mapping + registry + catalog function, so
   concurrent publishers converge instead of oscillating);
2. bridge drafts against the fresh tip as witness, stamped with the stable
   family id (no per-generation re-stamp);
3. append artifacts (reuse attested corpus rows when present);
4. link each relation to its store receipt and assert it to the graph.

Relations pin the stable family id, never the tip; artifacts never change
(content-addressed, generation-independent). Callers must derive from FRESH
inputs on every sweep: a stale registry would derive a stale id and
supersede the tip backwards. News selection keeps live windows only — expired
notes could never hydrate (window + record-time receipt gates).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from trader.application.world_model.about_bridge import (
    AboutBridgeBuild,
    build_company_about,
    build_news_about,
)
from trader.application.world_model.graph_ports import (
    KnowledgeArtifactJoinMapping,
    WorldGraphLedger,
    WorldKnowledgeLedger,
)
from trader.application.world_model.ontology_bootstrap import WorldOntologyAttestation
from trader.domain.company.intelligence import CompanyIntelligenceBrief
from trader.domain.world_availability import PersistedWorldRef
from trader.domain.world_episode import parse_utc_timestamp
from trader.domain.world_family_catalog import FamilyCatalog
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelationAsserted,
    WorldOntologyRevision,
)
from trader.domain.world_issuer_registry import IssuerRegistry
from trader.domain.world_knowledge import AboutKnowledgeLink
from trader.domain.world_scope import WorldScopeMapping


@dataclass(frozen=True)
class AboutWriteReport:
    """Counts for one sweep. Skips are impossible by construction (raise)."""

    revision_id: str
    drafts: int
    relations_linked: int
    artifacts_appended: int
    artifacts_reused: int
    excluded: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "drafts": self.drafts,
            "relations_linked": self.relations_linked,
            "artifacts_appended": self.artifacts_appended,
            "artifacts_reused": self.artifacts_reused,
            "excluded": dict(self.excluded),
        }


def _now_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime or None")
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _valid_until_ts(note: Mapping[str, Any]) -> float | None:
    raw = note.get("valid_until")
    if not raw:
        return None
    try:
        return parse_utc_timestamp(raw, "valid_until").timestamp()
    except (TypeError, ValueError):
        return None


def select_live_notes(
    notes: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[Mapping[str, Any]]:
    """Notes whose window may still hydrate. Unparseable windows are kept.

    Expired notes are dropped: they could never hydrate at a forward cutoff
    (window gate) nor historically (record-time receipt gate), so persisting
    them would only burn store scans.
    """

    if isinstance(notes, (str, bytes, bytearray)):
        raise TypeError("notes must be a sequence of mappings")
    now_ts = _now_utc(now).timestamp()
    selected: list[Mapping[str, Any]] = []
    for note in notes:
        if not isinstance(note, Mapping):
            raise TypeError("notes must be a sequence of mappings")
        until = _valid_until_ts(note)
        if until is not None and until < now_ts:
            continue
        selected.append(note)
    return selected


class AboutWriterService:
    """Idempotent sweep writer. Holds ports, never stores or files."""

    def __init__(
        self,
        *,
        graph: WorldGraphLedger,
        knowledge: WorldKnowledgeLedger,
        ontology: WorldOntologyAttestation,
    ) -> None:
        if not isinstance(ontology, WorldOntologyAttestation):
            raise TypeError("ontology must be WorldOntologyAttestation")
        self._graph = graph
        self._knowledge = knowledge
        self._ontology = ontology

    def _published_tip(self) -> WorldOntologyRevision:
        self._ontology.ensure_published()
        return self._ontology.expected_revision()

    def _write_drafts(
        self, build: AboutBridgeBuild, revision: WorldOntologyRevision
    ) -> AboutWriteReport:
        revision_id = str(revision.revision_id)
        known: KnowledgeArtifactJoinMapping = dict(self._knowledge.load_corpus())
        appended = 0
        reused = 0
        linked = 0
        for draft in build.drafts:
            joined = known.get(draft.envelope.artifact_id)
            if joined is None:
                persisted = self._knowledge.append_artifact(draft.envelope, draft.signal)
                receipt = persisted.receipt
                appended += 1
            else:
                envelope, signal, receipt = joined
                if envelope.content_sha256 != draft.envelope.content_sha256:
                    raise ValueError(
                        f"artifact {draft.envelope.artifact_id} already stored with different content"
                    )
                persisted = PersistedWorldRef(
                    identity=KnowledgeArtifactRef(
                        artifact_id=draft.envelope.artifact_id,
                        content_sha256=draft.envelope.content_sha256,
                    ),
                    receipt=receipt,
                )
                reused += 1
            for relation in draft.relations:
                link = AboutKnowledgeLink.from_persisted(relation, draft.envelope, persisted)
                if link.status != "linked" or link.relation is None:
                    raise ValueError(
                        f"link failed for {draft.envelope.artifact_id}: {link.skip_reason}"
                    )
                self._graph.append_knowledge_relation_event(
                    KnowledgeWorldRelationAsserted(relation=link.relation)
                )
                linked += 1
            known[draft.envelope.artifact_id] = (draft.envelope, draft.signal, receipt)
        return AboutWriteReport(
            revision_id=revision_id,
            drafts=len(build.drafts),
            relations_linked=linked,
            artifacts_appended=appended,
            artifacts_reused=reused,
            excluded=MappingProxyType(dict(build.exclusion_counts)),
        )

    def write_news(
        self,
        notes: Sequence[Mapping[str, Any]],
        *,
        mapping: WorldScopeMapping,
        select_live: bool = True,
        now: datetime | None = None,
    ) -> AboutWriteReport:
        """Bridge + append + link news notes. Pure sweep, no cursor."""

        if not isinstance(mapping, WorldScopeMapping):
            raise TypeError("mapping must be WorldScopeMapping")
        revision = self._published_tip()
        selected = select_live_notes(notes, now=now) if select_live else list(notes)
        build = build_news_about(selected, mapping, ontology_revision=revision.revision_id)
        return self._write_drafts(build, revision)

    def write_company(
        self,
        briefs: Mapping[str, CompanyIntelligenceBrief | Mapping[str, Any]],
        *,
        registry: IssuerRegistry,
        catalog: FamilyCatalog,
    ) -> AboutWriteReport:
        """Bridge + append + link company briefs. Pure sweep, no cursor."""

        if not isinstance(registry, IssuerRegistry):
            raise TypeError("registry must be IssuerRegistry")
        if not isinstance(catalog, FamilyCatalog):
            raise TypeError("catalog must be FamilyCatalog")
        revision = self._published_tip()
        build = build_company_about(registry, briefs, catalog, ontology_revision=revision.revision_id)
        return self._write_drafts(build, revision)


__all__ = [
    "AboutWriteReport",
    "AboutWriterService",
    "select_live_notes",
]
