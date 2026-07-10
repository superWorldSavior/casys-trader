"""Pure per-symbol mandate contracts emitted by universe composition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

MandateStatus = Literal["prepared", "active", "fallback"]


@dataclass(frozen=True)
class SymbolMandate:
    symbol: str
    why_selected: str
    role: str
    posture: str
    allowed_sides: tuple[str, ...] = ()
    family_context: Mapping[str, Any] | None = None
    company_context: Mapping[str, Any] | None = None
    company_brief_ref: Mapping[str, str] | None = None
    confidence: str = "unknown"

    @classmethod
    def from_mapping(cls, symbol: str, raw: Mapping[str, Any]) -> "SymbolMandate":
        allowed = tuple(
            dict.fromkeys(
                str(side).strip()
                for side in raw.get("allowed_sides") or ()
                if str(side).strip() in {"long", "short"}
            )
        )
        company_context = raw.get("company_context")
        brief_ref = raw.get("company_brief_ref")
        return cls(
            symbol=str(symbol).strip(),
            why_selected=str(raw.get("why_selected") or "").strip()[:500],
            role=str(raw.get("role") or "monitor").strip()[:80],
            posture=str(raw.get("posture") or "neutral").strip()[:120],
            allowed_sides=allowed,
            family_context=dict(raw.get("family_context") or {}),
            company_context=dict(company_context) if isinstance(company_context, Mapping) else None,
            company_brief_ref=dict(brief_ref) if isinstance(brief_ref, Mapping) else None,
            confidence=str(raw.get("confidence") or "unknown").strip()[:40],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "why_selected": self.why_selected,
            "role": self.role,
            "posture": self.posture,
            "allowed_sides": list(self.allowed_sides),
            "family_context": dict(self.family_context or {}),
            "company_context": dict(self.company_context or {}),
            "company_brief_ref": dict(self.company_brief_ref or {}),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class UniverseMandate:
    mandate_id: str
    candidate_scope_id: str
    venue: str
    agent_run_id: str
    as_of: str
    valid_until: str | None
    status: MandateStatus
    symbols: Mapping[str, SymbolMandate]
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mandate_id": self.mandate_id,
            "candidate_scope_id": self.candidate_scope_id,
            "venue": self.venue,
            "agent_run_id": self.agent_run_id,
            "as_of": self.as_of,
            "valid_until": self.valid_until,
            "status": self.status,
            "symbols": {symbol: mandate.to_dict() for symbol, mandate in self.symbols.items()},
            "fallback_reason": self.fallback_reason,
        }


__all__ = ["MandateStatus", "SymbolMandate", "UniverseMandate"]
