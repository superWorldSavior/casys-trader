"""Pure contracts for longitudinal company intelligence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping

EvidenceLabel = Literal[
    "fact_source_reported",
    "fact_provider_standardized",
    "derived_calculation",
    "issuer_management_claim",
    "analyst_interpretation",
    "missing_required_source",
    "stale_source",
    "contradicted_source",
    "unknown",
]
Confidence = Literal["high", "medium", "low"]
Freshness = Literal["fresh", "stale", "missing", "unknown"]
CoverageStatus = Literal["full", "partial", "missing", "unsupported"]
SelectionPosture = Literal[
    "supports_selection",
    "neutral",
    "argues_against",
    "insufficient_evidence",
]
CompanyThesisStatus = Literal[
    "strengthening",
    "intact",
    "watch",
    "impaired",
    "broken",
    "untested",
]
SecurityReadiness = Literal["not_evaluated", "conditional", "not_decision_grade"]
AnalysisDepth = Literal["screen", "deep"]

MAX_POINT_CHARS = 280
MAX_SUMMARY_CHARS = 600
MAX_POINTS_PER_SECTION = 8
MAX_METRICS_PER_SECTION = 16
MAX_SOURCE_REFS = 8
MAX_REASONS = 5
MAX_OPEN_QUESTIONS = 8

_EVIDENCE_LABELS = {
    "fact_source_reported",
    "fact_provider_standardized",
    "derived_calculation",
    "issuer_management_claim",
    "analyst_interpretation",
    "missing_required_source",
    "stale_source",
    "contradicted_source",
    "unknown",
}
_CONFIDENCE = {"high", "medium", "low"}
_FRESHNESS = {"fresh", "stale", "missing", "unknown"}
_COVERAGE = {"full", "partial", "missing", "unsupported"}
_SELECTION = {
    "supports_selection",
    "neutral",
    "argues_against",
    "insufficient_evidence",
}
_THESIS_STATUS = {"strengthening", "intact", "watch", "impaired", "broken", "untested"}
_SECURITY_READINESS = {"not_evaluated", "conditional", "not_decision_grade"}
_DEPTHS = {"screen", "deep"}


def _clean_text(value: Any, *, max_chars: int | None = None) -> str:
    text = str(value or "").strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[: max(0, max_chars - 3)].rstrip() + "..."
    return text


def _clean_string_tuple(value: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _clean_text(raw)
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
        if len(result) >= limit:
            break
    return tuple(result)


def _clean_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_datetime(value: Any) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class IssuerIdentity:
    issuer_name: str
    instrument_type: str = "equity"
    exchange: str | None = None
    venue: str | None = None
    currency: str | None = None
    external_ids: Mapping[str, str] | None = None
    identity_status: str = "unverified"

    @classmethod
    def from_mapping(cls, raw: Any) -> "IssuerIdentity":
        payload = _clean_mapping(raw)
        external_ids = {
            _clean_text(key): _clean_text(value)
            for key, value in _clean_mapping(payload.get("external_ids")).items()
            if _clean_text(key) and _clean_text(value)
        }
        return cls(
            issuer_name=_clean_text(payload.get("issuer_name") or payload.get("legal_name"), max_chars=180),
            instrument_type=_clean_text(payload.get("instrument_type")) or "equity",
            exchange=_clean_text(payload.get("exchange")) or None,
            venue=_clean_text(payload.get("venue")) or None,
            currency=_clean_text(payload.get("currency")) or None,
            external_ids=external_ids,
            identity_status=_clean_text(payload.get("identity_status")) or "unverified",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issuer_name": self.issuer_name,
            "instrument_type": self.instrument_type,
            "exchange": self.exchange,
            "venue": self.venue,
            "currency": self.currency,
            "external_ids": dict(self.external_ids or {}),
            "identity_status": self.identity_status,
        }


@dataclass(frozen=True)
class CompanyEvidenceItem:
    item_id: str
    symbol: str
    provider: str
    kind: str
    source_ref: str
    source_name: str
    as_of: str
    payload: Mapping[str, Any]
    content_hash: str
    source_url: str | None = None
    period_end: str | None = None
    currency: str | None = None
    evidence_label: EvidenceLabel = "fact_provider_standardized"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CompanyEvidenceItem | None":
        item_id = _clean_text(raw.get("item_id"))
        symbol = _clean_text(raw.get("symbol"))
        source_ref = _clean_text(raw.get("source_ref"))
        as_of = _clean_text(raw.get("as_of") or raw.get("filed_at") or raw.get("fetched_at"))
        if not item_id or not symbol or not source_ref or not as_of:
            return None
        payload = _clean_mapping(raw.get("payload"))
        content_hash = _clean_text(raw.get("content_hash")) or _content_hash(payload)
        label = _clean_text(raw.get("evidence_label")) or "fact_provider_standardized"
        if label not in _EVIDENCE_LABELS:
            label = "unknown"
        return cls(
            item_id=item_id,
            symbol=symbol,
            provider=_clean_text(raw.get("provider")) or "unknown",
            kind=_clean_text(raw.get("kind")) or "unknown",
            source_ref=source_ref,
            source_name=_clean_text(raw.get("source_name")) or _clean_text(raw.get("provider")) or "unknown",
            as_of=as_of,
            payload=payload,
            content_hash=content_hash,
            source_url=_clean_text(raw.get("source_url")) or None,
            period_end=_clean_text(raw.get("period_end")) or None,
            currency=_clean_text(raw.get("currency")) or None,
            evidence_label=label,  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "symbol": self.symbol,
            "provider": self.provider,
            "kind": self.kind,
            "source_ref": self.source_ref,
            "source_name": self.source_name,
            "as_of": self.as_of,
            "payload": dict(self.payload),
            "content_hash": self.content_hash,
            "source_url": self.source_url,
            "period_end": self.period_end,
            "currency": self.currency,
            "evidence_label": self.evidence_label,
        }


@dataclass(frozen=True)
class CompanyEvidenceSnapshot:
    symbol: str
    as_of: str
    identity: IssuerIdentity
    items: tuple[CompanyEvidenceItem, ...]
    coverage: Mapping[str, Any]
    input_signature: str

    @classmethod
    def build(
        cls,
        *,
        symbol: str,
        as_of: str,
        identity: IssuerIdentity,
        items: Iterable[CompanyEvidenceItem],
        coverage: Mapping[str, Any] | None = None,
    ) -> "CompanyEvidenceSnapshot":
        normalized_symbol = _clean_text(symbol)
        normalized_items = tuple(item for item in items if item.symbol == normalized_symbol)
        payload = {
            "symbol": normalized_symbol,
            "identity": identity.to_dict(),
            # A source probe timestamp or provider-local item id must not force
            # a new analysis when the normalized evidence itself is unchanged.
            "items": [
                {
                    "provider": item.provider,
                    "kind": item.kind,
                    "content_hash": item.content_hash,
                    "period_end": item.period_end,
                }
                for item in normalized_items
            ],
            "coverage": dict(coverage or {}),
        }
        return cls(
            symbol=normalized_symbol,
            as_of=_clean_text(as_of),
            identity=identity,
            items=normalized_items,
            coverage=dict(coverage or {}),
            input_signature=build_company_input_signature(payload),
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CompanyEvidenceSnapshot | None":
        symbol = _clean_text(raw.get("symbol"))
        as_of = _clean_text(raw.get("as_of"))
        if not symbol or not as_of:
            return None
        identity = IssuerIdentity.from_mapping(raw.get("identity") or raw.get("issuer_identity"))
        items = tuple(
            item
            for candidate in raw.get("items") or ()
            if isinstance(candidate, Mapping)
            if (item := CompanyEvidenceItem.from_mapping(candidate)) is not None and item.symbol == symbol
        )
        coverage = _clean_mapping(raw.get("coverage"))
        signature = _clean_text(raw.get("input_signature")) or build_company_input_signature(
            {
                "symbol": symbol,
                "identity": identity.to_dict(),
                "items": [
                    {
                        "provider": item.provider,
                        "kind": item.kind,
                        "content_hash": item.content_hash,
                        "period_end": item.period_end,
                    }
                    for item in items
                ],
                "coverage": coverage,
            }
        )
        return cls(symbol, as_of, identity, items, coverage, signature)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "identity": self.identity.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "coverage": dict(self.coverage),
            "input_signature": self.input_signature,
        }

    def source_catalog(self) -> dict[str, str]:
        return {item.source_ref: item.source_name for item in self.items}


@dataclass(frozen=True)
class SectionFreshness:
    source_as_of: str | None = None
    verified_at: str | None = None
    refresh_after: str | None = None
    freshness: Freshness = "unknown"
    reason: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "SectionFreshness":
        payload = _clean_mapping(raw)
        status = _clean_text(payload.get("freshness")) or "unknown"
        if status not in _FRESHNESS:
            status = "unknown"
        return cls(
            source_as_of=_clean_text(payload.get("source_as_of")) or None,
            verified_at=_clean_text(payload.get("verified_at")) or None,
            refresh_after=_clean_text(payload.get("refresh_after")) or None,
            freshness=status,  # type: ignore[arg-type]
            reason=_clean_text(payload.get("reason"), max_chars=MAX_POINT_CHARS) or None,
        )

    def at(self, moment: datetime | str | None) -> Freshness:
        if self.freshness in {"missing", "stale"}:
            return self.freshness
        at = moment if isinstance(moment, datetime) else _parse_datetime(moment)
        deadline = _parse_datetime(self.refresh_after)
        if at is None or deadline is None:
            return self.freshness
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return "stale" if at >= deadline else ("fresh" if self.freshness == "unknown" else self.freshness)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_as_of": self.source_as_of,
            "verified_at": self.verified_at,
            "refresh_after": self.refresh_after,
            "freshness": self.freshness,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SourcedCompanyPoint:
    point: str
    source_refs: tuple[str, ...]
    evidence_label: EvidenceLabel = "analyst_interpretation"
    confidence: Confidence = "medium"
    period: str | None = None
    horizon: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "SourcedCompanyPoint | None":
        payload = _clean_mapping(raw)
        point = _clean_text(payload.get("point"), max_chars=MAX_POINT_CHARS)
        if not point:
            return None
        label = _clean_text(payload.get("evidence_label")) or "analyst_interpretation"
        if label not in _EVIDENCE_LABELS:
            label = "unknown"
        confidence = _clean_text(payload.get("confidence")) or "medium"
        if confidence not in _CONFIDENCE:
            confidence = "low"
        return cls(
            point=point,
            source_refs=_clean_string_tuple(payload.get("source_refs"), limit=MAX_SOURCE_REFS),
            evidence_label=label,  # type: ignore[arg-type]
            confidence=confidence,  # type: ignore[arg-type]
            period=_clean_text(payload.get("period")) or None,
            horizon=_clean_text(payload.get("horizon")) or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "source_refs": list(self.source_refs),
            "evidence_label": self.evidence_label,
            "confidence": self.confidence,
            "period": self.period,
            "horizon": self.horizon,
        }


def _points(raw: Any, *, limit: int = MAX_POINTS_PER_SECTION) -> tuple[SourcedCompanyPoint, ...]:
    if not isinstance(raw, list):
        return ()
    result: list[SourcedCompanyPoint] = []
    for candidate in raw:
        point = SourcedCompanyPoint.from_mapping(candidate)
        if point is not None:
            result.append(point)
        if len(result) >= limit:
            break
    return tuple(result)


@dataclass(frozen=True)
class CompanySection:
    summary: str = ""
    source_refs: tuple[str, ...] = ()
    points: tuple[SourcedCompanyPoint, ...] = ()
    metrics: tuple[dict[str, Any], ...] = ()
    freshness: SectionFreshness = SectionFreshness()

    @classmethod
    def from_mapping(cls, raw: Any) -> "CompanySection":
        payload = _clean_mapping(raw)
        metrics = tuple(
            dict(item)
            for item in (payload.get("metrics") or ())
            if isinstance(item, Mapping)
        )[:MAX_METRICS_PER_SECTION]
        return cls(
            summary=_clean_text(payload.get("summary"), max_chars=MAX_SUMMARY_CHARS),
            source_refs=_clean_string_tuple(payload.get("source_refs"), limit=MAX_SOURCE_REFS),
            points=_points(payload.get("points")),
            metrics=metrics,
            freshness=SectionFreshness.from_mapping(payload.get("freshness")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "source_refs": list(self.source_refs),
            "points": [point.to_dict() for point in self.points],
            "metrics": [dict(metric) for metric in self.metrics],
            "freshness": self.freshness.to_dict(),
        }


@dataclass(frozen=True)
class CompanyThesis:
    status: CompanyThesisStatus = "untested"
    summary: str = ""
    pillars: tuple[SourcedCompanyPoint, ...] = ()
    confirming_evidence: tuple[SourcedCompanyPoint, ...] = ()
    disconfirming_evidence: tuple[SourcedCompanyPoint, ...] = ()
    kill_criteria: tuple[SourcedCompanyPoint, ...] = ()
    next_proof_points: tuple[SourcedCompanyPoint, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Any) -> "CompanyThesis":
        payload = _clean_mapping(raw)
        status = _clean_text(payload.get("status")) or "untested"
        if status not in _THESIS_STATUS:
            status = "untested"
        return cls(
            status=status,  # type: ignore[arg-type]
            summary=_clean_text(payload.get("summary"), max_chars=MAX_SUMMARY_CHARS),
            pillars=_points(payload.get("pillars")),
            confirming_evidence=_points(payload.get("confirming_evidence")),
            disconfirming_evidence=_points(payload.get("disconfirming_evidence")),
            kill_criteria=_points(payload.get("kill_criteria")),
            next_proof_points=_points(payload.get("next_proof_points")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "pillars": [point.to_dict() for point in self.pillars],
            "confirming_evidence": [point.to_dict() for point in self.confirming_evidence],
            "disconfirming_evidence": [point.to_dict() for point in self.disconfirming_evidence],
            "kill_criteria": [point.to_dict() for point in self.kill_criteria],
            "next_proof_points": [point.to_dict() for point in self.next_proof_points],
        }


@dataclass(frozen=True)
class SelectionView:
    posture: SelectionPosture = "insufficient_evidence"
    confidence: Confidence = "low"
    reasons: tuple[str, ...] = ()
    scope: str = "universe_surveillance"

    @classmethod
    def from_mapping(cls, raw: Any) -> "SelectionView":
        payload = _clean_mapping(raw)
        posture = _clean_text(payload.get("posture")) or "insufficient_evidence"
        if posture not in _SELECTION:
            posture = "insufficient_evidence"
        confidence = _clean_text(payload.get("confidence")) or "low"
        if confidence not in _CONFIDENCE:
            confidence = "low"
        return cls(
            posture=posture,  # type: ignore[arg-type]
            confidence=confidence,  # type: ignore[arg-type]
            reasons=tuple(
                _clean_text(reason, max_chars=MAX_POINT_CHARS)
                for reason in _clean_string_tuple(payload.get("reasons"), limit=MAX_REASONS)
            ),
            scope="universe_surveillance",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "posture": self.posture,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "scope": self.scope,
        }


@dataclass(frozen=True)
class CompanyIntelligenceBrief:
    brief_id: str
    symbol: str
    as_of: str
    input_signature: str
    depth: AnalysisDepth
    issuer_identity: IssuerIdentity
    coverage: Mapping[str, Any]
    business: CompanySection
    financial_snapshot: CompanySection
    earnings_and_guidance: CompanySection
    company_thesis: CompanyThesis
    catalysts: tuple[SourcedCompanyPoint, ...]
    risks: tuple[SourcedCompanyPoint, ...]
    open_questions: tuple[SourcedCompanyPoint, ...]
    selection_view: SelectionView
    security_readiness: SecurityReadiness
    source_refs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CompanyIntelligenceBrief | None":
        symbol = _clean_text(raw.get("symbol"))
        as_of = _clean_text(raw.get("as_of"))
        signature = _clean_text(raw.get("input_signature") or raw.get("source_signature"))
        depth = _clean_text(raw.get("depth")) or "screen"
        if not symbol or not as_of or not signature or depth not in _DEPTHS:
            return None
        readiness = _clean_text(raw.get("security_readiness")) or "not_evaluated"
        if readiness not in _SECURITY_READINESS:
            readiness = "not_decision_grade"
        coverage = _clean_mapping(raw.get("coverage"))
        status = _clean_text(coverage.get("status")) or "partial"
        if status not in _COVERAGE:
            coverage["status"] = "partial"
        brief_id = _clean_text(raw.get("brief_id")) or company_brief_id(symbol, signature, depth=depth)
        return cls(
            brief_id=brief_id,
            symbol=symbol,
            as_of=as_of,
            input_signature=signature,
            depth=depth,  # type: ignore[arg-type]
            issuer_identity=IssuerIdentity.from_mapping(raw.get("issuer_identity") or raw.get("identity")),
            coverage=coverage,
            business=CompanySection.from_mapping(raw.get("business")),
            financial_snapshot=CompanySection.from_mapping(raw.get("financial_snapshot")),
            earnings_and_guidance=CompanySection.from_mapping(raw.get("earnings_and_guidance")),
            company_thesis=CompanyThesis.from_mapping(raw.get("company_thesis")),
            catalysts=_points(raw.get("catalysts")),
            risks=_points(raw.get("risks")),
            open_questions=_points(raw.get("open_questions"), limit=MAX_OPEN_QUESTIONS),
            selection_view=SelectionView.from_mapping(raw.get("selection_view") or raw.get("selection_assessment")),
            security_readiness=readiness,  # type: ignore[arg-type]
            source_refs=_clean_string_tuple(raw.get("source_refs"), limit=MAX_SOURCE_REFS * 4),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "brief_id": self.brief_id,
            "symbol": self.symbol,
            "as_of": self.as_of,
            "input_signature": self.input_signature,
            "depth": self.depth,
            "issuer_identity": self.issuer_identity.to_dict(),
            "coverage": dict(self.coverage),
            "business": self.business.to_dict(),
            "financial_snapshot": self.financial_snapshot.to_dict(),
            "earnings_and_guidance": self.earnings_and_guidance.to_dict(),
            "company_thesis": self.company_thesis.to_dict(),
            "catalysts": [point.to_dict() for point in self.catalysts],
            "risks": [point.to_dict() for point in self.risks],
            "open_questions": [point.to_dict() for point in self.open_questions],
            "selection_view": self.selection_view.to_dict(),
            "security_readiness": self.security_readiness,
            "source_refs": list(self.source_refs),
        }

    def ref(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "brief_id": self.brief_id,
            "as_of": self.as_of,
            "input_signature": self.input_signature,
            "depth": self.depth,
        }

    def restrict_sources(self, allowed_refs: Iterable[str]) -> "CompanyIntelligenceBrief":
        allowed = {str(ref).strip() for ref in allowed_refs if str(ref).strip()}

        def filter_point(point: SourcedCompanyPoint) -> SourcedCompanyPoint | None:
            refs = tuple(ref for ref in point.source_refs if ref in allowed)
            return replace(point, source_refs=refs) if refs else None

        def filter_points(points: Iterable[SourcedCompanyPoint]) -> tuple[SourcedCompanyPoint, ...]:
            return tuple(filtered for point in points if (filtered := filter_point(point)) is not None)

        def filter_section(section: CompanySection) -> CompanySection:
            refs = tuple(ref for ref in section.source_refs if ref in allowed)
            summary = section.summary if refs else ""
            return replace(section, summary=summary, source_refs=refs, points=filter_points(section.points))

        thesis = replace(
            self.company_thesis,
            pillars=filter_points(self.company_thesis.pillars),
            confirming_evidence=filter_points(self.company_thesis.confirming_evidence),
            disconfirming_evidence=filter_points(self.company_thesis.disconfirming_evidence),
            kill_criteria=filter_points(self.company_thesis.kill_criteria),
            next_proof_points=filter_points(self.company_thesis.next_proof_points),
        )
        return replace(
            self,
            business=filter_section(self.business),
            financial_snapshot=filter_section(self.financial_snapshot),
            earnings_and_guidance=filter_section(self.earnings_and_guidance),
            company_thesis=thesis,
            catalysts=filter_points(self.catalysts),
            risks=filter_points(self.risks),
            open_questions=filter_points(self.open_questions),
            source_refs=tuple(ref for ref in self.source_refs if ref in allowed),
        )

    def freshness_status(self, at: datetime | str | None) -> str:
        statuses = [
            self.business.freshness.at(at),
            self.financial_snapshot.freshness.at(at),
            self.earnings_and_guidance.freshness.at(at),
        ]
        present = {status for status in statuses if status != "missing"}
        if not present:
            return "missing"
        if present == {"stale"}:
            return "stale"
        if "stale" in present:
            return "mixed"
        if "fresh" in present:
            return "fresh"
        return "unknown"


def build_company_input_signature(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def company_brief_id(symbol: str, input_signature: str, *, depth: str = "screen") -> str:
    digest = hashlib.sha256(f"{symbol}|{input_signature}|{depth}".encode()).hexdigest()
    return f"company_micro:v1:{symbol}:{digest}"


def _content_hash(payload: Any) -> str:
    return "sha256:" + build_company_input_signature(payload)
