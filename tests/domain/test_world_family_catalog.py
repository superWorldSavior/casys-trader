from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_family_catalog import (
    FAMILY_CATALOG_SCHEMA,
    FamilyCatalog,
    normalize_family_id,
    normalize_symbol,
)

MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_family_catalog.py"


def _entries() -> dict[str, str]:
    return {"TTE.PA": "eu_energy", "ASML.AS": "eu_tech", "SAN.PA": "eu_healthcare"}


def test_normalization_is_shared_between_build_and_lookup() -> None:
    assert normalize_symbol("  tte.pa ") == "TTE.PA"
    assert normalize_family_id("  EU_Tech ") == "eu_tech"
    with pytest.raises(ValueError, match="whitespace"):
        normalize_symbol("TTE PA")
    with pytest.raises(ValueError, match="bare taxonomy id"):
        normalize_family_id("EU Tech!")
    with pytest.raises(ValueError, match="non-empty"):
        normalize_symbol("   ")


def test_catalog_hash_is_deterministic_and_content_bound() -> None:
    first = FamilyCatalog(catalog_id="family_catalog.v1", entries=_entries())
    second = FamilyCatalog(catalog_id="family_catalog.v1", entries=dict(reversed(list(_entries().items()))))
    assert first.content_sha256 == second.content_sha256
    assert len(first.content_sha256 or "") == 64
    altered = FamilyCatalog(catalog_id="family_catalog.v1", entries={**_entries(), "SAP.DE": "eu_tech"})
    assert altered.content_sha256 != first.content_sha256
    renamed = FamilyCatalog(catalog_id="family_catalog.v2", entries=_entries())
    assert renamed.content_sha256 != first.content_sha256
    with pytest.raises(ValueError, match="content_sha256"):
        FamilyCatalog(catalog_id="family_catalog.v1", entries=_entries(), content_sha256="0" * 64)


def test_catalog_round_trips_and_rejects_unknown_fields() -> None:
    catalog = FamilyCatalog(catalog_id="family_catalog.v1", entries=_entries())
    payload = catalog.to_dict()
    assert payload["schema_version"] == FAMILY_CATALOG_SCHEMA
    replayed = FamilyCatalog.from_mapping(payload)
    assert replayed == catalog
    assert replayed.content_sha256 == catalog.content_sha256
    with pytest.raises(ValueError, match="unknown fields"):
        FamilyCatalog.from_mapping({**payload, "owner": "desk"})
    with pytest.raises(ValueError, match="schema_version"):
        FamilyCatalog.from_mapping({**payload, "schema_version": "family_catalog.v2"})
    with pytest.raises(TypeError, match="FamilyCatalog or a mapping"):
        FamilyCatalog.from_mapping(42)  # type: ignore[arg-type]


def test_catalog_rejects_duplicates_after_normalization() -> None:
    with pytest.raises(ValueError, match="duplicate catalog symbol"):
        FamilyCatalog(catalog_id="c", entries={"TTE.PA": "eu_energy", "tte.pa": "eu_tech"})


def test_from_grouped_fails_closed_on_dual_membership() -> None:
    catalog = FamilyCatalog.from_grouped(
        "family_catalog.v1",
        {"eu_energy": ["TTE.PA"], "eu_tech": ["ASML.AS", "SAP.DE"]},
    )
    assert catalog.family_for_symbol("tte.pa") == "eu_energy"
    with pytest.raises(ValueError, match="claimed by families"):
        FamilyCatalog.from_grouped("c", {"eu_energy": ["TTE.PA"], "eu_tech": ["TTE.PA"]})
    with pytest.raises(TypeError, match="list or tuple"):
        FamilyCatalog.from_grouped("c", {"eu_energy": "TTE.PA"})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mapping of family to symbols"):
        FamilyCatalog.from_grouped("c", ["eu_energy"])  # type: ignore[arg-type]


def test_lookups_are_total_and_sorted() -> None:
    catalog = FamilyCatalog(catalog_id="family_catalog.v1", entries=_entries())
    assert catalog.families == frozenset({"eu_energy", "eu_tech", "eu_healthcare"})
    assert catalog.family_for_symbol("san.pa") == "eu_healthcare"
    assert catalog.family_for_symbol("UNKNOWN.X") is None
    assert catalog.symbols_for_family("EU_TECH") == ("ASML.AS",)
    multi = FamilyCatalog.from_grouped("c", {"eu_tech": ["SAP.DE", "ASML.AS"]})
    assert multi.symbols_for_family("eu_tech") == ("ASML.AS", "SAP.DE")


def test_catalog_is_immutable() -> None:
    catalog = FamilyCatalog(catalog_id="c", entries=_entries())
    with pytest.raises(FrozenInstanceError):
        catalog.catalog_id = "other"  # type: ignore[misc]


def test_world_family_catalog_stays_stdlib_domain() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
