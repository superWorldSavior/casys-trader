"""Read-only YAML adapter for the frozen family-catalog generation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from trader.domain.world_family_catalog import FAMILY_CATALOG_SCHEMA, FamilyCatalog

_CATALOG_FILE = "world_family_catalog.yaml"
_DOCUMENT_KEYS = frozenset({"schema_version", "catalog_id", "entries", "content_sha256"})


def family_catalog_config_path(path: str | Path) -> Path:
    resolved = Path(path)
    if resolved.is_dir():
        return resolved / _CATALOG_FILE
    return resolved


def load_family_catalog(path: str | Path) -> FamilyCatalog:
    """Load and hash-verify one catalog generation. Documentary keys ignored."""

    resolved = family_catalog_config_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"family catalog is missing: {resolved}")
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("family catalog must be a YAML mapping")
    if payload.get("schema_version") not in (None, "", FAMILY_CATALOG_SCHEMA):
        raise ValueError(f"schema_version must be {FAMILY_CATALOG_SCHEMA}")
    catalog = FamilyCatalog.from_mapping({k: v for k, v in payload.items() if k in _DOCUMENT_KEYS})
    stated = payload.get("content_sha256")
    if stated is not None and stated != catalog.content_sha256:
        raise ValueError("family catalog content_sha256 does not match its entries")
    return catalog


__all__ = [
    "family_catalog_config_path",
    "load_family_catalog",
]
