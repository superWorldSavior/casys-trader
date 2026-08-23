"""Read-only point-in-time World-context source over raw history + sidecar receipts.

Readiness is the sidecar receipt ``ready_at`` bounded by in-memory first-seen
time: ``effective_ready_at = max(receipt.ready_at, first_seen_at)``.  File
mtimes and semantic ``as_of`` are never treated as proof that an artifact was
available at the market cutoff.  Boot scan is eager, but each receipt is
stamped only after it has been read; a single pre-scan boot clock is not
applied to receipts discovered later.

Late, unproven, or post-cutoff rows remain visible as store/audit facts, but
the model-facing lookup collapses them to canonical missing
(``no_proven_artifact_at_cutoff``, ``proven=False``, no artifact metadata).
A restart is intentionally conservative for old cutoffs unless a canonical V2
episode already exists.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.situation import NewsMacroBrief
from trader.domain.world_context import (
    POLICY_CONTAMINATED_REASON,
    EntityRef,
    KnowledgeArtifact,
    SensorEvidence,
    assert_context_schema,
    neutral_missing_sensor_evidence,
    temporally_eligible,
)
from trader.domain.world_episode import parse_utc_timestamp
from trader.infrastructure.state_db._jsonl_store import read_jsonl_objects
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    default_utc_clock,
    is_availability_receipt,
    load_receipts,
    payload_sha256,
    receipt_dir,
    unwrap_history_payload,
    validate_availability_receipt,
)
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore


_POLICY_KEY_FRAGMENTS = (
    "candidate",
    "hotlist",
    "mandate",
    "rank",
    "attractiveness",
    "feedback",
    "policy",
    "decision",
    "portfolio",
)
_EXPLICIT_PUBLICATION_FIELDS = ("published_at", "publication_time", "published")
_MALFORMED = object()


def _aware_utc(value: datetime, *, field_name: str = "clock") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must return a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _receipt_fingerprint(receipt: Mapping[str, Any]) -> str:
    return payload_sha256(receipt)


def _parse_optional(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return parse_utc_timestamp(value, "timestamp")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_valid_until(value: object) -> datetime | object | None:
    if value is None or value == "":
        return None
    try:
        return parse_utc_timestamp(value, "valid_until")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _MALFORMED


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_keys(item)


def macro_payload_contaminated(payload: Mapping[str, Any]) -> bool:
    """Exclude policy/candidate/company-anchor leakage from the macro sensor."""

    try:
        assert_context_schema({"input_refs": payload.get("input_refs") or {}}, "macro.input_refs")
    except (TypeError, ValueError):
        return True
    for key in _walk_keys(payload.get("input_refs") or {}):
        compact = str(key).strip().lower()
        if any(fragment in compact for fragment in _POLICY_KEY_FRAGMENTS):
            return True
    for key in payload:
        compact = str(key).strip().lower()
        if any(fragment in compact for fragment in _POLICY_KEY_FRAGMENTS):
            return True
    return False


def _explicit_published_at(payload: Mapping[str, Any]) -> datetime | None:
    for name in _EXPLICIT_PUBLICATION_FIELDS:
        parsed = _parse_optional(payload.get(name))
        if parsed is not None:
            return parsed
    return None


class WorldContextReader:
    """Frozen, local-file adapter. No network and no LLM I/O.

    Late/unproven sidecar rows are audit facts on disk; lookups collapse them
    to canonical model-facing missing.  Eligibility uses the first-seen bound,
    never filesystem mtime, and never mutates sidecar receipts.
    """

    def __init__(
        self,
        *,
        news_store: NewsMacroBriefStore | None = None,
        company_store: CompanyIntelligenceStore | None = None,
        news_dir: str | Path | None = None,
        company_dir: str | Path | None = None,
        clock: UtcClock | None = None,
    ) -> None:
        if news_store is None and news_dir is not None:
            news_store = NewsMacroBriefStore(news_dir)
        if company_store is None and company_dir is not None:
            company_store = CompanyIntelligenceStore(company_dir)
        self.news_store = news_store
        self.company_store = company_store
        self._clock = clock or default_utc_clock
        self._first_seen_at: dict[str, datetime] = {}
        self._prime_existing_receipts()

    def lookup_macro(self, *, venue: str, cutoff_at: datetime | str) -> SensorEvidence:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        if self.news_store is None:
            return neutral_missing_sensor_evidence()
        records = [
            record
            for record in self._iter_news_rows()
            if str((record["payload"] or {}).get("venue") or "").strip() == venue
        ]
        return self._select_sensor(
            records,
            cutoff=cutoff,
            kind="news_macro",
            subject=EntityRef("venue", venue),
            schema_version="news_macro_brief.v1",
            artifact_id_field="brief_id",
            contamination=macro_payload_contaminated,
        )

    def lookup_company(self, *, symbol: str, cutoff_at: datetime | str) -> SensorEvidence:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        if self.company_store is None:
            return neutral_missing_sensor_evidence()
        records = [
            record
            for record in self._iter_company_rows(symbol)
            if str((record["payload"] or {}).get("symbol") or "").strip() == symbol
        ]
        return self._select_sensor(
            records,
            cutoff=cutoff,
            kind="company_intelligence",
            subject=EntityRef("instrument", symbol),
            schema_version="company_intelligence_brief.v1",
            artifact_id_field="brief_id",
            contamination=None,
        )

    def _iter_news_rows(self) -> Iterator[dict[str, Any]]:
        base = self.news_store.base_dir
        receipts_root = receipt_dir(base)
        for path in sorted(base.glob("????-??-??.jsonl")):
            if path.name.startswith("latest-"):
                continue
            yield from self._rows_from_path(
                path,
                receipt_path=receipts_root / path.name,
                scope=path.stem,
                history_path=str(path.relative_to(base)),
            )

    def _iter_company_rows(self, symbol: str) -> Iterator[dict[str, Any]]:
        path = self.company_store.history_path(symbol)
        yield from self._rows_from_path(
            path,
            receipt_path=self.company_store.receipt_path(symbol),
            scope=symbol,
            history_path=str(path.relative_to(self.company_store.base_dir)),
        )

    def _rows_from_path(
        self,
        path: Path,
        *,
        receipt_path: Path,
        scope: str,
        history_path: str,
    ) -> Iterator[dict[str, Any]]:
        receipts = load_receipts(receipt_path)
        for row in read_jsonl_objects(path):
            payload = dict(unwrap_history_payload(row))
            artifact_id = str(payload.get("brief_id") or "").strip()
            try:
                digest = payload_sha256(payload)
            except (TypeError, ValueError):
                digest = ""
            proven = False
            ready_at = None
            matched_receipt = None
            for receipt in receipts:
                candidate = validate_availability_receipt(
                    receipt,
                    payload,
                    expected_artifact_id=artifact_id,
                    expected_scope=scope,
                    expected_history_path=history_path,
                )
                if candidate is None:
                    continue
                proven = True
                first_seen = self._remember_receipt(receipt)
                ready_at = max(candidate, first_seen)
                matched_receipt = receipt
                break
            yield {
                "row": row,
                "payload": payload,
                "proven": proven,
                "ready_at": ready_at,
                "receipt": matched_receipt,
                "payload_sha256": digest,
            }

    def _prime_existing_receipts(self) -> None:
        roots: list[Path] = []
        if self.news_store is not None:
            roots.append(receipt_dir(self.news_store.base_dir))
        if self.company_store is not None:
            roots.append(receipt_dir(self.company_store.base_dir))
        for root in roots:
            try:
                paths = sorted(root.glob("*.jsonl"))
            except OSError:
                continue
            for path in paths:
                for receipt in load_receipts(path):
                    if is_availability_receipt(receipt):
                        self._remember_receipt(receipt)

    def _remember_receipt(self, receipt: Mapping[str, Any], *, seen_at: datetime | None = None) -> datetime:
        fingerprint = _receipt_fingerprint(receipt)
        existing = self._first_seen_at.get(fingerprint)
        if existing is not None:
            return existing
        stamped = seen_at if seen_at is not None else _aware_utc(self._clock(), field_name="clock")
        self._first_seen_at[fingerprint] = stamped
        return stamped

    def _select_sensor(
        self,
        records: list[dict[str, Any]],
        *,
        cutoff: datetime,
        kind: str,
        subject: EntityRef,
        schema_version: str,
        artifact_id_field: str,
        contamination: Any,
    ) -> SensorEvidence:
        if not records:
            return neutral_missing_sensor_evidence()

        eligible: list[dict[str, Any]] = []
        stale_records: list[dict[str, Any]] = []
        for record in records:
            payload = record["payload"]
            if record["proven"] is not True or record["ready_at"] is None:
                continue
            ready_at = record["ready_at"]
            if ready_at > cutoff:
                continue
            valid_until = _parse_valid_until(payload.get("valid_until"))
            if valid_until is _MALFORMED:
                continue
            if valid_until is not None and cutoff >= valid_until:
                stale_records.append(record)
                continue
            if not temporally_eligible(cutoff_at=cutoff, ready_at=ready_at, valid_until=valid_until):
                continue
            eligible.append(record)

        if eligible:
            chosen = max(
                eligible,
                key=lambda item: (item["ready_at"], str(item["payload"].get(artifact_id_field) or "")),
            )
            payload = chosen["payload"]
            artifact = self._artifact(
                kind=kind,
                payload=payload,
                ready_at=chosen["ready_at"],
                subject=subject,
                schema_version=schema_version,
                artifact_id_field=artifact_id_field,
            )
            if contamination is not None and contamination(payload):
                return SensorEvidence(
                    status="missing",
                    reason=POLICY_CONTAMINATED_REASON,
                    proven=True,
                    payload=payload,
                    artifact=artifact,
                )
            return SensorEvidence(
                status="complete",
                reason="sidecar_ready",
                proven=True,
                payload=payload,
                artifact=artifact,
            )
        if stale_records:
            chosen = max(
                stale_records,
                key=lambda item: (item["ready_at"], str(item["payload"].get(artifact_id_field) or "")),
            )
            payload = chosen["payload"]
            return SensorEvidence(
                status="stale",
                reason="valid_until_at_or_before_cutoff",
                proven=True,
                payload=payload,
                artifact=self._artifact(
                    kind=kind,
                    payload=payload,
                    ready_at=chosen["ready_at"],
                    subject=subject,
                    schema_version=schema_version,
                    artifact_id_field=artifact_id_field,
                ),
            )
        return neutral_missing_sensor_evidence()

    def _artifact(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        ready_at: datetime,
        subject: EntityRef,
        schema_version: str,
        artifact_id_field: str,
    ) -> KnowledgeArtifact:
        artifact_id = str(payload.get(artifact_id_field) or payload.get("brief_id") or kind)
        valid_until = _parse_optional(payload.get("valid_until"))
        occurred_at = _parse_optional(payload.get("as_of"))
        source_refs = payload.get("source_refs") or ()
        if not isinstance(source_refs, (list, tuple)):
            source_refs = ()
        return KnowledgeArtifact(
            kind=kind,
            artifact_id=artifact_id,
            subjects=(subject,),
            schema_version=schema_version,
            content_sha256=payload_sha256(dict(payload)),
            occurred_at=occurred_at,
            published_at=_explicit_published_at(payload),
            ingested_at=ready_at,
            ready_at=ready_at,
            valid_until=valid_until,
            source_refs=tuple(str(item) for item in source_refs if str(item).strip()),
        )


def parse_macro_brief(payload: Mapping[str, Any]) -> NewsMacroBrief | None:
    return NewsMacroBrief.from_mapping(payload)


def parse_company_brief(payload: Mapping[str, Any]) -> CompanyIntelligenceBrief | None:
    return CompanyIntelligenceBrief.from_mapping(payload)


__all__ = [
    "WorldContextReader",
    "macro_payload_contaminated",
    "parse_company_brief",
    "parse_macro_brief",
]
