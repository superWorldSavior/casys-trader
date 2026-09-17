"""Pure mapping + verified briefs → issuer-registry build.

Iterates mapping entries (the listing authority) and binds each to a verified
company brief. The brief ``exchange`` code only cross-checks the mapped MIC:
conflicts exclude the symbol, unknown codes degrade to mapping-only
resolution. Every exclusion is counted by reason; nothing is inferred and
nothing is silent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trader.domain.company.intelligence import CompanyIntelligenceBrief
from trader.domain.world_exchange_mics import mic_for_yahoo_exchange
from trader.domain.world_issuer_registry import IssuerEntry, IssuerRegistry, issuer_entity_id_for_listing
from trader.domain.world_scope import WorldScopeMapping

ISSUER_EXCLUSION_REASONS = frozenset(
    {"missing_brief", "brief_unreadable", "identity_unverified", "exchange_conflict"}
)
_MIC_PREFIX = "mic:"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


@dataclass(frozen=True)
class IssuerExclusion:
    """One mapping entry refused with its reason. Never retried silently."""

    market_venue: str
    symbol: str
    reason: str
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_venue", _required_text(self.market_venue, "market_venue"))
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        reason = _required_text(self.reason, "reason")
        if reason not in ISSUER_EXCLUSION_REASONS:
            allowed = ", ".join(sorted(ISSUER_EXCLUSION_REASONS))
            raise ValueError(f"exclusion reason must be one of: {allowed}")
        object.__setattr__(self, "reason", reason)
        detail = self.detail if isinstance(self.detail, str) else ""
        object.__setattr__(self, "detail", detail.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "market_venue": self.market_venue,
            "symbol": self.symbol,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class IssuerRegistryBuild:
    """Deterministic registry plus the exclusions that shaped it."""

    registry: IssuerRegistry
    excluded: tuple[IssuerExclusion, ...]

    @property
    def exclusion_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.excluded:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def index_briefs_by_symbol(
    briefs: Mapping[str, CompanyIntelligenceBrief | Mapping[str, Any]],
) -> dict[str, CompanyIntelligenceBrief | Mapping[str, Any]]:
    """Briefs keyed by case-folded symbol. Ticker identity is case-insensitive;
    the first key in sorted order wins a fold collision (deterministic)."""

    if not isinstance(briefs, Mapping):
        raise TypeError("briefs must be a mapping of symbol to brief")
    indexed: dict[str, CompanyIntelligenceBrief | Mapping[str, Any]] = {}
    for key in sorted(briefs, key=str):
        indexed.setdefault(str(key).strip().upper(), briefs[key])
    return indexed


def _parse_brief(value: Any) -> CompanyIntelligenceBrief | None:
    if isinstance(value, CompanyIntelligenceBrief):
        return value
    if isinstance(value, Mapping):
        try:
            return CompanyIntelligenceBrief.from_mapping(value)
        except (TypeError, ValueError):
            return None
    raise TypeError("briefs must map symbols to CompanyIntelligenceBrief or mappings")


def build_issuer_registry(
    briefs: Mapping[str, CompanyIntelligenceBrief | Mapping[str, Any]],
    mapping: WorldScopeMapping,
    *,
    registry_id: str = "issuer_registry.v1",
) -> IssuerRegistryBuild:
    """Bind verified briefs to mapped listings. Pure; exclusions counted."""

    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    indexed = index_briefs_by_symbol(briefs)
    entries: dict[str, IssuerEntry] = {}
    excluded: list[IssuerExclusion] = []
    ordered = sorted(mapping.entries, key=lambda item: (item.anchor.market_venue, item.anchor.instrument))
    for entry in ordered:
        venue = entry.anchor.market_venue
        symbol = entry.anchor.instrument
        raw = indexed.get(symbol.strip().upper())
        if raw is None:
            excluded.append(IssuerExclusion(market_venue=venue, symbol=symbol, reason="missing_brief"))
            continue
        brief = _parse_brief(raw)
        if brief is None:
            excluded.append(IssuerExclusion(market_venue=venue, symbol=symbol, reason="brief_unreadable"))
            continue
        if brief.issuer_identity.identity_status != "verified":
            excluded.append(
                IssuerExclusion(
                    market_venue=venue,
                    symbol=symbol,
                    reason="identity_unverified",
                    detail=brief.issuer_identity.identity_status,
                )
            )
            continue
        venue_entity_id = entry.venue.entity_id
        if not venue_entity_id.startswith(_MIC_PREFIX):
            excluded.append(
                IssuerExclusion(
                    market_venue=venue, symbol=symbol, reason="exchange_conflict", detail=venue_entity_id
                )
            )
            continue
        mic = venue_entity_id[len(_MIC_PREFIX):]
        expected = mic_for_yahoo_exchange(brief.issuer_identity.exchange)
        if expected is not None and expected != mic:
            excluded.append(
                IssuerExclusion(
                    market_venue=venue,
                    symbol=symbol,
                    reason="exchange_conflict",
                    detail=f"brief={expected} mapping={mic}",
                )
            )
            continue
        node_id = f"instrument:{venue_entity_id}:symbol:{symbol}"
        issuer_entry = IssuerEntry(
            instrument_node_id=node_id,
            market_venue=venue,
            symbol=symbol,
            mic=mic,
            issuer_entity_id=issuer_entity_id_for_listing(mic, symbol),
            identity_status="verified",
            brief_as_of=brief.as_of,
            source_refs=(brief.brief_id,),
            resolution_method="mapping_plus_exchange" if expected == mic else "mapping_only",
        )
        previous = entries.get(node_id)
        if previous is not None and previous != issuer_entry:
            raise ValueError(f"conflicting issuer entries for instrument {node_id}")
        entries[node_id] = issuer_entry
    registry = IssuerRegistry(registry_id=registry_id, entries=entries)
    return IssuerRegistryBuild(registry=registry, excluded=tuple(excluded))


__all__ = [
    "ISSUER_EXCLUSION_REASONS",
    "IssuerExclusion",
    "IssuerRegistryBuild",
    "build_issuer_registry",
    "index_briefs_by_symbol",
]
