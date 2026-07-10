"""Composition of venue-specific company evidence providers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from trader.application.analyst.company_micro import CompanyEvidenceProvider
from trader.domain.company import CompanyEvidenceItem, CompanyEvidenceSnapshot, IssuerIdentity


class CompositeCompanyEvidenceProvider:
    def __init__(
        self,
        providers: Iterable[CompanyEvidenceProvider],
        *,
        issuer_names: Mapping[str, str] | None = None,
    ) -> None:
        self._providers = tuple(providers)
        self._issuer_names = {str(key): str(value) for key, value in (issuer_names or {}).items()}

    def collect(self, *, symbol: str, as_of: str) -> CompanyEvidenceSnapshot:
        snapshots: list[CompanyEvidenceSnapshot] = []
        provider_errors: list[str] = []
        for provider in self._providers:
            try:
                snapshot = provider.collect(symbol=symbol, as_of=as_of)
            except Exception as exc:  # noqa: BLE001 - one provider must not erase other evidence
                provider_errors.append(f"{provider.__class__.__name__}:{exc.__class__.__name__}")
                continue
            if snapshot is not None and snapshot.symbol == symbol:
                snapshots.append(snapshot)

        identity = _merge_identity(symbol, snapshots, issuer_names=self._issuer_names)
        items = _dedupe_items(item for snapshot in snapshots for item in snapshot.items)
        kinds = {item.kind for item in items}
        if not items:
            status = "missing"
        elif "profile" in kinds and kinds.intersection({"financial_statement", "filing"}):
            status = "full"
        else:
            status = "partial"
        coverage = {
            "status": status,
            "providers": [snapshot.coverage for snapshot in snapshots],
            "provider_errors": provider_errors,
            "item_count": len(items),
            "kinds": sorted(kinds),
        }
        return CompanyEvidenceSnapshot.build(
            symbol=symbol,
            as_of=as_of,
            identity=identity,
            items=items,
            coverage=coverage,
        )


def _merge_identity(
    symbol: str,
    snapshots: Iterable[CompanyEvidenceSnapshot],
    *,
    issuer_names: Mapping[str, str],
) -> IssuerIdentity:
    candidates = [snapshot.identity for snapshot in snapshots]
    verified = next((identity for identity in candidates if identity.identity_status == "verified"), None)
    base = verified or (candidates[0] if candidates else IssuerIdentity(issuer_names.get(symbol, symbol)))
    external_ids: dict[str, str] = {}
    for identity in candidates:
        external_ids.update(dict(identity.external_ids or {}))
    return IssuerIdentity(
        issuer_name=base.issuer_name or issuer_names.get(symbol, symbol),
        instrument_type=base.instrument_type,
        exchange=base.exchange,
        venue=base.venue,
        currency=base.currency,
        external_ids=external_ids,
        identity_status=base.identity_status,
    )


def _dedupe_items(items: Iterable[CompanyEvidenceItem]) -> tuple[CompanyEvidenceItem, ...]:
    result: list[CompanyEvidenceItem] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.item_id, item.content_hash)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


__all__ = ["CompositeCompanyEvidenceProvider"]
