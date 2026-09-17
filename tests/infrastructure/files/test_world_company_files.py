from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from trader.domain.world_family_catalog import FamilyCatalog
from trader.infrastructure.files.company_briefs import load_company_briefs
from trader.infrastructure.files.family_catalog_config import (
    family_catalog_config_path,
    load_family_catalog,
)


def _brief_file(symbol: str, *, depth: str = "screen", exchange: str | None = "TAI") -> dict:
    brief = {
        "brief_id": f"company_micro:v1:{symbol}:abc",
        "symbol": symbol,
        "as_of": "2026-09-05T17:26:49+00:00",
        "input_signature": "0" * 64,
        "depth": depth,
        "issuer_identity": {"issuer_name": f"{symbol} Inc", "exchange": exchange, "identity_status": "verified"},
        "coverage": {"status": "full"},
        "company_thesis": {"status": "watch"},
        "source_refs": ["s1"],
    }
    return {"schema_version": 1, "symbol": symbol, "briefs": {depth: brief}}


def test_family_catalog_config_loads_and_verifies_hash(tmp_path: Path) -> None:
    catalog = FamilyCatalog.from_grouped("family_catalog.v1", {"eu_tech": ["ASML.AS"], "eu_energy": ["TTE.PA"]})
    assert family_catalog_config_path(tmp_path) == tmp_path / "world_family_catalog.yaml"
    assert family_catalog_config_path(tmp_path / "custom.yaml") == tmp_path / "custom.yaml"
    target = tmp_path / "world_family_catalog.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "schema_version": "family_catalog.v1",
                "catalog_id": catalog.catalog_id,
                "content_sha256": catalog.content_sha256,
                "source": "test",
                "entries": dict(catalog.entries),
            }
        ),
        encoding="utf-8",
    )
    assert load_family_catalog(tmp_path) == catalog
    target.write_text(
        yaml.safe_dump(
            {
                "schema_version": "family_catalog.v1",
                "catalog_id": catalog.catalog_id,
                "content_sha256": "0" * 64,
                "entries": dict(catalog.entries),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="content_sha256"):
        load_family_catalog(tmp_path)
    with pytest.raises(FileNotFoundError, match="missing"):
        load_family_catalog(tmp_path / "absent")


def test_company_briefs_prefer_deep_and_count_skips(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    both = _brief_file("AAA", depth="screen")
    both["briefs"]["deep"] = _brief_file("AAA", depth="deep")["briefs"]["deep"]
    (current / "a.json").write_text(json.dumps(both), encoding="utf-8")
    (current / "b.json").write_text(json.dumps(_brief_file("BBB")), encoding="utf-8")
    (current / "broken.json").write_text("{nope", encoding="utf-8")
    (current / "empty.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    corpus = load_company_briefs(tmp_path)
    assert set(corpus.briefs) == {"AAA", "BBB"}
    assert corpus.briefs["AAA"].depth == "deep"
    assert corpus.briefs["BBB"].depth == "screen"
    assert corpus.skipped_count == 2
    assert load_company_briefs(tmp_path / "absent").briefs == {}


def test_company_briefs_reject_duplicate_symbols(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    (current / "a.json").write_text(json.dumps(_brief_file("AAA")), encoding="utf-8")
    (current / "b.json").write_text(json.dumps(_brief_file("AAA")), encoding="utf-8")
    corpus = load_company_briefs(tmp_path)
    assert set(corpus.briefs) == {"AAA"}
    assert corpus.skipped_count == 1


def test_company_brief_file_loads_single_and_never_raises(tmp_path: Path) -> None:
    from trader.infrastructure.files.company_briefs import load_company_brief_file

    good = tmp_path / "good.json"
    good.write_text(json.dumps(_brief_file("AAA")), encoding="utf-8")
    brief = load_company_brief_file(good)
    assert brief is not None
    assert brief.symbol == "AAA"
    assert load_company_brief_file(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_company_brief_file(broken) is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"briefs": {}}), encoding="utf-8")
    assert load_company_brief_file(empty) is None
